import os
import time
import logging
from math import floor
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from alpaca_trade_api.rest import REST, TimeFrame

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# Environment / API client
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,AMZN").split(",") if s.strip()]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, base_url=BASE_URL)

# =========================
# Config (aligns with your logs)
# =========================
ENTRY_MODE = os.getenv("ENTRY_MODE", "OR").upper()   # "OR" أو "AND"

# day_trend=True(0.0030)  → فعّل شرط اليومي وبنسبة حدية
DAY_TREND_ENABLED = os.getenv("DAY_TREND_ENABLED", "True").lower() == "true"
DAY_TREND_PCT     = float(os.getenv("DAY_TREND_PCT", "0.0030"))

# one_min=True(0.0010, min_vol=0)
ONE_MIN_ENABLED = os.getenv("ONE_MIN_ENABLED", "True").lower() == "true"
ONE_MIN_PCT     = float(os.getenv("ONE_MIN_PCT", "0.0010"))
MIN_VOL_1M      = int(os.getenv("MIN_VOL_1M", "0"))

# حلقات التشغيل
SLEEP = float(os.getenv("SLEEP", "3.0"))

# إدارة رأس المال (حسب ما كان ظاهر عندك)
SLOTS      = int(os.getenv("SLOTS", "1"))            # عدد المراكز القصوى بالتوازي
CASH_BUFFER= float(os.getenv("CASH_BUFFER", "5.0"))  # احتياطي لا يُستخدم
ONE_ENTRY  = os.getenv("ONE_ENTRY", "True").lower() == "true"  # إذا True يمنع دخول صفقة جديدة بوجود مركز مفتوح

# اختياري: لتحديد حجم الصفقة بدقة
FIXED_DOLLARS_PER_TRADE = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "0"))
PCT_OF_EQUITY_PER_TRADE = float(os.getenv("PCT_OF_EQUITY_PER_TRADE", "0"))
MAX_POSITIONS_PER_SYMBOL= int(os.getenv("MAX_POSITIONS_PER_SYMBOL", "1"))

# exits: trail=0.0040(min $0.20) stop=0.60  (نستخدم هنا Bracket TP/SL؛ التريلينغ اختياري)
TRAIL_PCT        = float(os.getenv("TRAIL_PCT", "0.0040"))     # غير مستخدمة في bracket مباشرة
TRAIL_MIN_DOLLAR = float(os.getenv("TRAIL_MIN_DOLLAR", "0.20"))# غير مستخدمة مباشرة
STOP_LOSS_PCT    = float(os.getenv("STOP_LOSS_PCT", "0.60"))   # 60% نزول (Stop واسع جدًا—غالبًا عندك كنسبة موجبة)
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT", "0.20")) # +20% ربح (للتجارب يمكن تقللها)

# =========================
# Helpers
# =========================
def rfc3339(dt: datetime) -> str:
    """Return UTC RFC3339 like 2025-09-01T00:00:00Z"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def account_snapshot():
    try:
        return api.get_account()
    except Exception as e:
        logging.error(f"[ACCT] error: {e}")
        return None

def buying_power_available() -> float:
    acct = account_snapshot()
    if not acct:
        return 0.0
    try:
        bp = float(acct.buying_power)  # قد تكون أكبر من الكاش إذا فيه رافعة
    except Exception:
        bp = 0.0
    avail = max(0.0, bp - CASH_BUFFER)
    return avail

def open_positions():
    try:
        return api.list_positions()
    except Exception as e:
        logging.error(f"[POS] list error: {e}")
        return []

def positions_in_symbol(symbol: str):
    return [p for p in open_positions() if p.symbol == symbol]

def can_open_more_positions() -> bool:
    return len(open_positions()) < SLOTS

def budget_per_trade() -> float:
    available = buying_power_available()
    if available <= 0:
        return 0.0
    if FIXED_DOLLARS_PER_TRADE > 0:
        return min(FIXED_DOLLARS_PER_TRADE, available)
    if PCT_OF_EQUITY_PER_TRADE > 0:
        return min(available, available * PCT_OF_EQUITY_PER_TRADE)
    # افتراضي: قسّم المتاح على الخانات المتبقية
    remaining = max(1, SLOTS - len(open_positions()))
    return available / remaining

def shares_for(price: float, budget: float) -> int:
    return max(0, floor(budget / max(price, 0.01)))

# =========================
# Market data
# =========================
def get_day_open(symbol: str) -> Optional[float]:
    """Open price of the latest daily bar."""
    try:
        now = datetime.now(timezone.utc)
        start = rfc3339(now - timedelta(days=5))  # ✅ RFC3339
        end   = rfc3339(now)
        bars = api.get_bars(symbol, TimeFrame.Day, start, end).df
        if bars.empty:
            return None
        # آخر يوم (آخر صف)
        return float(bars.iloc[-1]["open"])
    except Exception as e:
        logging.error(f"[BARS] {symbol} day open fetch error: {e}")
        return None

def get_last_1m_change_and_vol(symbol: str) -> (Optional[float], Optional[int]):
    """
    يرجّع نسبة التغير آخر دقيقة (مقارنةً بالبار السابق) وحجم آخر دقيقة.
    """
    try:
        now = datetime.now(timezone.utc)
        start = rfc3339(now - timedelta(minutes=10))
        end   = rfc3339(now)
        bars = api.get_bars(symbol, TimeFrame.Minute, start, end).df
        if bars is None or len(bars) < 2:
            return None, None
        last = bars.iloc[-1]
        prev = bars.iloc[-2]
        if prev["close"] == 0:
            return None, int(last.get("volume", 0))
        change = (float(last["close"]) - float(prev["close"])) / float(prev["close"])
        vol = int(last.get("volume", 0))
        return change, vol
    except Exception as e:
        logging.error(f"[BARS] {symbol} 1m bars error: {e}")
        return None, None

def get_last_price(symbol: str) -> Optional[float]:
    """Use latest trade price (أدق من عرض/طلب)."""
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price)
    except Exception as e:
        logging.error(f"[PRICE] {symbol} fetch error: {e}")
        return None

# =========================
# Signals
# =========================
def day_trend_signal(symbol: str) -> Optional[bool]:
    if not DAY_TREND_ENABLED:
        return None
    day_open = get_day_open(symbol)
    last_price = get_last_price(symbol)
    if day_open is None or last_price is None or day_open == 0:
        return False
    day_change = (last_price - day_open) / day_open
    ok = day_change >= DAY_TREND_PCT
    logging.info(f"[SCAN] {symbol} | day {day_change:.4f} vs {DAY_TREND_PCT:.4f} → {ok}")
    return ok

def one_min_signal(symbol: str) -> Optional[bool]:
    if not ONE_MIN_ENABLED:
        return None
    chg, vol = get_last_1m_change_and_vol(symbol)
    if chg is None or vol is None:
        return False
    vol_ok = vol >= MIN_VOL_1M
    ok = (chg >= ONE_MIN_PCT) and vol_ok
    logging.info(f"[SCAN] {symbol} | 1m {chg:.4f} vs {ONE_MIN_PCT:.4f}; vol {vol} (ok={vol_ok}) → {ok}")
    return ok

def entry_decision(symbol: str) -> bool:
    signals = []
    dt = day_trend_signal(symbol)
    if dt is not None:
        signals.append(dt)
    om = one_min_signal(symbol)
    if om is not None:
        signals.append(om)

    if not signals:  # لا شروط مفعلة
        return False

    if ENTRY_MODE == "AND":
        return all(signals)
    # الافتراضي OR
    return any(signals)

# =========================
# Orders
# =========================
def place_bracket_buy(symbol: str, price_hint: float, qty: int):
    """
    نستخدم Bracket: أمر شراء + TakeProfit + StopLoss.
    ملاحظة: Alpaca لا يجمع Trailing مع Bracket في أمر واحد، لذا نعتمد TP/SL هنا.
    """
    if qty <= 0:
        logging.info(f"[BUY] {symbol} skip—qty<=0")
        return

    # حدد أسعار TP/SL
    tp_price = round(price_hint * (1.0 + TAKE_PROFIT_PCT), 2)
    sl_price = round(price_hint * (1.0 - STOP_LOSS_PCT), 2)  # تحذير: 0.60 يعني 60% نزول!

    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force="day",
            order_class="bracket",
            take_profit={"limit_price": tp_price},
            stop_loss={"stop_price": sl_price}
        )
        logging.info(f"[BUY] {symbol} qty={qty} @~{price_hint:.2f} TP={tp_price} SL={sl_price} (bracket placed)")
        return order
    except Exception as e:
        logging.error(f"[BUY] {symbol} error: {e}")
        return None

# =========================
# Main loop
# =========================
def run_bot():
    logging.info(
        f"Starting bot | ENTRY_MODE={ENTRY_MODE} | "
        f"day_trend={DAY_TREND_ENABLED}({DAY_TREND_PCT}) | "
        f"one_min={ONE_MIN_ENABLED}({ONE_MIN_PCT}, min_vol={MIN_VOL_1M}) | "
        f"sleep={SLEEP}s | slots={SLOTS} | cash_buffer=${CASH_BUFFER:.2f} | "
        f"exits: TP={TAKE_PROFIT_PCT} stop={STOP_LOSS_PCT} | one_entry={ONE_ENTRY}"
    )

    while True:
        try:
            # احترام ONE_ENTRY: لا تدخل إن كان فيه مراكز مفتوحة
            if ONE_ENTRY and len(open_positions()) > 0:
                logging.info("[ENTRY] one_entry=True & positions>0 → skipping new entries")
                time.sleep(SLEEP)
                continue

            for symbol in SYMBOLS:
                # لا تكرر شراء نفس السهم أكثر من المسموح
                if len(positions_in_symbol(symbol)) >= MAX_POSITIONS_PER_SYMBOL:
                    logging.info(f"[ENTRY] {symbol} skip—already holding (limit {MAX_POSITIONS_PER_SYMBOL})")
                    continue

                # تحقق من الخانات
                if not can_open_more_positions():
                    logging.info(f"[ENTRY] slots full ({SLOTS}) → skipping new entries")
                    break

                # قرار الدخول
                if not entry_decision(symbol):
                    continue

                # حساب الكمية والميزانية
                price = get_last_price(symbol)
                if price is None or price <= 0:
                    logging.info(f"[ENTRY] {symbol} skip—no price")
                    continue

                budget = budget_per_trade()
                qty = shares_for(price, budget)
                if qty <= 0:
                    logging.info(f"[ENTRY] {symbol} skip—budget ${budget:.2f} too small for price {price:.2f}")
                    continue

                place_bracket_buy(symbol, price, qty)

            time.sleep(SLEEP)

        except Exception as loop_err:
            logging.error(f"[LOOP] error: {loop_err}")
            time.sleep(SLEEP)

# =========================
# Entrypoint
# =========================
if __name__ == "__main__":
    run_bot()
