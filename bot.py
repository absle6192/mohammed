# bot.py  — LITE intraday bot with dynamic trailing stop
# -----------------------------------------------
# What it does:
# - Scans a watchlist
# - Basic entry filter (daily % change, spread sanity, simple momentum/volume hints)
# - Places a bracket-like buy (with TP optional) + initial stop
# - Keeps a dynamic trailing-stop that moves up as price makes new highs
# -----------------------------------------------

import os
import time
import math
import datetime as dt
from typing import Dict, Optional

from alpaca_trade_api import REST
from alpaca_trade_api.common import URL

# ========= USER SETTINGS =========
API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
PAPER = os.getenv("APCA_PAPER", "true").lower() in ("1", "true", "yes")
BASE_URL = URL("https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets")

# Symbols to scan (edit to your list)
WATCHLIST = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","UNH","V","KO","PEP","PG","HD",
    "LLY","MRK","COST","TMO","ORCL","BAC","QCOM","IBM","INTC","CAT","GE"
]

# Position sizing
DOLLARS_PER_TRADE = 850.0     # approx per position
MAX_OPEN_POS = 6              # cap concurrent positions

# Entry filters
MIN_DAILY_PCT = 0.02          # 2% daily change threshold (if daily % is available)
MIN_1M_MOMENTUM = -0.20/100   # allow >= -0.20% over the last 1m (very light gate)
MAX_SPREAD_PCT = 0.40/100     # max spread 0.40%

# Risk/Reward
TP_PCT = 0.04                 # +4% take-profit target (set None to disable TP)
INIT_SL_PCT = 0.01            # initial stop  -1% from fill
TRAIL_FROM_PEAK_PCT = 0.01    # trailing stop distance 1% from highest seen since entry

# Engine
POLL_SEC = 3

# ========= API =========
api = REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

# ========= STATE (in-memory) =========
# Keeps entry info for dynamic trailing: { symbol: { 'qty':int, 'entry':float, 'highest':float, 'stop_id':str|None } }
BOOK: Dict[str, Dict] = {}

def log(msg: str):
    now = dt.datetime.now().strftime("%b %d %H:%M:%S")
    print(f"{now} | INFO | {msg}", flush=True)

# ------------------------------ Utilities ------------------------------

def get_latest_quote(symbol: str):
    """
    FIX: use get_latest_quote (correct modern call).
    Returns object with .ask_price, .bid_price, etc.
    """
    return api.get_latest_quote(symbol)

def get_last_trade(symbol: str):
    """
    Recent last trade (price) – used as 'current' for simpler math.
    """
    return api.get_latest_trade(symbol)

def mid_price(q) -> Optional[float]:
    if q and q.ask_price and q.bid_price and q.ask_price > 0 and q.bid_price > 0:
        return (q.ask_price + q.bid_price) / 2.0
    return None

def get_spread_pct(symbol: str) -> Optional[float]:
    """
    Spread sanity check in % of mid.
    """
    q = get_latest_quote(symbol)       # <-- FIXED here
    if not q: 
        return None
    m = mid_price(q)
    if not m or m <= 0:
        return None
    spread = (q.ask_price - q.bid_price) / m
    return spread

def get_daily_change_pct(symbol: str) -> Optional[float]:
    """
    Tries to estimate daily % change using bars (today open vs last trade).
    If data missing, returns None (and we do NOT reject on None).
    """
    try:
        today = dt.date.today()
        start = dt.datetime(today.year, today.month, today.day, 9, 30)
        bars = api.get_bars(symbol, "1Min", start.isoformat(), limit=1).df
        last_trade = get_last_trade(symbol)
        if bars is None or bars.empty or last_trade is None:
            return None
        open_price = float(bars['open'].iloc[0])
        curr = float(last_trade.price)
        if open_price <= 0:
            return None
        return (curr - open_price) / open_price
    except Exception:
        return None

def last_1m_momentum_pct(symbol: str) -> Optional[float]:
    """
    Simple 1-minute momentum: (last - prev_close_of_1m) / prev_close_of_1m
    If unavailable, return None.
    """
    try:
        now = dt.datetime.utcnow()
        start = (now - dt.timedelta(minutes=2)).isoformat()
        bars = api.get_bars(symbol, "1Min", start, limit=2).df
        if bars is None or len(bars) < 2:
            return None
        prev_close = float(bars['close'].iloc[-2])
        last_close = float(bars['close'].iloc[-1])
        if prev_close <= 0:
            return None
        return (last_close - prev_close) / prev_close
    except Exception:
        return None

# ------------------------------ Entry Logic ------------------------------

def can_enter(symbol: str) -> (bool, str):
    # Spread
    sp = get_spread_pct(symbol)
    if sp is not None and sp > MAX_SPREAD_PCT:
        return False, f"wide_spread({sp:.3%})"

    # Daily %
    day = get_daily_change_pct(symbol)
    if day is not None and day < MIN_DAILY_PCT:
        return False, f"daily_change_below({day:.2%}<{MIN_DAILY_PCT:.2%})"

    # 1m momentum
    m1 = last_1m_momentum_pct(symbol)
    if m1 is not None and m1 < MIN_1M_MOMENTUM:
        return False, f"weak_1m_momentum({m1:.2%})"

    return True, "ok"

def calc_qty(symbol: str, price: float) -> int:
    if price <= 0:
        return 0
    qty = int(DOLLARS_PER_TRADE // price)
    return max(qty, 0)

# ------------------------------ Orders ------------------------------

def place_buy_with_initial_stop(symbol: str, price_hint: float):
    qty = calc_qty(symbol, price_hint)
    if qty < 1:
        log(f"SKIP {symbol}: qty<1 (price={price_hint})")
        return

    # market buy (simpler/faster); optionally could use limit at ask
    o = api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="day"
    )
    log(f"BUY {symbol} qty={qty}")

    # fetch fill price
    time.sleep(1.0)
    pos = api.get_position(symbol)
    entry = float(pos.avg_entry_price)

    # initial stop
    stop_price = round(entry * (1.0 - INIT_SL_PCT), 2 if entry < 1000 else 3)
    stop_order = api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="stop",
        time_in_force="day",
        stop_price=stop_price
    )
    stop_id = stop_order.id

    # optional TP
    if TP_PCT is not None:
        tp_price = round(entry * (1.0 + TP_PCT), 2 if entry < 1000 else 3)
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="limit",
            time_in_force="day",
            limit_price=tp_price
        )
        log(f"TP set at {tp_price}")

    BOOK[symbol] = {
        "qty": qty,
        "entry": entry,
        "highest": entry,        # start tracking the high since entry
        "stop_id": stop_id
    }
    log(f"INIT SL for {symbol} at {stop_price}; tracking started (entry {entry})")

def update_trailing_stop(symbol: str):
    """
    Tracks the highest price seen since entry; if price makes a new high,
    lift the stop to (highest * (1 - TRAIL_FROM_PEAK_PCT)).
    """
    if symbol not in BOOK:
        return

    info = BOOK[symbol]
    qty = info["qty"]
    stop_id = info["stop_id"]
    highest = info["highest"]

    last = get_last_trade(symbol)
    if not last:
        return

    curr = float(last.price)

    # update the highest
    if curr > highest:
        info["highest"] = curr
        highest = curr

    # new trailing stop level
    desired_stop = highest * (1.0 - TRAIL_FROM_PEAK_PCT)

    # Make sure stop is below current price by the exchange minimum tick
    min_tick = 0.01 if curr < 1000 else 0.001
    desired_stop = round(min(desired_stop, curr - min_tick), 2 if curr < 1000 else 3)

    # Fetch current stop and modify only if we need to move it UP
    try:
        existing = api.get_order(stop_id) if stop_id else None
    except Exception:
        existing = None

    need_replace = True
    if existing and existing.stop_price:
        if float(existing.stop_price) >= desired_stop:
            need_replace = False

    if need_replace:
        # cancel the old stop (if any) and place a new tighter one
        try:
            if stop_id:
                api.cancel_order(stop_id)
        except Exception:
            pass

        new_stop = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            time_in_force="day",
            stop_price=desired_stop
        )
        info["stop_id"] = new_stop.id
        log(f"TRAIL {symbol}: new_stop={desired_stop:.2f} (highest={highest:.2f})")

# ------------------------------ Main Loop ------------------------------

def open_positions() -> Dict[str, float]:
    live = {}
    for p in api.list_positions():
        live[p.symbol] = float(p.qty)
    return live

def prune_book():
    """
    Remove symbols from BOOK that no longer exist in positions (flat or fully sold).
    """
    live = set(open_positions().keys())
    gone = [s for s in BOOK.keys() if s not in live]
    for s in gone:
        BOOK.pop(s, None)

def step():
    # update trailing stops for open positions
    for sym in list(BOOK.keys()):
        update_trailing_stop(sym)

    # entries, respect max slots
    live = open_positions()
    if len(live) >= MAX_OPEN_POS:
        return

    slots = MAX_OPEN_POS - len(live)

    for sym in WATCHLIST:
        if sym in live:
            continue
        ok, why = can_enter(sym)
        if not ok:
            log(f"NO_ENTRY {sym}: {why}")
            continue

        # price hint
        lt = get_last_trade(sym)
        if not lt:
            continue
        price = float(lt.price)
        place_buy_with_initial_stop(sym, price)

        slots -= 1
        if slots <= 0:
            break

def main():
    log("Starting LITE day-trading bot (daily% + spread + light momentum; dynamic trailing stop ON)...")
    # reset per “day”
    log("New trading day: Reset state.")
    while True:
        try:
            prune_book()
            step()
        except Exception as e:
            log(f"ERROR loop: {e}")
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
