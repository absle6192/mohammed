# bot.py
import os
import json
import time
import random
import logging
from collections import deque
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

# ============== Entry Filters (ENV) ==============
# 1) Daily trend (price vs today's open)
ENABLE_DAY_TREND = os.getenv("ENABLE_DAY_TREND", "1") == "1"
DAY_TREND_PCT    = float(os.getenv("DAY_TREND_PCT", "0.003"))   # 0.30%

# 2) 1-minute bar momentum
ENABLE_1M        = os.getenv("ENABLE_1M", "1") == "1"
ENTRY_PCT        = float(os.getenv("ENTRY_PCT", "0.001"))       # 0.10%
MIN_VOL_1M       = int(os.getenv("MIN_VOL_1M", "0"))            # 0 disables

# 3) Realtime tick (last trade vs price X seconds ago)
ENABLE_TICK      = os.getenv("ENABLE_TICK", "1") == "1"
TICK_PCT         = float(os.getenv("TICK_PCT", "0.0005"))       # 0.05%
TICK_LOOKBACK_S  = float(os.getenv("TICK_LOOKBACK_SECS", "3"))

# ============== Risk/Exit ==============
TRAIL_PCT          = float(os.getenv("TRAIL_PCT", "0.004"))         # 0.4% of avg entry
TRAIL_DOLLARS_MIN  = float(os.getenv("TRAIL_DOLLARS_MIN", "0.20"))  # >= $0.20
STOP_LOSS_DOLLARS  = float(os.getenv("STOP_LOSS_DOLLARS", "0.60"))  # fixed $ stop
FILL_RETRIES       = int(os.getenv("FILL_RETRIES", "8"))
FILL_RETRY_SECS    = float(os.getenv("FILL_RETRY_SECS", "1.5"))
HEARTBEAT_SECS     = int(os.getenv("HEARTBEAT_SECS", "60"))

# ============== Capital & Scheduling ==============
MAX_SLOTS          = int(os.getenv("MAX_SLOTS", "4"))               # max concurrent positions
CASH_BUFFER        = float(os.getenv("CASH_BUFFER", "5.0"))

# One-entry-per-symbol-per-day lock
ONE_ENTRY_PER_SYMBOL_PER_DAY = os.getenv("ONE_ENTRY_PER_SYMBOL_PER_DAY", "1") == "1"
STATE_PATH         = os.getenv("ONE_ENTRY_STATE_PATH", "/tmp/trade_state.json")

# Diagnostics
VERBOSE            = os.getenv("VERBOSE", "1") == "1"
LOCK_LOG_COOLDOWN  = int(os.getenv("LOCK_LOG_COOLDOWN", "300"))     # sec
SKIP_LOG_COOLDOWN  = int(os.getenv("SKIP_LOG_COOLDOWN", "30"))      # sec
SCAN_SLEEP_SECS    = float(os.getenv("SCAN_SLEEP_SECS", "5"))       # loop sleep

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

def get_today_open(symbol: str):
    """Return today's daily open using Day timeframe."""
    try:
        dbar = api.get_bars(symbol, TimeFrame.Day, limit=1, feed="sip")
        if not dbar:
            return None
        # same iterator/list guard
        try:
            obar = dbar[0]
        except Exception:
            from itertools import islice
            obar = next(islice(dbar, 0, 1), None)
        return getattr(obar, "o", None)
    except Exception as e:
        logging.error(f"[DAYBAR] Fetch error for {symbol}: {e}")
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
    return pos

def place_exits(symbol: str, avg: float, qty: int):
    """Place both Trailing Stop and fixed Stop orders for the same qty."""
    trail_dollars = max(0.02, max(avg * TRAIL_PCT, TRAIL_DOLLARS_MIN))
    trail_dollars = tick_round(trail_dollars)
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

# ============== Realtime tick store ==============
# For each symbol keep a small deque of (ts, price) to lookup price >= lookback seconds ago
_tick_history = {sym: deque(maxlen=120) for sym in SYMBOLS}  # ~ up to 10 minutes at 5s cadence

def update_tick_and_get_change(symbol: str):
    """
    Push last trade price into history and compute tick_move vs price >= lookback seconds ago.
    Returns (tick_move, last_price, has_baseline)
    """
    try:
        trade = api.get_last_trade(symbol)
        price = float(getattr(trade, "price", None))
        if price <= 0:
            return (0.0, None, False)
    except Exception as e:
        logging.error(f"[TICK] Fetch error for {symbol}: {e}")
        return (0.0, None, False)

    now_ts = time.time()
    dq = _tick_history[symbol]
    dq.append((now_ts, price))

    # find baseline at least TICK_LOOKBACK_S ago
    baseline_price = None
    for ts, p in reversed(dq):
        if now_ts - ts >= TICK_LOOKBACK_S:
            baseline_price = p
            break

    if baseline_price is None:
        # not enough history yet
        return (0.0, price, False)

    tick_move = (price - baseline_price) / baseline_price if baseline_price else 0.0
    return (tick_move, price, True)

# ============== Main Loop ==============
logging.info(
    "Starting bot | day_trend=%s(%.4f) | one_min=%s(%.4f, min_vol=%d) | tick=%s(%.4f/%ss) | slots=%d | cash_buffer=$%.2f | exits: trail=%.4f(min $%.2f) stop=$%.2f | one_entry=%s",
    ENABLE_DAY_TREND, DAY_TREND_PCT, ENABLE_1M, ENTRY_PCT, MIN_VOL_1M,
    ENABLE_TICK, TICK_PCT, TICK_LOOKBACK_S,
    MAX_SLOTS, CASH_BUFFER, TRAIL_PCT, TRAIL_DOLLARS_MIN, STOP_LOSS_DOLLARS, ONE_ENTRY_PER_SYMBOL_PER_DAY
)
logging.info("Symbols universe: %s", ",".join(SYMBOLS))

last_heartbeat = time.time()
last_locked_log = {}
last_skip_log = {}

while True:
    try:
        clock = api.get_clock()
        now_ny = ny_now()
        market_day = ny_market_day(now_ny)

        if ONE_ENTRY_PER_SYMBOL_PER_DAY:
            sync_locks_from_orders(market_day)

        if clock.is_open:
            # Respect global slots
            if count_open_positions() >= MAX_SLOTS:
                if time.time() - last_skip_log.get("MAXSLOTS", 0) >= SKIP_LOG_COOLDOWN:
                    logging.info(f"[SLOTS] Reached MAX_SLOTS={MAX_SLOTS}. No new entries.")
                    last_skip_log["MAXSLOTS"] = time.time()
                time.sleep(SCAN_SLEEP_SECS)
                continue

            symbols_order = SYMBOLS[:]
            random.shuffle(symbols_order)

            for sym in symbols_order:
                try:
                    # Daily lock
                    if ONE_ENTRY_PER_SYMBOL_PER_DAY and is_locked_today(sym, market_day):
                        now_ts = time.time()
                        if now_ts - last_locked_log.get(sym, 0) >= LOCK_LOG_COOLDOWN:
                            logging.info(f"[ONE] {sym} locked for {market_day}.")
                            last_locked_log[sym] = now_ts
                        continue

                    # Skip if position/order exists
                    now_ts = time.time()
                    if has_open_position(sym) or has_open_orders(sym):
                        if now_ts - last_skip_log.get(sym, 0) >= SKIP_LOG_COOLDOWN:
                            logging.info(f"[SKIP] {sym} has open position/order.")
                            last_skip_log[sym] = now_ts
                        continue

                    if count_open_positions() >= MAX_SLOTS:
                        break

                    # ===== Gather signals =====
                    one_min_move = None
                    one_min_vol  = None
                    day_move     = None
                    tick_move    = None

                    # 1m bar
                    bar = get_last_minute_bar(sym)
                    if bar:
                        o = getattr(bar, "o", None)
                        c = getattr(bar, "c", None)
                        v = getattr(bar, "v", None)
                        if o and c:
                            one_min_move = (c - o) / o
                            one_min_vol  = int(v) if v is not None else None

                    # daily open move (vs last price or 1m close)
                    today_open = get_today_open(sym) if ENABLE_DAY_TREND else None
                    last_price_for_day = None

                    # realtime price
                    tick_move_val, last_price, has_baseline = update_tick_and_get_change(sym) if ENABLE_TICK else (None, None, False)
                    if ENABLE_TICK:
                        tick_move = tick_move_val

                    if ENABLE_DAY_TREND and today_open:
                        last_price_for_day = last_price if last_price is not None else (getattr(bar, "c", None) if bar else None)
                        if last_price_for_day:
                            day_move = (last_price_for_day - today_open) / today_open

                    # ===== Evaluate filters =====
                    reasons = []
                    ok = True

                    if ENABLE_DAY_TREND:
                        if day_move is None or day_move < DAY_TREND_PCT:
                            ok = False
                            reasons.append(f"day { (day_move or 0)*100:.2f}% < {DAY_TREND_PCT*100:.2f}%")

                    if ENABLE_1M:
                        if one_min_move is None or one_min_move < ENTRY_PCT:
                            ok = False
                            reasons.append(f"1m { (one_min_move or 0)*100:.2f}% < {ENTRY_PCT*100:.2f}%")
                        if MIN_VOL_1M > 0:
                            if one_min_vol is None or one_min_vol < MIN_VOL_1M:
                                ok = False
                                reasons.append(f"vol {one_min_vol or 0} < {MIN_VOL_1M}")

                    if ENABLE_TICK:
                        if not has_baseline or tick_move is None or tick_move < TICK_PCT:
                            ok = False
                            reasons.append(f"tick { (tick_move or 0)*100:.2f}% < {TICK_PCT*100:.2f}% (lookback {TICK_LOOKBACK_S:.0f}s)")

                    if not ok:
                        if VERBOSE:
                            now_ts = time.time()
                            key = f"SCAN-{sym}"
                            if now_ts - last_skip_log.get(key, 0) >= SKIP_LOG_COOLDOWN:
                                logging.info(f"[SCAN] {sym} -> SKIP | " +
                                             ("; ".join(reasons)))
                                last_skip_log[key] = now_ts
                        continue

                    # ===== Passed filters -> compute qty and buy =====
                    price_for_qty = last_price if last_price is not None else (getattr(bar, "c", None) if bar else None)
                    if not price_for_qty or price_for_qty <= 0:
                        continue

                    qty = compute_slot_qty(price_for_qty)
                    if qty <= 0:
                        now_ts = time.time()
                        key = f"CASH-{sym}"
                        if now_ts - last_skip_log.get(key, 0) >= SKIP_LOG_COOLDOWN:
                            logging.info(f"[SKIP] {sym} qty<=0 @ {price_for_qty:.4f} | cash=${available_cash():.2f}")
                            last_skip_log[key] = now_ts
                        continue

                    logging.info(f"[BUY] {sym} qty={qty} @ ~{price_for_qty:.4f} "
                                 f"(day={ (day_move or 0)*100:.2f}% ; 1m={ (one_min_move or 0)*100:.2f}% ; tick={ (tick_move or 0)*100:.2f}% ) "
                                 f"| slots={count_open_positions()}/{MAX_SLOTS}")

                    api.submit_order(symbol=sym, qty=qty, side="buy", type="market", time_in_force="day")
                    logging.info(f"[BUY] Market order sent for {sym}, qty={qty}")

                    if ONE_ENTRY_PER_SYMBOL_PER_DAY:
                        lock_symbol_today(sym, market_day)
                        logging.info(f"[ONE] {sym}: locked for {market_day} (one trade per symbol per day).")

                    # Wait stable fill, then exits
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

            time.sleep(SCAN_SLEEP_SECS)

        else:
            if HEARTBEAT_SECS > 0 and (time.time() - last_heartbeat) >= HEARTBEAT_SECS:
                try:
                    next_open = api.get_clock().next_open
                except Exception:
                    next_open = None
                logging.info(f"[HB] Market closed. Next open: {next_open}. Bot alive.")
                last_heartbeat = time.time()
            time.sleep(SCAN_SLEEP_SECS)

    except Exception as e:
        logging.error(f"[MAIN] Loop error: {e}")
        time.sleep(10)
