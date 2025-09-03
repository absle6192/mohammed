# bot.py
import os
import time
import logging
from datetime import datetime, timezone, date
from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

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
API_KEY      = os.getenv("APCA_API_KEY_ID", "")
API_SECRET   = os.getenv("APCA_API_SECRET_KEY", "")
API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, API_BASE_URL)

# Symbols universe
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "AAPL,MSFT,AMZN,NVDA,META,TSLA,GOOGL,AMD"
).split(",") if s.strip()]

# =========================
# Behavior (ENV)
# =========================
# Entry logic mode: "OR" (any condition) or "AND" (all conditions)
ENTRY_MODE = os.getenv("ENTRY_MODE", "OR").strip().upper()  # "OR" or "AND"

# Day-trend condition (today's last vs today's open)
ENABLE_DAY_TREND = os.getenv("ENABLE_DAY_TREND", "1") == "1"
DAY_TREND_PCT    = float(os.getenv("DAY_TREND_PCT", "0.003"))  # 0.3%

# 1-minute candle condition (last close vs last open)
ENABLE_ONE_MIN   = os.getenv("ENABLE_ONE_MIN", "1") == "1"
ONE_MIN_PCT      = float(os.getenv("ONE_MIN_PCT", "0.001"))    # 0.1%

# Poll cadence (seconds)
POLL_SEC         = float(os.getenv("POLL_SEC", "3"))

# Max concurrent positions (cash split across slots)
SLOTS            = int(os.getenv("SLOTS", "1"))
CASH_BUFFER      = float(os.getenv("CASH_BUFFER", "5"))

# One entry per symbol per day
ONE_ENTRY_PER_SYMBOL_PER_DAY = os.getenv("ONE_ENTRY_PER_SYMBOL_PER_DAY", "1") == "1"

# Exits: trailing stop dollars = max(TRAIL_MIN_DOLLARS, avg_entry * TRAIL_PCT)
TRAIL_MIN_DOLLARS = float(os.getenv("TRAIL_MIN_DOLLARS", "0.20"))  # absolute floor
TRAIL_PCT         = float(os.getenv("TRAIL_PCT", "0.004"))         # 0.4%

# Heartbeat when market closed (0 disables)
HEARTBEAT_SECS    = int(os.getenv("HEARTBEAT_SECS", "0"))

# Data feed for bars
FEED = os.getenv("FEED", "sip")

# =========================
# Helpers
# =========================
def today_str() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()

def can_trade_now() -> bool:
    try:
        clock = api.get_clock()
        return bool(getattr(clock, "is_open", False))
    except Exception as e:
        logging.error(f"[CLOCK] Error: {e}")
        return False

def get_free_cash() -> float:
    try:
        acct = api.get_account()
        return float(acct.cash)
    except Exception as e:
        logging.error(f"[ACCT] Error: {e}")
        return 0.0

def compute_qty_by_slots(symbol: str, last_price: float) -> int:
    if last_price <= 0:
        return 0
    free_cash = max(0.0, get_free_cash() - CASH_BUFFER)
    alloc = free_cash if SLOTS <= 0 else (free_cash / float(SLOTS))
    qty = int(max(1, alloc // last_price))
    return qty

def bars_1m(symbol: str, n: int = 2):
    try:
        return api.get_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), limit=n, feed=FEED)
    except Exception as e:
        logging.error(f"[BARS] {symbol} 1m error: {e}")
        return []

def bar_today_open(symbol: str):
    """Return today's first minute bar (approx today's open price)."""
    try:
        bars = api.get_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), limit=400, feed=FEED)
        if not bars:
            return None
        todays = [b for b in bars if b.t.astimezone().date() == date.today()]
        return todays[0] if todays else None
    except Exception as e:
        logging.error(f"[BARS] {symbol} day open error: {e}")
        return None

def eval_conditions(symbol: str) -> tuple[bool, dict]:
    """
    Evaluate enabled entry conditions. Returns (decision, details)
    decision follows ENTRY_MODE ("OR" / "AND").
    """
    results = []
    details = {}

    # Day-trend
    if ENABLE_DAY_TREND:
        open_bar = bar_today_open(symbol)
        last_bar = bars_1m(symbol, n=1)
        if open_bar and last_bar:
            o = float(getattr(open_bar, "o", 0.0))
            c = float(getattr(last_bar[0], "c", 0.0))
            day_pct = (c - o) / o if o else 0.0
            details["day_pct"] = day_pct
            results.append(day_pct >= DAY_TREND_PCT)
        else:
            details["day_pct"] = None
            results.append(False)

    # 1-minute
    if ENABLE_ONE_MIN:
        b = bars_1m(symbol, n=1)
        if b:
            o = float(getattr(b[0], "o", 0.0))
            c = float(getattr(b[0], "c", 0.0))
            m1_pct = (c - o) / o if o else 0.0
            details["m1_pct"] = m1_pct
            results.append(m1_pct >= ONE_MIN_PCT)
        else:
            details["m1_pct"] = None
            results.append(False)

    if not results:
        return False, details

    if ENTRY_MODE == "AND":
        return all(results), details
    else:
        return any(results), details  # default OR

def place_trailing_stop(symbol: str, qty: int, avg_entry: float):
    trail_dollars = max(TRAIL_MIN_DOLLARS, float(avg_entry) * TRAIL_PCT)
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="trailing_stop",
            time_in_force="day",
            trail_price=round(trail_dollars, 4),
        )
        logging.info(f"[EXIT] Trailing stop placed for {symbol} | trail=${trail_dollars:.4f} | qty={qty}")
    except Exception as e:
        logging.error(f"[EXIT] Trailing stop error for {symbol}: {e}")

def get_open_qty(symbol: str) -> int:
    try:
        pos = api.get_position(symbol)
        return int(float(pos.qty))
    except Exception:
        return 0

def sync_filled_position(symbol: str, attempts: int = 6, wait_sec: float = 1.0):
    """Wait for position to appear after market order."""
    for _ in range(attempts):
        try:
            pos = api.get_position(symbol)
            qty = int(float(pos.qty))
            avg = float(pos.avg_entry_price)
            if qty > 0:
                return qty, avg
        except Exception:
            pass
        time.sleep(wait_sec)
    return 0, 0.0

# Track per-day locks
traded_today: dict[str, str] = {}  # symbol -> ISO date

def already_traded_today(symbol: str) -> bool:
    return traded_today.get(symbol) == today_str()

def lock_trade_today(symbol: str):
    traded_today[symbol] = today_str()
    logging.info(f"[ONE] {symbol} locked for {today_str()}.")

# =========================
# Main
# =========================
logging.info(
    "Starting bot | ENTRY_MODE=%s | day_trend=%s(%.4f) | one_min=%s(%.4f) | "
    "poll=%.1fs | slots=%d | cash_buffer=$%.2f | exits: trail=%.4f(min $%.2f) | one_entry=%s",
    ENTRY_MODE, ENABLE_DAY_TREND, DAY_TREND_PCT, ENABLE_ONE_MIN, ONE_MIN_PCT,
    POLL_SEC, SLOTS, CASH_BUFFER, TRAIL_PCT, TRAIL_MIN_DOLLARS, ONE_ENTRY_PER_SYMBOL_PER_DAY
)

last_hb = time.time()

while True:
    try:
        if not can_trade_now():
            if HEARTBEAT_SECS > 0 and (time.time() - last_hb) >= HEARTBEAT_SECS:
                logging.info("[HB] Market closed. Bot alive.")
                last_hb = time.time()
            time.sleep(5)
            continue

        # Current positions count (respect SLOTS)
        try:
            positions_count = len(api.list_positions())
        except Exception:
            positions_count = 0

        for sym in SYMBOLS:
            try:
                # one-entry-per-symbol-per-day
                if ONE_ENTRY_PER_SYMBOL_PER_DAY and already_traded_today(sym):
                    logging.info(f"[ONE] {sym}: already traded on {today_str()}. Skip.")
                    continue

                # respect slots
                try:
                    positions_count = len(api.list_positions())
                except Exception:
                    pass
                if SLOTS > 0 and positions_count >= SLOTS:
                    logging.info(f"[SLOTS] Reached MAX_SLOTS={SLOTS}. No new entries.")
                    break

                decision, det = eval_conditions(sym)
                day_s = f"{det.get('day_pct', None):.4%}" if det.get('day_pct') is not None else "n/a"
                m1_s  = f"{det.get('m1_pct', None):.4%}" if det.get('m1_pct') is not None else "n/a"

                if not decision:
                    logging.info(
                        f"[SCAN] {sym} -> SKIP | day {day_s} {'<' if det.get('day_pct') is not None else ''} {DAY_TREND_PCT:.2%}; "
                        f"1m {m1_s} {'<' if det.get('m1_pct') is not None else ''} {ONE_MIN_PCT:.2%}"
                    )
                    continue

                # Use last 1m bar close as reference price
                b = bars_1m(sym, n=1)
                if not b:
                    continue
                last_price = float(getattr(b[0], "c", 0.0))
                if last_price <= 0:
                    continue

                qty = compute_qty_by_slots(sym, last_price)
                if qty <= 0:
                    logging.info(f"[BUY] Skip {sym}: qty<=0 (price={last_price}, cash={get_free_cash():.2f})")
                    continue

                # Market BUY
                api.submit_order(
                    symbol=sym,
                    qty=qty,
                    side="buy",
                    type="market",
                    time_in_force="day",
                )
                logging.info(f"[BUY] {sym} qty={qty} @ ~{last_price:.4f} | day {day_s} | 1m {m1_s}")

                if ONE_ENTRY_PER_SYMBOL_PER_DAY:
                    lock_trade_today(sym)

                # Wait & confirm position, then place trailing stop
                time.sleep(3.0)
                filled_qty, avg_entry = sync_filled_position(sym, attempts=6, wait_sec=1.0)
                if filled_qty > 0:
                    place_trailing_stop(sym, filled_qty, avg_entry)
                else:
                    logging.warning(f"[EXIT] Skip exits for {sym}: position not found/filled after wait.")

            except Exception as e:
                logging.error(f"[LOOP] Error for {sym}: {e}")

        time.sleep(POLL_SEC)

    except Exception as e:
        logging.error(f"[MAIN] Loop error: {e}")
        time.sleep(5)
