# bot.py
import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from alpaca_trade_api.rest import REST, TimeFrame

# ============== Logging ==============
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ============== API & ENV ==============
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

# ============== Parameters ==============
ENTRY_PCT          = float(os.getenv("ENTRY_PCT", "0.001"))         # 0.1% 1-min move
TRAIL_PCT          = float(os.getenv("TRAIL_PCT", "0.004"))         # 0.4% of avg entry
TRAIL_DOLLARS_MIN  = float(os.getenv("TRAIL_DOLLARS_MIN", "0.20"))  # min $ for trailing
STOP_LOSS_DOLLARS  = float(os.getenv("STOP_LOSS_DOLLARS", "0.60"))  # fixed $ stop from avg
FILL_RETRIES       = int(os.getenv("FILL_RETRIES", "8"))
FILL_RETRY_SECS    = float(os.getenv("FILL_RETRY_SECS", "1.5"))
HEARTBEAT_SECS     = int(os.getenv("HEARTBEAT_SECS", "60"))

# Cash split
MAX_SLOTS          = int(os.getenv("MAX_SLOTS", "4"))
CASH_BUFFER        = float(os.getenv("CASH_BUFFER", "5.0"))

# One-entry-per-symbol-per-day lock
ONE_ENTRY_PER_SYMBOL_PER_DAY = os.getenv("ONE_ENTRY_PER_SYMBOL_PER_DAY", "1") == "1"
STATE_PATH         = os.getenv("ONE_ENTRY_STATE_PATH", "/tmp/trade_state.json")

VERBOSE            = os.getenv("VERBOSE", "1") == "1"

# ============== Market TZ helpers ==============
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    NY_TZ = None

def ny_now():
    now_utc = datetime.now(timezone.utc)
    return now_utc.astimezone(NY_TZ) if NY_TZ else now_utc

def ny_market_day(dt: datetime) -> str:
    return dt.astimezone(NY_TZ).date().isoformat() if NY_TZ else dt.date().isoformat()

def ny_day_bounds(day_str: str):
    if NY_TZ:
        d = datetime.fromisoformat(day_str)
        start_local = datetime.combine(d, datetime.min.time()).replace(tzinfo=NY_TZ)
        end_local   = datetime.combine(d, datetime.max.time()).replace(tzinfo=NY_TZ)
        return start_local.astimezone(timezone.utc).isoformat(), end_local.astimezone(timezone.utc).isoformat()
    else:
        start_utc = datetime.fromisoformat(day_str).replace(tzinfo=timezone.utc)
        end_utc   = start_utc + timedelta(days=1) - timedelta(microseconds=1)
        return start_utc.isoformat(), end_utc.isoformat()

# ============== Persistent daily locks ==============
def state_load():
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def state_save(d: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(d, f)
    except Exception as e:
        logging.warning(f"[ONE] Could not persist state: {e}")

# { "YYYY-MM-DD": {"AAPL": true, ... } }
TRADED_MAP = state_load()

def is_locked_today(symbol: str, day: str) -> bool:
    return bool(TRADED_MAP.get(day, {}).get(symbol, False))

def lock_symbol_today(symbol: str, day: str):
    if day not in TRADED_MAP:
        TRADED_MAP[day] = {}
    TRADED_MAP[day][symbol] = True
    state_save(TRADED_MAP)

def sync_locks_from_orders(day: str):
    """Lock symbols that already had BUY today (covers manual sells/restarts)."""
    try:
        start_iso, end_iso = ny_day_bounds(day)
        orders = api.list_orders(status="all", after=start_iso, until=end_iso, nested=False)
        added = 0
        for o in orders:
            try:
                if str(o.side).lower() == "buy":
                    sym = str(o.symbol).upper()
                    if not is_locked_today(sym, day):
                        lock_symbol_today(sym, day)
                        added += 1
            except Exception:
                pass
        if added and VERBOSE:
            logging.info(f"[ONE][SYNC] Locked {added} symbols from today's orders.")
    except Exception as e:
        logging.warning(f"[ONE][SYNC] Could not sync locks from orders: {e}")

# ============== Helpers ==============
def tick_round(price: float) -> float:
    if price <= 0:
        return 0.01
    tick = 0.01 if price >= 1.0 else 0.0001
    steps = round(price / tick)
    return round(steps * tick, 4)

def available_cash() -> float:
    try:
        acc = api.get_account()
        return max(0.0, float(acc.cash))
    except Exception as e:
        logging.error(f"[ACCT] Could not fetch account cash: {e}")
        return 0.0

def count_open_positions() -> int:
    try:
        positions = api.list_positions()
        return sum(1 for p in positions if float(p.qty) > 0)
    except Exception:
        return 0

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

def compute_slot_qty(price: float) -> int:
    if price <= 0:
        return 0
    cash = max(0.0, available_cash() - CASH_BUFFER)
    open_pos = count_open_positions()
    slots_left = max(1, MAX_SLOTS - open_pos)
    alloc_cash = cash / slots_left
    qty = int(alloc_cash // price)
    return max(0, qty)

def get_last_minute_bar(symbol: str):
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

def wait_for_stable_position(symbol: str, retries: int, sleep_secs: float):
    """Wait until position exists and qty is stable across two consecutive reads."""
    last_qty = None
    pos = None
    for _ in range(retries):
        time.sleep(sleep_secs)
        try:
            pos = api.get_position(symbol)
            qty = int(float(pos.qty))
            if qty > 0:
                if last_qty is not None and qty == last_qty:
                    return pos  # stable
                last_qty = qty
        except Exception:
            pass
    return pos  # return whatever we have (may be None)

def place_exits(symbol: str, avg: float, qty: int):
    """Place both Trailing Stop and fixed Stop orders for the same qty."""
    # trailing amount in $
    trail_dollars = max(0.02, max(avg * TRAIL_PCT, TRAIL_DOLLARS_MIN))
    trail_dollars = tick_round(trail_dollars)
    # fixed stop price in $
    stop_price = tick_round(max(0.01, avg - STOP_LOSS_DOLLARS))

    # Trailing Stop
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="trailing_stop",
            trail_price=trail_dollars,
            time_in_force="day",
        )
        logging.info(f"[EXIT] {symbol} trailing_stop trail=${trail_dollars} (avg={avg:.4f}) | qty={qty}")
    except Exception as e:
        logging.error(f"[EXIT] Trailing stop error for {symbol}: {e}")

    # Fixed Stop
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            stop_price=stop_price,
            time_in_force="day",
        )
        logging.info(f"[EXIT] {symbol} stop loss @ ${stop_price} | qty={qty}")
    except Exception as e:
        logging.error(f"[EXIT] Fixed stop error for {symbol}: {e}")

# ============== Main Loop ==============
logging.info(
    "Starting bot | entry_pct=%.4f | trail_pct=%.4f | trail_min=$%.2f | stop_loss=$%.2f | slots=%d | cash_buffer=$%.2f | one_entry_per_symbol_per_day=%s"
    % (ENTRY_PCT, TRAIL_PCT, TRAIL_DOLLARS_MIN, STOP_LOSS_DOLLARS, MAX_SLOTS, CASH_BUFFER, str(ONE_ENTRY_PER_SYMBOL_PER_DAY))
)

last_heartbeat = time.time()
last_skip_print = {}
SKIP_COOLDOWN = 30

while True:
    try:
        clock = api.get_clock()
        now_ny = ny_now()
        market_day = ny_market_day(now_ny)

        if ONE_ENTRY_PER_SYMBOL_PER_DAY:
            sync_locks_from_orders(market_day)

        if clock.is_open:
            if count_open_positions() >= MAX_SLOTS:
                if time.time() - last_skip_print.get("MAXSLOTS", 0) >= SKIP_COOLDOWN:
                    logging.info(f"[SLOTS] Reached MAX_SLOTS={MAX_SLOTS}. No new entries.")
                    last_skip_print["MAXSLOTS"] = time.time()
                time.sleep(5)
                continue

            for sym in SYMBOLS:
                try:
                    if ONE_ENTRY_PER_SYMBOL_PER_DAY and is_locked_today(sym, market_day):
                        if VERBOSE:
                            logging.info(f"[ONE] {sym} locked for {market_day}.")
                        continue

                    nowp = time.time()
                    if has_open_position(sym) or has_open_orders(sym):
                        if nowp - last_skip_print.get(sym, 0) >= SKIP_COOLDOWN:
                            logging.info(f"[SKIP] {sym} has open position/order.")
                            last_skip_print[sym] = nowp
                        continue

                    if count_open_positions() >= MAX_SLOTS:
                        break

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

                    qty = compute_slot_qty(c)
                    if qty <= 0:
                        if nowp - last_skip_print.get(f"CASH-{sym}", 0) >= SKIP_COOLDOWN:
                            logging.info(f"[SKIP] {sym} qty<=0 @ {c:.4f} | cash=${available_cash():.2f}")
                            last_skip_print[f"CASH-{sym}"] = nowp
                        continue

                    logging.info(f"[BUY] {sym} qty={qty} @ {c:.4f} (move={move_pct:.4%}) | slots={count_open_positions()}/{MAX_SLOTS}")
                    api.submit_order(symbol=sym, qty=qty, side="buy", type="market", time_in_force="day")
                    logging.info(f"[BUY] Market order sent for {sym}, qty={qty}")

                    if ONE_ENTRY_PER_SYMBOL_PER_DAY:
                        lock_symbol_today(sym, market_day)
                        logging.info(f"[ONE] {sym}: locked for {market_day} (one trade per symbol per day).")

                    # Wait for stable qty then place exits
                    pos = wait_for_stable_position(sym, FILL_RETRIES, FILL_RETRY_SECS)
                    if not pos or float(pos.qty) <= 0:
                        logging.error(f"[EXIT] No filled position for {sym}; skip exits.")
                        continue

                    avg = float(pos.avg_entry_price)
                    final_qty = int(float(pos.qty))
                    place_exits(sym, avg, final_qty)

                    if count_open_positions() >= MAX_SLOTS:
                        break

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
