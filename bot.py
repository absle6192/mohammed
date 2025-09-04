import os
import time
import json
import logging
from typing import List, Dict, Tuple, Optional
from datetime import datetime, timedelta

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

# Times
NY  = pytz.timezone("America/New_York")
UTC = pytz.UTC

# =========================
# Persistent State (Daily Lock)
# =========================
STATE_FILE = "state.json"

def new_daily_state() -> Dict:
    return {
        "date": datetime.now(NY).date().isoformat(),
        "locked_after_sell": {sym: False for sym in SYMBOLS},
        "had_position": {sym: False for sym in SYMBOLS},
        "last_buy_ts": {sym: None for sym in SYMBOLS},
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
            st["last_buy_ts"].setdefault(sym, None)
    return st

def save_state(st: Dict):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=2)

state = load_state()

# =========================
# Helpers
# =========================
def two_dec(x: float) -> float:
    return float(f"{x:.2f}")

def _to_dt(x) -> Optional[datetime]:
    if not x:
        return None
    if isinstance(x, datetime):
        return x
    s = str(x)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def is_today_ny(dt: Optional[datetime]) -> bool:
    if not dt:
        return False
    return dt.astimezone(NY).date() == datetime.now(NY).date()

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

def list_orders_all(limit: int = 200):
    try:
        return api.list_orders(status="all", limit=limit, direction="desc")
    except Exception:
        return []

OPENLIKE = {"new", "accepted", "open", "partially_filled"}

def has_open_buy_orders_today(sym: str) -> bool:
    for o in list_orders_all():
        try:
            if o.symbol == sym and o.side == "buy" and o.status in OPENLIKE and is_today_ny(_to_dt(o.created_at or o.submitted_at)):
                return True
        except Exception:
            continue
    return False

def has_open_sell_orders(sym: str) -> bool:
    for o in list_orders_all():
        try:
            if o.symbol == sym and o.side == "sell" and o.status in OPENLIKE:
                return True
        except Exception:
            continue
    return False

def has_active_stop(sym: str) -> bool:
    for o in list_orders_all():
        try:
            if o.symbol == sym and o.side == "sell" and o.type in ("stop", "stop_limit") and o.status in OPENLIKE:
                return True
        except Exception:
            continue
    return False

def bought_today(sym: str) -> bool:
    for o in list_orders_all():
        try:
            if o.symbol == sym and o.side == "buy" and is_today_ny(_to_dt(o.filled_at)):
                return True
        except Exception:
            continue
    return False

def sold_out_today(sym: str, holding_now: bool) -> bool:
    if holding_now:
        return False
    for o in list_orders_all():
        try:
            if o.symbol == sym and o.side == "sell" and is_today_ny(_to_dt(o.filled_at)):
                return True
        except Exception:
            continue
    return False

def place_bracket_buy(sym: str, qty: int):
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
    state["last_buy_ts"][sym] = datetime.now(UTC).isoformat()
    state["had_position"][sym] = True
    save_state(state)

def seconds_since_last_buy(sym: str) -> float:
    ts = state["last_buy_ts"].get(sym)
    if not ts:
        return 1e9
    try:
        dt = _to_dt(ts).astimezone(UTC)
        return (datetime.now(UTC) - dt).total_seconds()
    except Exception:
        return 1e9

def ensure_protective_stop(sym: str):
    """أضف STOP حماية فقط إذا لا يوجد bracket/بيع مفتوح، ولا وقف نشط، ومضت 20s بعد الشراء."""
    # 1) هل ماسك مركز؟
    try:
        pos = api.get_position(sym)
        qty = int(float(pos.qty))
    except Exception:
        return
    if qty <= 0:
        return

    # امنح وقت قصير بعد الشراء حتى تُنشأ أوامر bracket على السيرفر
    if seconds_since_last_buy(sym) < 20:
        return

    # 2) لو فيه أي أوامر بيع مفتوحة (من bracket) -> لا تركّب حماية
    if has_open_sell_orders(sym):
        return

    # 3) لو فيه وقف نشط -> خلاص
    if has_active_stop(sym):
        return

    # 4) ضع وقف حماية
    try:
        last = float(api.get_latest_trade(sym).price)
    except Exception:
        return
    stop_price = two_dec(last * (1 - STOP_LOSS_PCT))
    logging.warning(f"{sym}: No active STOP & no sell orders. Placing protective STOP at {stop_price}")
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
        # Reset daily locks if new NY day
        today = datetime.now(NY).date().isoformat()
        if state["date"] != today:
            logging.info("New trading day detected. Resetting daily locks.")
            state = new_daily_state()
            save_state(state)

        # Maintain positions & infer locks from real orders
        for sym in SYMBOLS:
            # هل ماسك مركز الآن؟
            holding = False
            try:
                pos = api.get_position(sym)
                holding = float(pos.qty) > 0
            except Exception:
                holding = False

            # إذا خرجنا من المركز واليوم عندنا بيع منفذ -> اقفل اليوم
            if sold_out_today(sym, holding):
                if not state["locked_after_sell"].get(sym, False):
                    state["locked_after_sell"][sym] = True
                    logging.info(f"{sym}: sold today -> LOCKED for the rest of the day.")
                    save_state(state)

            state["had_position"][sym] = holding

            # أضف وقف حماية عند الحاجة فقط
            ensure_protective_stop(sym)

        # Scan entries
        for sym in SYMBOLS:
            # لا تدخل إذا الرمز مقفول اليوم
            if state["locked_after_sell"].get(sym, False):
                continue

            # لا تدخل إذا ماسك أو عندك أوامر شراء مفتوحة اليوم أو شراء منفذ اليوم
            is_holding = False
            try:
                pos = api.get_position(sym)
                is_holding = float(pos.qty) > 0
            except Exception:
                is_holding = False
            if is_holding or has_open_buy_orders_today(sym) or bought_today(sym):
                continue

            # شرط A: لحظي
            c1, c2 = get_last_two_closes(sym)
            mom_ok = False
            if c1 is not None and c2 is not None and c1 > 0:
                mom_ok = ((c2 - c1) / c1) >= MOMENTUM_THRESHOLD

            # شرط B: يومي
            day_open = get_today_open(sym)
            daily_ok = False
            if day_open is not None and day_open > 0 and c2 is not None:
                daily_ok = ((c2 - day_open) / day_open) >= DAILY_THRESHOLD

            if mom_ok or daily_ok:
                qty = calc_qty_for_dollars(sym, PER_TRADE_DOLLARS)
                if qty > 0:
                    place_bracket_buy(sym, qty)

        time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        logging.info("Stopped by user.")
        break
    except Exception as e:
        logging.exception(f"Loop error: {e}")
        time.sleep(5)
