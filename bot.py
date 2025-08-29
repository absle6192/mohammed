# bot.py
# Lightweight day-trading bot for Alpaca (equities)
# - Uses daily % change, 1m momentum, VWAP/5m high helpers
# - Places BRACKET order (TP/SL)
# - Upgrades stop loss dynamically (trailing behavior) as price makes new highs
# - Uses get_latest_quote() (fix for newer Alpaca API)

import os
import math
import time
import logging
from datetime import datetime, timezone
from typing import Optional, Dict

from alpaca_trade_api import REST
from alpaca_trade_api.rest import TimeFrame

# -------------------- CONFIG --------------------
API_KEY       = os.getenv("APCA_API_KEY_ID")
API_SECRET    = os.getenv("APCA_API_SECRET_KEY")
BASE_URL      = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# Universe to scan
SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN",
    "META", "TSLA", "V", "UNH", "PG", "KO", "PEP",
    "NFLX", "CRM", "HD", "MRK", "COST", "QCOM", "ORCL", "WMT", "TMO"
]

# Risk & order sizing
DOLLARS_PER_TRADE      = 900.0       # how much capital per entry
TAKE_PROFIT_PCT        = 0.04        # +4% TP
STOP_LOSS_PCT          = 0.015       # -1.5% initial SL

# Entry filters
DAILY_CHANGE_PCT_MIN   = 0.02        # require >= +2% on the day (if data available)
ONE_MIN_MOMENTUM_MIN   = 0.00        # require 1m change >= 0.0% (tweakable)
MAX_SPREAD_PCT         = 0.30        # max bid-ask spread in %
REQUIRE_PRICE_ABOVE_VWAP = False     # set True if you want above VWAP filter

# Dynamic trailing stop settings
TRAIL_START_PROFIT_PCT = 0.02        # start trailing once profit >= +2%
TRAIL_GIVEBACK_PCT     = 0.01        # move SL to (highest_seen * (1 - 1%))
TRAIL_MIN_BREAKEVEN    = True        # never trail below breakeven once active

# Loop timing
SLEEP_BETWEEN_SCANS    = 5.0         # seconds

# ------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

api = REST(API_KEY, API_SECRET, BASE_URL)

# Track per-position highest price seen (for trailing)
highest_seen: Dict[str, float] = {}

# -------------------- UTILITIES --------------------
def round_tick(price: float) -> float:
    """Equities tick size = $0.01."""
    return round(price + 1e-9, 2)

def get_latest_price(symbol: str) -> Optional[float]:
    try:
        q = api.get_latest_quote(symbol)
        # Prefer last trade price when available
        t = api.get_latest_trade(symbol)
        px = float(t.price) if t and t.price else (float(q.ask_price) if q and q.ask_price else None)
        return float(px) if px is not None else None
    except Exception as e:
        log.warning("get_latest_price fail %s: %s", symbol, e)
        return None

def get_spread_pct(symbol: str) -> Optional[float]:
    try:
        q = api.get_latest_quote(symbol)
        if not q or not q.ask_price or not q.bid_price:
            return None
        ask = float(q.ask_price); bid = float(q.bid_price)
        mid = (ask + bid) / 2.0
        if mid <= 0:
            return None
        return (ask - bid) / mid * 100.0
    except Exception as e:
        log.warning("spread fail %s: %s", symbol, e)
        return None

def get_daily_change_pct(symbol: str) -> Optional[float]:
    """Return today's % change vs previous close. If not available, return None (do NOT block)."""
    try:
        bar = api.get_bars(symbol, TimeFrame.Day, limit=2).df
        if bar is None or len(bar) < 2:
            return None
        prev_close = float(bar["close"].iloc[-2])
        curr      = float(bar["close"].iloc[-1])
        if prev_close <= 0:
            return None
        return (curr - prev_close) / prev_close * 100.0
    except Exception as e:
        log.warning("daily change fail %s: %s", symbol, e)
        return None

def get_1m_change_pct(symbol: str) -> Optional[float]:
    """Return last 1-minute % change. If not available, return None."""
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=2).df
        if bars is None or len(bars) < 2:
            return None
        prev = float(bars["close"].iloc[-2])
        curr = float(bars["close"].iloc[-1])
        if prev <= 0:
            return None
        return (curr - prev) / prev * 100.0
    except Exception as e:
        log.warning("1m change fail %s: %s", symbol, e)
        return None

def get_vwap(symbol: str) -> Optional[float]:
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=30).df
        if bars is None or len(bars) == 0:
            return None
        v = (bars["vwap"].dropna()).iloc[-1] if "vwap" in bars else None
        return float(v) if v and v > 0 else None
    except Exception as e:
        log.warning("vwap fail %s: %s", symbol, e)
        return None

def can_enter(symbol: str) -> (bool, str):
    """Returns (ok, reason)."""
    # Spread filter
    sp = get_spread_pct(symbol)
    if sp is not None and sp > MAX_SPREAD_PCT:
        return False, f"spread {sp:.2f}% > {MAX_SPREAD_PCT:.2f}%"

    # Daily change filter (only if we have the value)
    day = get_daily_change_pct(symbol)
    if day is not None and day < (DAILY_CHANGE_PCT_MIN * 100.0):
        return False, f"daily {day:.2f}% < {DAILY_CHANGE_PCT_MIN*100:.2f}%"

    # 1m momentum filter (only if we have the value)
    m1 = get_1m_change_pct(symbol)
    if m1 is not None and m1 < (ONE_MIN_MOMENTUM_MIN * 100.0):
        return False, f"1m {m1:.2f}% < {ONE_MIN_MOMENTUM_MIN*100:.2f}%"

    # VWAP filter (optional)
    if REQUIRE_PRICE_ABOVE_VWAP:
        last = get_latest_price(symbol)
        vwap = get_vwap(symbol)
        if last and vwap and last < vwap:
            return False, "below VWAP"

    return True, "ok"

def calc_qty(symbol: str, price: float) -> int:
    if price <= 0:
        return 0
    qty = int(DOLLARS_PER_TRADE // price)
    return max(qty, 1)

def clip_initial_levels(price: float) -> (float, float):
    tp = round_tick(price * (1.0 + TAKE_PROFIT_PCT))
    sl = round_tick(price * (1.0 - STOP_LOSS_PCT))
    # Ensure exchange constraint: TP >= price+0.01, SL <= price-0.01
    tp = max(tp, round_tick(price + 0.01))
    sl = min(sl, round_tick(price - 0.01))
    return tp, sl

def place_bracket_buy(symbol: str, price_hint: Optional[float] = None):
    last = get_latest_price(symbol) if price_hint is None else price_hint
    if not last or last <= 0:
        log.info("SKIP %s: no price", symbol); return
    qty = calc_qty(symbol, last)
    if qty < 1:
        log.info("SKIP %s: qty<1 (price=%.2f)", symbol, last); return

    tp, sl = clip_initial_levels(last)

    log.info("BUY %s qty=%d @~%.2f  TP=%.2f  SL=%.2f", symbol, qty, last, tp, sl)
    try:
        api.submit_order(
            symbol=symbol,
            qty=str(qty),
            side="buy",
            type="market",
            time_in_force="day",
            order_class="bracket",
            take_profit={"limit_price": str(tp)},
            stop_loss={"stop_price": str(sl)},
        )
    except Exception as e:
        log.error("submit_order failed for %s: %s", symbol, e)

def find_open_stop_child(symbol: str):
    """Find the open stop-loss child order for a symbol (from bracket)."""
    try:
        orders = api.list_orders(status="open", nested=True)
        for o in orders:
            if o.symbol != symbol:
                continue
            # stop orders have type 'stop' or 'stop_limit' and side='sell'
            if getattr(o, "type", "") in ("stop", "stop_limit") and getattr(o, "side", "") == "sell":
                return o
    except Exception as e:
        log.warning("find_open_stop_child fail %s: %s", symbol, e)
    return None

def update_trailing_stop(symbol: str, entry_price: float, current_price: float):
    """
    If profit >= TRAIL_START_PROFIT_PCT:
      move SL up to max(breakeven, highest*(1-TRAIL_GIVEBACK_PCT))
    """
    if current_price <= 0 or entry_price <= 0:
        return

    # Track highest
    hi = highest_seen.get(symbol, entry_price)
    if current_price > hi:
        hi = current_price
        highest_seen[symbol] = hi

    up_pct = (current_price - entry_price) / entry_price
    if up_pct < TRAIL_START_PROFIT_PCT:
        return  # trailing not armed yet

    # Desired new stop
    desired = hi * (1.0 - TRAIL_GIVEBACK_PCT)
    if TRAIL_MIN_BREAKEVEN:
        desired = max(desired, entry_price)

    desired = round_tick(desired)

    # Exchange constraint must hold vs current price
    max_allowed = round_tick(current_price - 0.01)
    if desired > max_allowed:
        desired = max_allowed

    stop_child = find_open_stop_child(symbol)
    if not stop_child:
        # No stop to update (could happen if it filled or was canceled); skip
        return

    try:
        old = float(getattr(stop_child, "stop_price", 0) or 0)
        if desired > old + 0.009:  # only lift if meaningfully higher
            api.replace_order(
                stop_child.id,
                stop_price=str(desired),
            )
            log.info("TRAIL %s: stop raised from %.2f -> %.2f (hi=%.2f)",
                     symbol, old, desired, hi)
    except Exception as e:
        log.warning("replace stop failed %s: %s", symbol, e)

# -------------------- MAIN LOOP --------------------
def scan_and_trade():
    # Manage existing positions (update trailing stops)
    try:
        positions = api.list_positions()
        for p in positions:
            if p.side != "long":
                continue
            symbol = p.symbol
            entry  = float(p.avg_entry_price)
            last   = get_latest_price(symbol)
            if last:
                update_trailing_stop(symbol, entry, last)
    except Exception as e:
        log.warning("positions/trailing fail: %s", e)

    # Try new entries
    for sym in SYMBOLS:
        ok, why = can_enter(sym)
        if not ok:
            log.info("NO_ENTRY %s: %s", sym, why)
            continue
        # Avoid duplicate entry if we already hold it
        try:
            pos = api.get_position(sym)
            if pos and float(pos.qty) > 0:
                log.info("HOLD %s: already in position", sym)
                continue
        except Exception:
            pass  # no position -> exception, fine

        place_bracket_buy(sym)

def main():
    log.info("Starting bot (daily%%>=%.2f, 1m%%>=%.2f, spread<=%.2f, TP=%.1f%%, SL=%.1f%%, trailing start=%.1f%%, giveback=%.1f%%)...",
             DAILY_CHANGE_PCT_MIN*100, ONE_MIN_MOMENTUM_MIN*100, MAX_SPREAD_PCT,
             TAKE_PROFIT_PCT*100, STOP_LOSS_PCT*100,
             TRAIL_START_PROFIT_PCT*100, TRAIL_GIVEBACK_PCT*100)
    while True:
        try:
            scan_and_trade()
        except Exception as e:
            log.error("Loop error: %s", e)
        time.sleep(SLEEP_BETWEEN_SCANS)

if __name__ == "__main__":
    main()
