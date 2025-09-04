import os
import time
import json
import logging
from typing import List, Dict
from datetime import datetime

import pytz
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

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Strategy Params
# =========================
SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,NVDA,TSLA,AMZN").split(",") if s.strip()]
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.003"))  # 0.3% على آخر دقيقة
FIXED_DOLLARS_PER_TRADE = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "5000"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.01"))  # 1%
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", "0.01"))   # 1%
POLL_SECONDS    = int(os.getenv("POLL_SECONDS", "15"))

# توقيت السوق
NY = pytz.timezone("America/New_York")

# =========================
# Persistent State
#   locked_after_sell[sym] = True  -> لا يُسمح بالشراء لهذا الرمز لباقي اليوم
#   had_position[sym]      = True  -> كان ماسك مركز آخر دورة
#   date = YYYY-MM-DD بتوقيت نيويورك
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
def get_last_two_closes(sym: str):
    """يرجع آخر إغلاقين (شمعة دقيقة)"""
    bars = api.get_bars(sym, TimeFrame.Minute, limit=2)
    if len(bars) < 2:
        return None, None
    return float(bars[-2].c), float(bars[-1].c)

def calc_qty_for_dollars(sym: str, dollars: float) -> int:
    """حساب الكمية حسب المبلغ الثابت"""
    quote = api.get_latest_trade(sym)
    price = float(quote.price)
    if price <= 0:
        return 0
    qty = int(dollars // price)
    return max(qty, 0)

def place_bracket_buy(sym: str, qty: int):
    """شراء Market + أوامر TP/SL كـ bracket"""
    quote = api.get_latest_trade(sym)
    entry = float(quote.price)
    take_profit = round(entry * (1 + TAKE_PROFIT_PCT), 4)
    stop_loss   = round(entry * (1 - STOP_LOSS_PCT), 4)

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

def ensure_protective_stop(sym: str):
    """
    إذا كان فيه مركز مفتوح لكن ما فيه أمر وقف فعّال للسهم، أنشئ STOP حماية فورًا.
    يحميك لو لأي سبب انرسل شراء بدون bracket.
    """
    # هل ماسك مركز؟
    try:
        pos = api.get_position(sym)
        qty = int(float(pos.qty))
        if qty <= 0:
            return
    except Exception:
        return  # لا يوجد مركز

    # هل يوجد أمر وقف مفتوح بالفعل؟
    try:
        open_orders = api.list_orders(status="open", direction="asc")
    except Exception as e:
        logging.warning(f"list_orders failed: {e}")
        return

    has_stop = False
    for o in open_orders:
        try:
            if o.symbol == sym and o.side == "sell" and o.type in ("stop", "stop_limit"):
                has_stop = True
                break
        except Exception:
            continue

    if has_stop:
        return

    # ضع وقف حماية بناءً على آخر سعر
    try:
        last = float(api.get_latest_trade(sym).price)
    except Exception:
        return
    stop_price = round(last * (1 - STOP_LOSS_PCT), 4)

    logging.warning(f"{sym}: No active STOP found. Placing protective STOP at {stop_price}")
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
logging.info("Started bot (per-symbol daily lock after sell + auto protective stop).")

while True:
    try:
        # إعادة ضبط اليوم إذا تغيّر التاريخ
        today = datetime.now(NY).date().isoformat()
        if state["date"] != today:
            logging.info("New trading day detected. Resetting locks.")
            state = new_daily_state()
            save_state(state)

        # تحديث حالة المراكز + قفل الرمز بعد الإغلاق
        for sym in SYMBOLS:
            holding = False
            try:
                pos = api.get_position(sym)
                holding = float(pos.qty) > 0
            except Exception:
                holding = False

            # إذا كان ماسك سابقاً وأصبح الآن بدون مركز -> بيع تم -> اقفل بقية اليوم
            if state["had_position"].get(sym, False) and not holding and not state["locked_after_sell"].get(sym, False):
                state["locked_after_sell"][sym] = True
                logging.info(f"{sym}: position closed -> LOCKED for the rest of the day.")
                save_state(state)

            # حدّث حالة الإمساك
            state["had_position"][sym] = holding

            # ضمان وجود وقف حماية لو ما فيه bracket
            ensure_protective_stop(sym)

        # إشارات الدخول
        for sym in SYMBOLS:
            if state["locked_after_sell"][sym]:
                continue  # هذا الرمز باعه اليوم -> ممنوع إعادة شراء اليوم

            # لا تدخل إذا أنت ماسك أصلاً
            is_holding = False
            try:
                pos = api.get_position(sym)
                is_holding = float(pos.qty) > 0
            except Exception:
                is_holding = False
            if is_holding:
                continue

            # مومنتُم بسيط على آخر دقيقة
            c1, c2 = get_last_two_closes(sym)
            if c1 is None or c2 is None:
                continue
            mom = (c2 - c1) / c1

            if mom >= MOMENTUM_THRESHOLD:
                qty = calc_qty_for_dollars(sym, FIXED_DOLLARS_PER_TRADE)
                if qty <= 0:
                    continue
                place_bracket_buy(sym, qty)
                # اعتبر أننا دخلنا مركز (سيتم تركيب TP/SL تلقائياً كـ bracket)
                state["had_position"][sym] = True
                save_state(state)

        time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        logging.info("Stopped by user.")
        break
    except Exception as e:
        logging.exception(f"Loop error: {e}")
        time.sleep(5)
