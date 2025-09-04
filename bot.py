import os
import time
import json
import logging
from typing import List, Dict, Tuple, Optional
from datetime import datetime

import pytz
from alpaca_trade_api.rest import REST, TimeFrame

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# =========================
# Environment / API client
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Strategy Params
# =========================
SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "AAPL,MSFT,NVDA,TSLA,AMZN,GOOGL,META,AMD"
).split(",") if s.strip()]

TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL", "50000"))  # رأس المال الكلي
NUM_SLOTS     = int(os.getenv("NUM_SLOTS", "8"))            # تقسيم ثابت على 8 خانات
PER_TRADE_DOLLARS = TOTAL_CAPITAL / max(NUM_SLOTS, 1)

MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.003"))  # 0.3% دقيقة
DAILY_THRESHOLD    = float(os.getenv("DAILY_THRESHOLD", "0.01"))      # 1% يومي

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.01"))  # 1%
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", "0.01"))    # 1%
POLL_SECONDS    = int(os.getenv("POLL_SECONDS", "15"))

NY = pytz.timezone("America/New_York")

# =========================
# Persistent State (Daily Lock)
# =========================
STATE_FILE = "state.json"

def new_daily_state() -> Dict:
    return {
        "date": datetime.now(NY).date().isoformat(),
        "locked_after_sell": {sym: False for sym in SYMBOLS},
        "had_position": {sym: False for sym in SYMBOLS},
    }

def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return new_daily_state()
    try:
        with open(STATE_FILE, "r") as f:
            st = json.load(f)
    except Exception:
        return new_daily_state()

    today = datetime.now(NY).date().isoformat()
    if st.get("date") != today:
        st = new_daily_state()
    else:
        for sym in SYMBOLS:
            st["locked_after_sell"].setdefault(sym, False)
            st["had_position"].setdefault(sym, False)
    return st

def save_state(st: Dict):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)

state = load_state()

# =========================
# Helpers
# =========================
def two_dec(x: float) -> float:
    """إجبار خانتين عشريتين (يتفادى sub-penny)."""
    return float(f"{x:.2f}")

def get_last_two_closes(sym: str) -> Tuple[Optional[float], Optional[float]]:
    try:
        bars = api.get_bars(sym, TimeFrame.Minute, limit=2)
        if len(bars) < 2:
            return None, None
        return float(bars[-2].c), float(bars[-1].c)
    except Exception:
        return None, None

def get_today_open(sym: str) -> Optional[float]:
    try:
        daily_bars = api.get_bars(sym, TimeFrame.Day, limit=1)
        if not daily_bars:
            return None
        return float(daily_bars[0].o)
    except Exception:
        return None

def calc_qty_for_dollars(sym: str, dollars: float) -> int:
    try:
        last_trade = api.get_latest_trade(sym)
        price = float(last_trade.price)
    except Exception:
        return 0
    if price <= 0:
        return 0
    return max(int(dollars // price), 0)

def place_bracket_buy(sym: str, qty: int):
    """Market Buy مع Bracket TP/SL بأسعار بخانتين عشريتين."""
    if qty <= 0:
        return
    last_trade = api.get_latest_trade(sym)
    entry = float(last_trade.price)
    take_profit = two_dec(entry * (1 + TAKE_PROFIT_PCT))
    stop_loss   = two_dec(entry * (1 - STOP_LOSS_PCT))

    logging.info(f"{sym} BUY {qty} @~{entry} TP={take_profit} SL={stop_loss}")
    api.submit_order(
        symbol=sym,
        qty=qty,
        side='buy',
        type='market',
        time_in_force='day',
        order_class='bracket',
        take_profit={'limit_price': take_profit},
        stop_loss={'stop_price': stop_loss}
    )

def has_open_sell_orders(sym: str) -> bool:
    """هل يوجد أي أوامر بيع مفتوحة (بما فيها أوامر bracket للأطفال)؟"""
    try:
        open_orders = api.list_orders(status="open", direction="asc")
    except Exception:
        return False
    for o in open_orders:
        try:
            if o.symbol == sym and o.side == "sell":
                return True
        except Exception:
            continue
    return False

def has_active_stop(sym: str) -> bool:
    """يتأكد من وجود أمر وقف نشط لهذا الرمز."""
    try:
        open_orders = api.list_orders(status="open", direction="asc")
    except Exception:
        return False
    for o in open_orders:
        try:
            if o.symbol == sym and o.side == "sell" and o.type in ("stop", "stop_limit"):
                return True
        except Exception:
            continue
    return False

def ensure_protective_stop(sym: str):
    """
    يضيف STOP حماية فقط إذا:
    - عندك مركز (qty > 0)
    - ما فيه أي أوامر بيع مفتوحة (أوامر bracket موجودة = بيع)
    - ما فيه وقف نشط
    """
    # 1) هل ماسك مركز؟
    try:
        pos = api.get_position(sym)
        qty = int(float(pos.qty))
    except Exception:
        return
    if qty <= 0:
        return

    # 2) إذا فيه أي أوامر بيع مفتوحة (من bracket) -> لا تحط وقف حماية
    if has_open_sell_orders(sym):
        return

    # 3) إذا فيه وقف نشط أصلًا -> خلاص
    if has_active_stop(sym):
        return

    # 4) ضع وقف حماية بخانتين عشريتين
    try:
        last = float(api.get_latest_trade(sym).price)
    except Exception:
        return
    stop_price = two_dec(last * (1 - STOP_LOSS_PCT))

    logging.warning(f"{sym}: No active STOP found & no sell orders. Placing protective STOP at {stop_price}")
    try:
        api.submit_order(
            symbol=sym,
            qty=qty,
            side="sell",
            type="stop",
            time_in_force="day",
            stop_price=stop_price
        )
    except Exception as e:
        logging.error(f"Failed to place protective stop for {sym}: {e}")

# =========================
# Main Loop
# =========================
logging.info(f"Bot started | TOTAL_CAPITAL={TOTAL_CAPITAL} | NUM_SLOTS={NUM_SLOTS} | PER_TRADE_DOLLARS={PER_TRADE_DOLLARS}")

while True:
    try:
        # إعادة ضبط الأقفال عند يوم تداول جديد (بتوقيت نيويورك)
        today = datetime.now(NY).date().isoformat()
        if state["date"] != today:
            logging.info("New trading day detected. Resetting daily locks.")
            state = new_daily_state()
            save_state(state)

        # متابعة حالة المراكز + تفعيل القفل بعد البيع
        for sym in SYMBOLS:
            holding = False
            try:
                pos = api.get_position(sym)
                holding = float(pos.qty) > 0
            except Exception:
                holding = False

            # انتقال من ماسك -> فلات = بيع تم => اقفل الرمز لباقي اليوم
            if state["had_position"].get(sym, False) and not holding and not state["locked_after_sell"].get(sym, False):
                state["locked_after_sell"][sym] = True
                logging.info(f"{sym}: position closed -> LOCKED for the rest of the day.")
                save_state(state)

            state["had_position"][sym] = holding

            # ضمان وجود وقف حماية لو (ما فيه أوامر بيع مفتوحة) و (ما فيه وقف نشط)
            ensure_protective_stop(sym)

        # مسح الدخول
        for sym in SYMBOLS:
            # تخطَّ الرمز إذا مقفول اليوم أو عندك مركز فيه
            if state["locked_after_sell"].get(sym, False):
                continue
            is_holding = False
            try:
                pos = api.get_position(sym)
                is_holding = float(pos.qty) > 0
            except Exception:
                is_holding = False
            if is_holding:
                continue

            # شرط A: لحظي (دقيقة)
            c1, c2 = get_last_two_closes(sym)
            mom_ok = False
            if c1 is not None and c2 is not None and c1 > 0:
                mom_ok = ((c2 - c1) / c1) >= MOMENTUM_THRESHOLD

            # شرط B: يومي مقابل الافتتاح
            day_open = get_today_open(sym)
            daily_ok = False
            if day_open is not None and day_open > 0 and c2 is not None:
                daily_ok = ((c2 - day_open) / day_open) >= DAILY_THRESHOLD

            if mom_ok or daily_ok:
                qty = calc_qty_for_dollars(sym, PER_TRADE_DOLLARS)
                if qty > 0:
                    place_bracket_buy(sym, qty)
                    # دخلنا مركز -> لا دخول ثاني لنفس الرمز إلا بعد الخروج
                    state["had_position"][sym] = True
                    save_state(state)

        time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        logging.info("Stopped by user.")
        break
    except Exception as e:
        logging.exception(f"Loop error: {e}")
        time.sleep(5)
