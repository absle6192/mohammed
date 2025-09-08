import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone

from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bot")

# =========================
# Environment / Config
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv(
    "SYMBOLS",
    "TSLA,NVDA,AAPL,MSFT,AMZN,META,GOOGL,AMD"
).split(",") if s.strip()]

# entry & risk
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.001"))  # 0.1% 1-min momentum
NOTIONAL_PER_TRADE = float(os.getenv("NOTIONAL_PER_TRADE", "6250"))   # $ per buy
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "8"))

# trailing stop
TRAIL_PCT   = float(os.getenv("TRAIL_PCT", "0.7"))   # 0.7% trailing
TRAIL_PRICE = float(os.getenv("TRAIL_PRICE", "0.0")) # 0 to disable, prefer TRAIL_PCT

# re-entry guards
NO_REENTRY_TODAY = os.getenv("NO_REENTRY_TODAY", "true").lower() == "true"
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))  # after any sell on same symbol

if not API_KEY or not API_SECRET:
    log.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Helpers (Time)
# =========================
def utc_now():
    return datetime.now(timezone.utc)

def utc_today():
    return utc_now().date()

# =========================
# Re-entry Registry
# =========================
sold_registry: Dict[str, datetime] = {}  # last sell time per symbol (UTC)

def record_today_sells(api: REST, symbols: List[str]) -> None:
    """Scan recent closed orders and record sell times (today only)."""
    try:
        closed = api.list_orders(status="closed", limit=200, direction="desc")
    except Exception as e:
        log.warning(f"list_orders failed: {e}")
        return

    for o in closed:
        try:
            if o.side != "sell" or o.symbol not in symbols:
                continue
            if not getattr(o, "filled_at", None):
                continue
            if o.filled_at.date() == utc_today():
                sold_registry[o.symbol] = o.filled_at if o.filled_at.tzinfo else o.filled_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue

def sold_today(symbol: str) -> bool:
    if not NO_REENTRY_TODAY:
        return False
    ts = sold_registry.get(symbol)
    return bool(ts and ts.date() == utc_today())

def in_cooldown(symbol: str) -> bool:
    if COOLDOWN_MINUTES <= 0:
        return False
    ts = sold_registry.get(symbol)
    return bool(ts and utc_now() < ts + timedelta(minutes=COOLDOWN_MINUTES))

# =========================
# Market / Orders helpers
# =========================
def market_open_now() -> bool:
    try:
        clock = api.get_clock()
        return bool(clock.is_open)
    except Exception:
        return True  # fail-open to keep loop alive

def last_trade_price(symbol: str) -> Optional[float]:
    try:
        quote = api.get_latest_trade(symbol)
        return float(quote.price)
    except Exception:
        return None

def has_open_position(symbol: str) -> bool:
    try:
        pos = api.get_position(symbol)
        return abs(float(pos.qty)) > 0
    except Exception:
        return False

def count_open_positions() -> int:
    try:
        return len(api.list_positions())
    except Exception:
        return 0

def open_orders_map() -> Dict[str, bool]:
    m: Dict[str, bool] = {}
    try:
        for o in api.list_orders(status="open"):
            m[o.symbol] = True
    except Exception:
        pass
    return m

def cancel_symbol_open_orders(symbol: str):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                try:
                    api.cancel_order(o.id)
                    log.info(f"Canceled open order: {symbol} ({o.side} {o.type})")
                except Exception as e:
                    log.warning(f"Cancel failed for {symbol}: {e}")
    except Exception:
        pass

# =========================
# Entry Signal (simple 1-min momentum)
# =========================
def entry_signal_for(symbol: str) -> bool:
    """
    Simple signal: last 1m bar close vs open >= MOMENTUM_THRESHOLD.
    """
    try:
        bars = api.get_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), limit=2).df
        if bars.empty:
            return False
        last = bars.iloc[-1]
        if last["open"] <= 0:
            return False
        momentum = (last["close"] - last["open"]) / last["open"]
        return momentum >= MOMENTUM_THRESHOLD
    except Exception as e:
        log.debug(f"entry_signal error {symbol}: {e}")
        return False

# =========================
# Guard: Can open new long?
# =========================
def can_open_new_long(symbol: str, open_orders: Dict[str, bool]) -> bool:
    if has_open_position(symbol):
        return False
    if open_orders.get(symbol, False):
        return False
    if sold_today(symbol):
        return False
    if in_cooldown(symbol):
        return False
    if count_open_positions() >= MAX_OPEN_POSITIONS:
        return False
    return True

# =========================
# Place Orders
# =========================
def place_market_buy(symbol: str, notional: float) -> Optional[str]:
    try:
        o = api.submit_order(
            symbol=symbol,
            side="buy",
            type="market",
            time_in_force="day",
            notional=notional
        )
        log.info(f"[BUY] {symbol} notional ${notional}")
        return o.id
    except Exception as e:
        log.error(f"BUY failed {symbol}: {e}")
        return None

def place_trailing_stop(symbol: str, qty: float) -> Optional[str]:
    try:
        if TRAIL_PRICE > 0:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_price=str(TRAIL_PRICE), qty=str(qty)
            )
        else:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_percent=str(TRAIL_PCT), qty=str(qty)
            )
        log.info(f"[TRAIL] {symbol} qty {qty}")
        return o.id
    except Exception as e:
        log.error(f"TRAIL failed {symbol}: {e}")
        return None

def try_attach_trailing_stop(symbol: str):
    """
    If we just bought using notional (fractional qty), fetch position qty
    and attach trailing stop once qty is available.
    """
    try:
        pos = api.get_position(symbol)
        qty = float(pos.qty)
        if qty > 0:
            place_trailing_stop(symbol, qty)
    except Exception as e:
        log.debug(f"attach trail skipped {symbol}: {e}")

# =========================
# Main loop
# =========================
def main_loop():
    log.info("Bot started.")
    while True:
        try:
            if not market_open_now():
                time.sleep(10)
                continue

            # refresh sell registry & open orders snapshot
            record_today_sells(api, SYMBOLS)
            open_map = open_orders_map()

            # exit management is delegated to broker via trailing stop orders
            # ensure no stale open orders before considering a new entry
            for symbol in SYMBOLS:
                # ENTRY
                if entry_signal_for(symbol) and can_open_new_long(symbol, open_map):
                    cancel_symbol_open_orders(symbol)  # safety
                    buy_id = place_market_buy(symbol, NOTIONAL_PER_TRADE)
                    if buy_id:
                        # small wait so position qty updates before placing trail
                        time.sleep(1.5)
                        try_attach_trailing_stop(symbol)

            # update sold registry frequently (to catch immediate sells)
            record_today_sells(api, SYMBOLS)

        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(3)  # loop cadence

# =========================
# Entry
# =========================
if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
