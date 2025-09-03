# bot.py
import os
import json
import time
import logging
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
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "AAPL,MSFT,AMZN,NVDA,META,TSLA,GOOGL,AMD"
).split(",") if s.strip()]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Trading Parameters (ENV)
# =========================
ENTRY_PCT        = float(os.getenv("ENTRY_PCT", "0.001"))             # 0.1% 1-min candle move
FIXED_DPT        = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "500")) # $ per trade

# Trailing stop (dynamic take-profit)
TRAIL_PCT         = float(os.getenv("TRAIL_PCT", "0.004"))            # 0.4% of entry
TRAIL_DOLLARS_MIN = float(os.getenv("TRAIL_DOLLARS_MIN", "0.20"))     # >= $0.20

# Fill waiting after market buy
FILL_RETRIES     = int(os.getenv("FILL_RETRIES", "5"))
FILL_RETRY_SECS  = float(os.getenv("FILL_RETRY_SECS", "2.0"))

HEARTBEAT_SECS   = int(os.getenv("HEARTBEAT_SECS", "60"))             # 0 disables

# One-entry-per-symbol-per-day lock
ONE_ENTRY_PER_SYMBOL_PER_DAY = os.getenv("ONE_ENTRY_PER_SYMBOL_PER_DAY", "1") == "1"
STATE_PATH = os.getenv("ONE_ENTRY_STATE_PATH", "/tmp/trade_state.json")

# =========================
# One-day lock state helpers
# =========================
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    _ny_tz = ZoneInfo("America/New_York")
except Exception:
    _ny_tz = None  # fallback handled below

def _state_load():
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _state_save(d):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(d, f)
    except Exception as e:
        logging.warning(f"[ONE] Could not persist state: {e}")

def _market_day(clock_obj):
    """
    Return market-local date string (YYYY-MM-DD) using Alpaca clock timestamp
    in America/New_York time.
    """
    try:
        ts = clock_obj.timestamp  # datetime
        if _ny_tz:
            d = ts.astimezone(_ny_tz).date()
        else:
            # Fallback: assume timestamp is UTC; NY = UTC-4/5; date may drift on DST edges
            d = (ts).date()
        return d.isoformat()
    except Exception:
        # As a last resort, use today's NY date by API call now()
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            if _ny_tz:
                return now.astimezone(_ny_tz).date().isoformat()
            return now.date().isoformat()
        except Exception:
            return "1970-01-01"

_traded_day_by_symbol = _state_load()  # {"AAPL": "2025-09-03", ...}

# =========================
# Helpers
# =========================
def tick_round(price: float) -> float:
    """Round price to a valid tick (>=$1 => 0.01, else 0.0001)."""
    if price <= 0:
        return 0.01
    tick = 0.01 if price >= 1.0 else 0.0001
    steps = round(price / tick)
    return round(steps * tick, 4)

def compute_qty(last_price: float) -> int:
    if last_price <= 0:
        return 0
    return max(1, int(FIXED_DPT // last_price))

def has_open_position(symbol: str) -> bool:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty) > 0
    except Exception:
        return False

def has_open_orders(symbol: str) -> bool:
    try:
        orders = api.list_orders(status="open", symbols=[symbol])
        return len(orders) > 0
    except Exception:
        return False

def get_last_minute_bar(symbol: str):
    """Return latest 1-min bar (handles iterator/list differences)."""
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=1, feed="sip")
        if not bars:
            return None
        try:
            return bars[0]
        except Exception:
            from itertools import islice
            return next(islice(bars, 0, 1), None)
    except Exception as e:
        logging.error(f"[BARS] Fetch error for {symbol}: {e}")
        return None

def wait_for_position(symbol: str, retries: int, sleep_secs: float):
    """Poll for a filled position after sending a market buy."""
    pos = None
    for _ in range(retries):
        time.sleep(sleep_secs)
        try:
            pos = api.get_position(symbol)
            if float(pos.qty) > 0:
                return pos
        except Exception:
            pass
    return None

# =========================
# Main
# =========================
logging.info(
    "Starting bot | feed=sip | entry_pct=%.4f | trail_pct=%.4f | trail_min=$%.2f | one_entry_per_symbol_per_day=%s"
    % (ENTRY_PCT, TRAIL_PCT, TRAIL_DOLLARS_MIN, str(ONE_ENTRY_PER_SYMBOL_PER_DAY))
)

last_heartbeat = time.time()
last_skip_print = {}
SKIP_COOLDOWN = 30  # seconds to throttle SKIP prints

while True:
    try:
        clock = api.get_clock()

        if clock.is_open:
            market_day = _market_day(clock)

            for sym in SYMBOLS:
                try:
                    # --- One-entry-per-symbol-per-day lock ---
                    if ONE_ENTRY_PER_SYMBOL_PER_DAY:
                        last_day = _traded_day_by_symbol.get(sym)
                        if last_day == market_day:
                            # Throttle prints
                            nowp = time.time()
                            if nowp - last_skip_print.get(f"ONE-{sym}", 0) >= SKIP_COOLDOWN:
                                logging.info(f"[ONE] {sym}: already traded on {market_day}. Skip.")
                                last_skip_print[f"ONE-{sym}"] = nowp
                            continue

                    # avoid duplicates on the same symbol (open pos or orders)
                    nowp = time.time()
                    if has_open_position(sym) or has_open_orders(sym):
                        if nowp - last_skip_print.get(sym, 0) >= SKIP_COOLDOWN:
                            logging.info(f"[SKIP] {sym} has open position/order.")
                            last_skip_print[sym] = nowp
                        continue

                    bar = get_last_minute_bar(sym)
                    if bar is None:
                        continue

                    o = getattr(bar, "o", None)
                    c = getattr(bar, "c", None)
                    if not o or not c:
                        continue

                    move_pct = (c - o) / o if o else 0.0
                    if move_pct < ENTRY_PCT:
                        continue

                    qty = compute_qty(c)
                    if qty <= 0:
                        logging.info(f"[SKIP] {sym} qty<=0 at price {c}")
                        continue

                    logging.info(f"[BUY] {sym} qty={qty} @ {c:.4f} (move={move_pct:.4%})")

                    # --- Market Buy ---
                    api.submit_order(
                        symbol=sym,
                        qty=qty,
                        side="buy",
                        type="market",
                        time_in_force="day"
                    )
                    logging.info(f"[BUY] Market order sent for {sym}, qty={qty}")

                    # Mark symbol as traded for this market day (lock immediately)
                    if ONE_ENTRY_PER_SYMBOL_PER_DAY:
                        _traded_day_by_symbol[sym] = market_day
                        _state_save(_traded_day_by_symbol)
                        logging.info(f"[ONE] {sym}: locked for {market_day} (one trade per symbol per day).")

                    # --- Wait for fill (position to appear) ---
                    pos = wait_for_position(sym, FILL_RETRIES, FILL_RETRY_SECS)
                    if not pos or float(pos.qty) <= 0:
                        logging.error(f"[TRAIL] No filled position for {sym}; skip trailing stop.")
                        continue

                    avg = float(pos.avg_entry_price)

                    # --- Compute trailing amount in dollars ---
                    trail_dollars = max(0.02, max(avg * TRAIL_PCT, TRAIL_DOLLARS_MIN))
                    trail_dollars = tick_round(trail_dollars)

                    # --- Place Trailing Stop (dynamic take profit) ---
                    api.submit_order(
                        symbol=sym,
                        qty=qty,
                        side="sell",
                        type="trailing_stop",
                        trail_price=trail_dollars,   # dollar offset from highest price
                        time_in_force="day"
                    )
                    logging.info(f"[TRAIL] {sym} trailing_stop trail=${trail_dollars} (avg={avg:.4f})")

                except Exception as e:
                    logging.error(f"[RTH] Scan error for {sym}: {e}")

            time.sleep(5)

        else:
            if HEARTBEAT_SECS > 0 and (time.time() - last_heartbeat) >= HEARTBEAT_SECS:
                try:
                    next_open = api.get_clock().next_open
                except Exception:
                    next_open = None
                logging.info(f"[HB] Market closed. Next open: {next_open}. Bot alive.")
                last_heartbeat = time.time()
            time.sleep(5)

    except Exception as e:
        logging.error(f"[MAIN] Loop error: {e}")
        time.sleep(10)
