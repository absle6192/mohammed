import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Set
from datetime import datetime, timedelta, timezone

from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

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
DATA_FEED  = os.getenv("APCA_API_DATA_FEED", "sip").strip().lower()  # use 'sip' for best coverage
SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,AMZN,NVDA,AMD,TSLA,META,GOOGL").split(",") if s.strip()]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment. Set APCA_API_KEY_ID / APCA_API_SECRET_KEY.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

# =========================
# CONFIG (INLINE)
# =========================
TOTAL_CAPITAL: float = float(os.getenv("TOTAL_CAPITAL", "50000"))
NUM_SLOTS: int       = int(os.getenv("NUM_SLOTS", "8"))           # concurrent positions
PER_TRADE_DOLLARS: float = math.floor(TOTAL_CAPITAL / NUM_SLOTS)

LOOKBACK_MIN           = 1        # momentum lookback (minutes)
MOMENTUM_THRESHOLD     = 0.0005   # +0.05% (reduced from 0.10%) – entry threshold on 1m change
TAKE_PROFIT_PCT        = 0.012    # +1.2%
STOP_LOSS_PCT          = 0.010    # -1.0%
USE_TRAILING_STOP      = True
TRAIL_PCT              = 0.006    # 0.6% trailing stop if enabled

VOLUME_1M              = 100000   # min 1-minute volume (reduced)
MAX_SPREAD_PCT         = 0.006    # max spread 0.60%

MINUTES_BEFORE_CLOSE   = 10       # don't open new positions this many minutes before close
MAX_HOLD_HOURS         = 6        # safety: liquidate very old intraday positions (if any)

SLEEP_SECONDS          = 5.0      # polling interval
CLOCK_SLOWDOWN_ON_ERR  = 2.0

# =========================
# Day state
# =========================
entered_today: Set[str] = set()        # symbols we opened today (to avoid re-entry after exit)
exited_today: Set[str]  = set()        # symbols we closed today
positions_today: Set[str] = set()      # symbols currently open today

# =========================
# Helpers
# =========================
def us_now() -> datetime:
    return datetime.now(timezone.utc)

def get_clock():
    try:
        return api.get_clock()
    except Exception as e:
        logging.warning(f"clock fetch failed: {e}")
        return None

def minutes_to_close(clock) -> Optional[int]:
    try:
        if not clock or not clock.is_open:
            return None
        remaining = clock.next_close - us_now()
        return max(0, int(remaining.total_seconds() // 60))
    except Exception as e:
        logging.warning(f"minutes_to_close err: {e}")
        return None

def get_spread_pct(sym: str) -> Optional[float]:
    try:
        quote = api.get_latest_quote(sym, feed=DATA_FEED)
        if not quote or quote.ask_price <= 0 or quote.bid_price <= 0:
            return None
        mid = (quote.ask_price + quote.bid_price) / 2.0
        spread = (quote.ask_price - quote.bid_price) / mid
        return max(0.0, spread)
    except Exception as e:
        logging.debug(f"quote err {sym}: {e}")
        return None

def get_1m_momentum_and_volume(sym: str) -> Optional[Dict[str, float]]:
    """Return last 2 bars to compute 1m momentum and last bar volume."""
    try:
        end   = us_now()
        start = end - timedelta(minutes=LOOKBACK_MIN + 2)
        bars = api.get_bars(
            sym,
            TimeFrame(1, TimeFrameUnit.Minute),
            start.isoformat(),
            end.isoformat(),
            adjustment="raw",
            feed=DATA_FEED,
            limit=3
        )
        bars = list(bars)
        if len(bars) < 2:
            return None
        last = bars[-1]
        prev = bars[-2]
        momentum = (last.c - prev.c) / prev.c if prev.c > 0 else 0.0
        return {"momentum": momentum, "vol1m": float(last.v), "last_close": last.c}
    except Exception as e:
        logging.debug(f"bars err {sym}: {e}")
        return None

def already_in_position(sym: str) -> bool:
    try:
        pos = api.get_position(sym)
        return float(pos.qty) != 0
    except Exception:
        return False

def place_buy(sym: str, dollars: float):
    # use notional to buy
    try:
        api.submit_order(
            symbol=sym,
            notional=str(dollars),
            side="buy",
            type="market",
            time_in_force="day"
        )
        logging.info(f"BUY sent: {sym} notional ~${dollars:,.0f}")
    except Exception as e:
        logging.error(f"BUY failed {sym}: {e}")

def place_tp_sl_or_trailing(sym: str, entry_price: float):
    try:
        if USE_TRAILING_STOP:
            # trailing stop only (exchange OCO notional not supported directly, use separate order)
            api.submit_order(
                symbol=sym,
                side="sell",
                type="trailing_stop",
                time_in_force="day",
                trail_percent=str(TRAIL_PCT * 100.0)
            )
            logging.info(f"TRAIL set {sym} at {TRAIL_PCT:.2%}")
        else:
            take_profit = round(entry_price * (1.0 + TAKE_PROFIT_PCT), 2)
            stop_loss   = round(entry_price * (1.0 - STOP_LOSS_PCT), 2)
            api.submit_order(
                symbol=sym,
                side="sell",
                type="limit",
                limit_price=str(take_profit),
                time_in_force="day"
            )
            api.submit_order(
                symbol=sym,
                side="sell",
                type="stop",
                stop_price=str(stop_loss),
                time_in_force="day"
            )
            logging.info(f"TP/SL set {sym} TP={take_profit} SL={stop_loss}")
    except Exception as e:
        logging.error(f"protective orders failed {sym}: {e}")

def close_all_positions(reason: str):
    try:
        api.close_all_positions(cancel_orders=True)
        logging.info(f"Flattened all positions: {reason}")
    except Exception as e:
        logging.error(f"close_all_positions err: {e}")

def refresh_today_sets():
    """Clear daily locks at start of a new trading day."""
    global entered_today, exited_today, positions_today
    entered_today.clear()
    exited_today.clear()
    positions_today.clear()
    logging.info("New trading day -> cleared locks & markers.")

# =========================
# Boot message
# =========================
logging.info(f"Bot started | CAPITAL={TOTAL_CAPITAL} | SLOTS={NUM_SLOTS} | PER_TRADE={PER_TRADE_DOLLARS} | FEED={DATA_FEED}")
logging.info(f"MOMENTUM_THRESHOLD in use = {MOMENTUM_THRESHOLD:.4f} ({MOMENTUM_THRESHOLD:.2%})")

last_calendar_day = None

# =========================
# Main loop
# =========================
while True:
    try:
        clock = get_clock()
        if not clock:
            time.sleep(SLEEP_SECONDS * CLOCK_SLOWDOWN_ON_ERR)
            continue

        # Reset daily state if we rolled into a new trading day
        today_date = datetime.now(timezone.utc).date()
        if last_calendar_day != today_date:
            last_calendar_day = today_date
            refresh_today_sets()

        if not clock.is_open:
            logging.info("Market closed. Sleeping…")
            time.sleep(max(30.0, SLEEP_SECONDS * 6))
            continue

        mins_to_close = minutes_to_close(clock) or 0

        # Track current positions list
        try:
            open_positions = api.list_positions()
            positions_today = {p.symbol for p in open_positions}
        except Exception as e:
            logging.debug(f"list_positions err: {e}")
            open_positions = []

        # Don’t open new trades close to the bell
        allow_new = mins_to_close > MINUTES_BEFORE_CLOSE

        # Iterate watchlist
        for sym in SYMBOLS:
            if sym in positions_today:
                continue  # already in position
            if sym in entered_today:
                continue  # day lock: don't re-enter same day after exit
            if not allow_new:
                continue

            info = get_1m_momentum_and_volume(sym)
            spread = get_spread_pct(sym)

            if info is None or spread is None:
                logging.debug(f"SKIP {sym} | missing data")
                continue

            mom   = info["momentum"]
            vol1m = info["vol1m"]

            # Diagnostic log to know why we skipped/entered
            logging.info(
                f"CHK {sym} | mom={mom:.2%} need>{MOMENTUM_THRESHOLD:.2%} | "
                f"vol1m={int(vol1m):,} need>={VOLUME_1M:,} | spread={spread:.2%} max<{MAX_SPREAD_PCT:.2%}"
            )

            # Entry filters
            if mom < MOMENTUM_THRESHOLD:
                continue
            if vol1m < VOLUME_1M:
                continue
            if spread > MAX_SPREAD_PCT:
                continue

            # Position sizing & submit buy
            dollars = PER_TRADE_DOLLARS
            place_buy(sym, dollars)
            entered_today.add(sym)

            # Fetch fill/avg price to set protection (best-effort)
            time.sleep(1.0)
            try:
                # find the most recent buy order for the symbol
                orders = api.list_orders(status="all", limit=50, nested=False)
                fills = [o for o in orders if o.symbol == sym and o.side == "buy"]
                fills.sort(key=lambda o: o.submitted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                if fills and fills[0].filled_avg_price:
                    entry_price = float(fills[0].filled_avg_price)
                else:
                    # fallback: use last close from bars
                    entry_price = float(info["last_close"])
            except Exception:
                entry_price = float(info["last_close"])
            place_tp_sl_or_trailing(sym, entry_price)

        # Housekeeping: close any super-old intraday positions (safety)
        try:
            for p in open_positions:
                # if position open too long intraday and near the bell, flatten
                if mins_to_close <= MINUTES_BEFORE_CLOSE:
                    api.close_position(p.symbol, cancel_orders=True)
                    exited_today.add(p.symbol)
                    logging.info(f"Flatten near close: {p.symbol}")
        except Exception as e:
            logging.debug(f"housekeeping err: {e}")

        time.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        logging.info("Interrupted — shutting down.")
        break
    except Exception as e:
        logging.error(f"Main loop error: {e}")
        time.sleep(SLEEP_SECONDS * CLOCK_SLOWDOWN_ON_ERR)
