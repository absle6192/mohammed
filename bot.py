# bot.py — RTH momentum entries + managed percent Trailing Stop (no-loss floor)
# plus "one entry per symbol per day" rule

import os
import time
import logging
from typing import Dict, Optional, Set
from datetime import date

from alpaca_trade_api.rest import REST, TimeFrame, Order

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# =========================
# API & ENV
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "AAPL,MSFT,AMZN,NVDA,TSLA,BA,NFLX"
).split(",") if s.strip()]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Strategy ENV
# =========================
ENTRY_PCT        = float(os.getenv("ENTRY_PCT", "0.003"))        # 0.3% on 1m candle (close vs open)
FIXED_DPT        = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "500"))
USE_TRAILING     = os.getenv("USE_TRAILING_STOP", "1") == "1"
TRAIL_PCT        = float(os.getenv("TRAIL_PCT", "0.006"))        # 0.6% trailing from highest
TRAIL_ARM_PCT    = float(os.getenv("TRAIL_ARM_PCT", "0.003"))    # 0.3% move in favor to arm
HEARTBEAT_SECS   = int(os.getenv("HEARTBEAT_SECS", "60"))

# Safety: ensure stop never goes below entry (no-loss guarantee)
if TRAIL_PCT > TRAIL_ARM_PCT:
    logging.info(f"Adjusting TRAIL_PCT from {TRAIL_PCT:.4f} to {TRAIL_ARM_PCT:.4f} to guarantee no-loss.")
    TRAIL_PCT = TRAIL_ARM_PCT

# =========================
# In-memory trailing state
# =========================
class TrailState:
    def __init__(self, entry: float, highest: float):
        self.entry = float(entry)
        self.highest = float(highest)
        self.armed = False               # becomes True after price >= entry*(1+TRAIL_ARM_PCT)
        self.stop_order_id: Optional[str] = None

trail_states: Dict[str, TrailState] = {}

# "Sold today" ban list: a set of symbols that cannot re-enter until next trading day
sold_today: Set[str] = set()
current_session_day: Optional[date] = None  # US market session date

# =========================
# Helpers
# =========================
def compute_qty(last_price: float) -> int:
    if last_price <= 0:
        return 0
    return int(max(1, FIXED_DPT // last_price))

def get_last_minute_bar(symbol: str):
    bars = api.get_bars(symbol, TimeFrame.Minute, limit=1, feed="sip")
    return bars[0] if bars else None

def list_open_stop_orders(symbol: str):
    try:
        orders = api.list_orders(status="open", symbols=[symbol], nested=True)
        return [o for o in orders if o.side == "sell" and o.type == "stop"]
    except Exception:
        return []

def cancel_order(order_id: str):
    try:
        api.cancel_order(order_id)
    except Exception as e:
        logging.warning(f"Cancel order failed {order_id}: {e}")

def place_or_replace_stop(symbol: str, stop_price: float, qty: int) -> Optional[str]:
    stop_price = round(float(stop_price), 4)
    if qty <= 0:
        return None
    # cancel older stops
    for o in list_open_stop_orders(symbol):
        cancel_order(o.id)
    try:
        o: Order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            time_in_force="gtc",
            stop_price=stop_price,
        )
        logging.info(f"[TRAIL] Set/Update STOP {symbol} qty={qty} stop={stop_price}")
        return o.id
    except Exception as e:
        logging.error(f"[TRAIL] submit stop failed {symbol}: {e}")
        return None

def sync_trail_state_from_positions():
    """Rebuild trail states on restart and keep 'sold_today' consistent."""
    try:
        pos_list = api.list_positions()
        open_syms = {p.symbol for p in pos_list}
        # seed trail states for open positions
        for p in pos_list:
            sym = p.symbol
            entry = float(p.avg_entry_price)
            last = float(p.current_price)
            ts = trail_states.get(sym)
            if ts is None:
                ts = TrailState(entry=entry, highest=last)
                if last >= entry * (1 + TRAIL_ARM_PCT):
                    ts.armed = True
                trail_states[sym] = ts
        # any trail state without an open position => consider it sold already
        for sym in list(trail_states.keys()):
            if sym not in open_syms:
                sold_today.add(sym)
                del trail_states[sym]
    except Exception as e:
        logging.error(f"sync_trail_state error: {e}")

def manage_trailing_for_symbol(symbol: str):
    """Maintain trailing stop with a hard floor at entry (never sell for a loss)."""
    try:
        pos = api.get_position(symbol)
        qty = int(float(pos.qty))
    except Exception:
        return  # no position

    if qty <= 0:
        return

    last = float(pos.current_price)
    entry = float(pos.avg_entry_price)

    ts = trail_states.get(symbol)
    if ts is None:
        ts = TrailState(entry=entry, highest=last)
        trail_states[symbol] = ts

    # update highest
    if last > ts.highest:
        ts.highest = last

    # arm after favorable move
    if not ts.armed and last >= ts.entry * (1 + TRAIL_ARM_PCT):
        ts.armed = True
        logging.info(f"[TRAIL] Armed {symbol} (entry={ts.entry:.4f}, last={last:.4f})")

    # not armed yet → never sell (prevents loss)
    if not ts.armed:
        return

    # compute trailing stop with no-loss floor
    stop_price = max(ts.entry, ts.highest * (1 - TRAIL_PCT))

    # check existing stop; replace only if changed meaningfully
    current_stops = list_open_stop_orders(symbol)
    current_stop_px = None
    if current_stops:
        try:
            current_stop_px = float(current_stops[0].stop_price)
        except Exception:
            current_stop_px = None

    if current_stop_px is None or abs(current_stop_px - stop_price) / stop_price > 0.0001:
        ts.stop_order_id = place_or_replace_stop(symbol, stop_price, qty)

def try_momentum_entry(symbol: str):
    # block re-entry if sold today
    if symbol in sold_today:
        return

    bar = get_last_minute_bar(symbol)
    if not bar or not getattr(bar, "o", None) or not getattr(bar, "c", None):
        return
    move_pct = (bar.c - bar.o) / bar.o if bar.o else 0.0
    if move_pct < ENTRY_PCT:
        return

    qty = compute_qty(bar.c)
    if qty <= 0:
        return

    try:
        api.submit_order(symbol=symbol, qty=qty, side="buy", type="market", time_in_force="day")
        logging.info(f"[BUY] {symbol} qty={qty} @ {bar.c:.4f} (move={move_pct:.4%})")
        # seed trail state
        try:
            pos = api.get_position(symbol)
            entry = float(pos.avg_entry_price)
            trail_states[symbol] = TrailState(entry=entry, highest=float(pos.current_price))
        except Exception:
            pass
    except Exception as e:
        logging.error(f"[BUY] Submit error for {symbol}: {e}")

def detect_session_day() -> Optional[date]:
    """
    Determine the current US session day using Alpaca clock:
    - If market open: use next_close date (today's session).
    - If market closed: use next_open date (upcoming session).
    """
    try:
        clk = api.get_clock()
        if clk.is_open:
            return clk.next_close.date()
        return clk.next_open.date()
    except Exception:
        return None

def rollover_if_new_day():
    global current_session_day
    session_day = detect_session_day()
    if session_day is None:
        return
    if current_session_day is None:
        current_session_day = session_day
        return
    if session_day != current_session_day:
        logging.info(f"[DAY] New session detected ({current_session_day} → {session_day}). Clearing bans/state.")
        current_session_day = session_day
        sold_today.clear()
        # do not clear trail_states for open positions; resync to be safe
        sync_trail_state_from_positions()

def refresh_bans_from_positions():
    """Mark symbols as sold today when their positions disappear (stop filled or manual exit)."""
    try:
        open_syms = {p.symbol for p in api.list_positions()}
        # anything we were trailing but is no longer open -> sold today
        for sym in list(trail_states.keys()):
            if sym not in open_syms:
                sold_today.add(sym)
                del trail_states[sym]
    except Exception as e:
        logging.error(f"refresh_bans_from_positions error: {e}")

# =========================
# Main
# =========================
logging.info(
    "Starting bot | feed=sip | entry_pct=%.4f | use_trailing=%s | trail_pct=%.4f | arm=%.4f | one-entry-per-day=ON"
    % (ENTRY_PCT, USE_TRAILING, TRAIL_PCT, TRAIL_ARM_PCT)
)

last_hb = time.time()
sync_trail_state_from_positions()
current_session_day = detect_session_day()

while True:
    try:
        rollover_if_new_day()

        clk = api.get_clock()
        if clk.is_open:
            # entries
            for sym in SYMBOLS:
                try_momentum_entry(sym)

            # trailing management
            if USE_TRAILING:
                for sym in list(trail_states.keys()):
                    manage_trailing_for_symbol(sym)

            # after managing, see if any position got closed → ban re-entry for today
            refresh_bans_from_positions()

            time.sleep(3)
        else:
            if HEARTBEAT_SECS > 0 and (time.time() - last_hb) >= HEARTBEAT_SECS:
                try:
                    nxt = api.get_clock().next_open
                except Exception:
                    nxt = None
                logging.info(f"[HB] Market closed. Next open: {nxt}. Bot alive.")
                last_hb = time.time()
            time.sleep(5)

    except Exception as e:
        logging.error(f"[MAIN] Loop error: {e}")
        time.sleep(10)
