# bot.py
import os
import time
import logging
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from alpaca_trade_api.rest import REST, TimeFrame

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# API & ENV
# =========================
API_KEY     = os.getenv("APCA_API_KEY_ID", "")
API_SECRET  = os.getenv("APCA_API_SECRET_KEY", "")
API_BASE_URL= os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, API_BASE_URL)

# رموز التداول (تقدر تغيّرها من المتغير البيئي SYMBOLS)
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "AAPL,MSFT,AMZN,NVDA,META,TSLA,GOOGL,AMD"
).split(",") if s.strip()]

# =========================
# Strategy Params (ENV)
# =========================
# --- مرونة الدخول: OR = يكفي شرط واحد / AND = لازم كل الشروط
ENTRY_MODE = os.getenv("ENTRY_MODE", "OR").upper()  # "OR" or "AND"

# --- شرط اليومي: صعود عن افتتاح اليوم (مثلاً 0.003 = 0.3%)
ENABLE_DAY_TREND = os.getenv("ENABLE_DAY_TREND", "1") == "1"
DAY_TREND_PCT    = float(os.getenv("DAY_TREND_PCT", "0.003"))

# --- شرط الدقيقة: نسبة الحركة في آخر شمعة 1 دقيقة (مثلاً 0.001 = 0.1%)
ENABLE_ONE_MIN   = os.getenv("ENABLE_ONE_MIN", "1") == "1"
ONE_MIN_PCT      = float(os.getenv("ONE_MIN_PCT", "0.001"))
ONE_MIN_MIN_VOL  = float(os.getenv("ONE_MIN_MIN_VOL", "0"))  # اختياري فلتر حجم

# --- زمن التكرار بين المسوح (ثواني)
SCAN_SLEEP_SECS  = float(os.getenv("SCAN_SLEEP_SECS", "3"))

# --- إدارة الأموال: تقسيم الكاش على عدد فتحات (Slots)
MAX_SLOTS        = int(os.getenv("SLOTS", "1"))
CASH_BUFFER      = float(os.getenv("CASH_BUFFER", "5"))  # احتياط بسيط لمنع أخطاء الكمية

# --- الخروج: Trailing Stop بالدولار الأدنى أو بالنسبة
TRAIL_PCT        = float(os.getenv("TRAIL_PCT", "0.0040"))  # 0.40% افتراضي
TRAIL_MIN_DOLLAR = float(os.getenv("TRAIL_MIN", "0.20"))    # لا يقل عن 20 سنت
# خيار إيقاف ثابت اختياري (دولارات من متوسط الدخول). اتركه 0 لتعطيله.
FIXED_STOP_DOLLAR = float(os.getenv("STOP_LOSS_DOLLAR", "0.60"))

# --- مرة واحدة لكل سهم في اليوم
ONE_ENTRY_PER_SYMBOL_PER_DAY = os.getenv("ONE_ENTRY_PER_SYMBOL_PER_DAY", "1") == "1"

US_EASTERN = ZoneInfo("America/New_York")

# =========================
# Helpers (time / day-open)
# =========================
def utc_now():
    return datetime.now(timezone.utc)

def today_date_ny():
    return datetime.now(US_EASTERN).date()

@lru_cache(maxsize=256)
def get_today_open(symbol: str) -> float | None:
    """سعر افتتاح اليوم للرمز اعتماداً على شمعات يومية من ألباكا (بدون tz_convert)."""
    try:
        start_utc = (utc_now() - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
        bars = api.get_bars(symbol, TimeFrame.Day, start=start_utc, end=utc_now(), limit=3, feed="sip")
        if not bars:
            return None
        target_date = today_date_ny()
        for b in bars:
            if b.t.astimezone(US_EASTERN).date() == target_date:
                return float(b.o)
        return None
    except Exception as e:
        logging.error(f"[BARS] {symbol} day open fetch error: {e}")
        return None

def day_move_from_open(symbol: str, last_price: float) -> float | None:
    """نسبة الحركة من الافتتاح: (last - open) / open."""
    o = get_today_open(symbol)
    if not o or o <= 0:
        return None
    return (last_price - o) / o

def compute_trail_amount(avg_price: float) -> float:
    """حساب مقدار التريل بالدولار (الأكبر بين نسبة مئوية وحد أدنى)."""
    return round(max(TRAIL_MIN_DOLLAR, avg_price * TRAIL_PCT), 4)

def slots_in_use() -> int:
    """عدد الصفقات المفتوحة حالياً (عدد المراكز)."""
    try:
        positions = api.list_positions()
        return len(positions)
    except Exception as e:
        logging.error(f"[SLOTS] positions fetch error: {e}")
        return 0

def have_open_position(symbol: str) -> bool:
    try:
        api.get_position(symbol)
        return True
    except Exception:
        return False

def compute_qty_from_slot(price: float) -> int:
    """تقدير الكمية لكل صفقة بناءً على القوة الشرائية وعدد الـ slots."""
    try:
        acc = api.get_account()
        buying_power = float(getattr(acc, "buying_power"))
    except Exception as e:
        logging.error(f"[QTY] account fetch error: {e}")
        return 0

    slots_left = max(1, MAX_SLOTS - slots_in_use())
    alloc_per_slot = max(0.0, (buying_power - CASH_BUFFER) / slots_left)
    if price <= 0 or alloc_per_slot <= 0:
        return 0
    qty = int(alloc_per_slot // price)
    return max(qty, 0)

# =========================
# Day lock (once per symbol/day)
# =========================
day_locks: dict[str, str] = {}  # symbol -> "YYYY-MM-DD"

def is_locked_today(symbol: str) -> bool:
    d = today_date_ny().isoformat()
    return day_locks.get(symbol) == d

def lock_today(symbol: str):
    d = today_date_ny().isoformat()
    day_locks[symbol] = d
    logging.info(f"[ONE] {symbol} locked for {d}.")

# =========================
# Orders
# =========================
def place_trailing_stop(symbol: str, qty: int, avg_entry: float):
    """يضع أمر Trailing stop آمن بالدولار (trail_price)."""
    trail = compute_trail_amount(avg_entry)
    try:
        api.submit_order(
            symbol=symbol,
            side="sell",
            qty=qty,
            type="trailing_stop",
            trail_price=trail,
            time_in_force="day",
        )
        logging.info(f"[EXIT] TRAIL {symbol} trail=${trail:.4f} (avg={avg_entry:.4f}, qty={qty})")
    except Exception as e:
        logging.error(f"[EXIT] Trailing stop error for {symbol}: {e}")

    # إيقاف ثابت اختياري
    if FIXED_STOP_DOLLAR > 0:
        try:
            stop_price = round(max(0.01, avg_entry - FIXED_STOP_DOLLAR), 4)
            api.submit_order(
                symbol=symbol,
                side="sell",
                qty=qty,
                type="stop",
                stop_price=stop_price,
                time_in_force="day",
            )
            logging.info(f"[EXIT] FIXED SL {symbol} @ {stop_price} (avg={avg_entry:.4f})")
        except Exception as e:
            logging.error(f"[EXIT] Fixed stop error for {symbol}: {e}")

def place_entry_and_exits(symbol: str, qty: int):
    """يرسل أمر شراء، ثم ينتظر ظهور المركز ليضع أوامر الخروج."""
    if qty <= 0:
        return
    try:
        api.submit_order(
            symbol=symbol,
            side="buy",
            qty=qty,
            type="market",
            time_in_force="day",
        )
        logging.info(f"[BUY] Market order sent for {symbol}, qty={qty}")
    except Exception as e:
        logging.error(f"[BUY] Submit error for {symbol}: {e}")
        return

    # ننتظر ظهور المركز (حل مشكلة insufficient qty)
    avg_price = None
    filled_qty = 0
    for _ in range(12):  # بحد أقصى ~36 ثانية
        time.sleep(3)
        try:
            pos = api.get_position(symbol)
            avg_price = float(pos.avg_entry_price)
            filled_qty = int(float(pos.qty))
            if filled_qty > 0:
                break
        except Exception:
            continue

    if filled_qty <= 0 or not avg_price:
        logging.error(f"[EXIT] Could not determine filled qty/avg for {symbol}. No exits placed.")
        return

    place_trailing_stop(symbol, filled_qty, avg_price)

# =========================
# Conditions
# =========================
def evaluate_entry_conditions(symbol: str) -> tuple[bool, dict]:
    """
    يفحص الشروط المفعّلة ويرجع (قرار الدخول, تفاصيل للمطبوعات).
    ENTRY_MODE=OR: يكفي تحقق شرط واحد / AND: لازم كل الشروط.
    """
    # آخر شمعة دقيقة
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=1, feed="sip")
        if not bars:
            return (False, {"reason": "no_1m_bars"})
        bar = bars[0]
        last_c = float(bar.c)
        last_v = float(getattr(bar, "v", 0.0))
    except Exception as e:
        logging.error(f"[SCAN] 1m fetch error for {symbol}: {e}")
        return (False, {"reason": "1m_fetch_error"})

    checks = []
    details = {}

    # شرط الدقيقة
    if ENABLE_ONE_MIN:
        try:
            move_1m = (bar.c - bar.o) / bar.o if bar.o else 0.0
            vol_ok  = (last_v >= ONE_MIN_MIN_VOL)
            cond_1m = (move_1m >= ONE_MIN_PCT) and vol_ok
            checks.append(cond_1m)
            details["1m_move"] = move_1m
            details["1m_vol_ok"] = vol_ok
        except Exception:
            checks.append(False)
            details["1m_error"] = True

    # شرط اليومي
    if ENABLE_DAY_TREND:
        dm = day_move_from_open(symbol, last_c)
        if dm is None:
            checks.append(False)
            details["day_move"] = None
        else:
            cond_day = (dm >= DAY_TREND_PCT)
            checks.append(cond_day)
            details["day_move"] = dm

    # قرار الدخول
    ok = False
    if not checks:
        ok = False
    elif ENTRY_MODE == "AND":
        ok = all(checks)
    else:
        ok = any(checks)

    details["last"] = last_c
    return (ok, details)

# =========================
# Main loop
# =========================
def main():
    logging.info(
        "Starting bot | ENTRY_MODE=%s | day_trend=%s(%.4f) | one_min=%s(%.4f, min_vol=%.0f) | "
        "sleep=%.1fs | slots=%d | cash_buffer=$%.2f | exits: trail=%.4f(min $%.2f) stop=%.2f | one_entry=%s",
        ENTRY_MODE, ENABLE_DAY_TREND, DAY_TREND_PCT,
        ENABLE_ONE_MIN, ONE_MIN_PCT, ONE_MIN_MIN_VOL,
        SCAN_SLEEP_SECS, MAX_SLOTS, CASH_BUFFER, TRAIL_PCT, TRAIL_MIN_DOLLAR, FIXED_STOP_DOLLAR,
        ONE_ENTRY_PER_SYMBOL_PER_DAY
    )

    # مزامنة “قفل اليوم” من أوامر اليوم (حماية إضافية عند إعادة التشغيل)
    try:
        d = today_date_ny().isoformat()
        open_orders = api.list_orders(status="all", after=(utc_now() - timedelta(hours=8)))
        for o in open_orders:
            sym = getattr(o, "symbol", None)
            submitted_at = getattr(o, "submitted_at", None)
            if not sym or not submitted_at:
                continue
            if submitted_at.astimezone(US_EASTERN).date().isoformat() == d:
                day_locks[sym] = d
        logging.info(f"[ONE][SYNC] Locked {len(set(day_locks.keys()))} symbols from today's orders.")
    except Exception:
        pass

    while True:
        try:
            clock = api.get_clock()
            if not clock.is_open:
                time.sleep(SCAN_SLEEP_SECS)
                continue

            # لا نتجاوز عدد الفتحات
            if slots_in_use() >= MAX_SLOTS:
                time.sleep(SCAN_SLEEP_SECS)
                continue

            for sym in SYMBOLS:
                # مرّة واحدة لكل سهم في اليوم
                if ONE_ENTRY_PER_SYMBOL_PER_DAY and is_locked_today(sym):
                    logging.info(f"[ONE] {sym}: already traded on {today_date_ny()}. Skip.")
                    continue

                # لا تدخل لو عندك مركز مفتوح في السهم
                if have_open_position(sym):
                    continue

                ok, info = evaluate_entry_conditions(sym)

                # طباعة شفافة للسبب
                day_txt = "n/a" if info.get("day_move") is None else f"{info['day_move']:.2%}"
                one_txt = "n/a"
                if "1m_move" in info:
                    one_txt = f"{info['1m_move']:.2%}"
                    if ENABLE_ONE_MIN:
                        one_txt += " (vol ok)" if info.get("1m_vol_ok") else " (vol low)"
                logging.info(f"[SCAN] {sym} -> {'ENTER' if ok else 'SKIP'} | day {day_txt} vs {DAY_TREND_PCT:.2%}; "
                             f"1m {one_txt} vs {ONE_MIN_PCT:.2%}")

                if not ok:
                    continue

                qty = compute_qty_from_slot(info["last"])
                if qty <= 0:
                    logging.info(f"[QTY] {sym} qty=0 -> skip")
                    lock_today(sym)  # قفل ناعم حتى لا نحاول مراراً
                    continue

                # نفّذ دخول + ضع أوامر الخروج
                place_entry_and_exits(sym, qty)
                if ONE_ENTRY_PER_SYMBOL_PER_DAY:
                    lock_today(sym)

                # بعد تنفيذ صفقة واحدة في هذه الدورة، اترك مهلة قصيرة
                time.sleep(SCAN_SLEEP_SECS)

            time.sleep(SCAN_SLEEP_SECS)

        except Exception as e:
            logging.error(f"[MAIN] Loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
