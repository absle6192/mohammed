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
DATA_FEED  = os.getenv("APCA_API_DATA_FEED", "sip").strip().lower()
SYMBOLS: List[str] = [
    s.strip().upper() for s in os.getenv(
        "SYMBOLS",
        "AAPL,MSFT,AMZN,NVDA,AMD,TSLA,META,GOOGL"
    ).split(",") if s.strip()
]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys. Set APCA_API_KEY_ID / APCA_API_SECRET_KEY.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

# =========================
# CONFIG
# =========================
TOTAL_CAPITAL: float   = float(os.getenv("TOTAL_CAPITAL", "50000"))
NUM_SLOTS: int         = int(os.getenv("NUM_SLOTS", "8"))
PER_TRADE_DOLLARS: int = math.floor(TOTAL_CAPITAL / NUM_SLOTS)

LOOKBACK_MIN           = 1
MOMENTUM_THRESHOLD     = 0.0005   # +0.05% entry
TAKE_PROFIT_PCT        = 0.012    # +1.2% (used if trailing disabled)
STOP_LOSS_PCT          = 0.010    # -1.0%
USE_TRAILING_STOP      = True
TRAIL_PCT              = 0.006    # 0.6% trailing

VOLUME_1M              = 100000   # min 1m volume
MAX_SPREAD_PCT         = 0.006    # 0.60% max spread

MINUTES_BEFORE_CLOSE   = 10
MAX_HOLD_HOURS         = 6

SLEEP_SECONDS          = 5.0
CLOCK_SLOWDOWN_ON_ERR  = 2.0

# =========================
# Day state
# =========================
entered_today: Set[str]   = set()   # symbols bought today (prevent re-entry)
positions_today: Set[str] = set()   # currently open

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
        q = api.get_latest_quote(sym, feed=DATA_FEED)
        if not q or q.ask_price <= 0 or q.bid_price <= 0:
            return None
        mid = (q.ask_price + q.bid_price) / 2.0
        return max(0.0, (q.ask_price - q.bid_price) / mid)
    except Exception as e:
        logging.debug(f"quote err {sym}: {e}")
        return None

def get_1m_momentum_and_volume(sym: str) -> Optional[Dict[str, float]]:
    try:
        end   = us_now()
        start = end - timedelta(minutes=LOOKBACK_MIN + 2)
        bars  = list(api.get_bars(
            sym,
            TimeFrame(1, TimeFrameUnit.Minute),
            start.isoformat(),
            end.isoformat(),
            adjustment="raw",
            feed=DATA_FEED,
            limit=3
        ))
        if len(bars) < 2:
            return None
        last, prev = bars[-1], bars[-2]
        momentum = (last.c - prev.c) / prev.c if prev.c > 0 else 0.0
        return {"momentum": momentum, "vol1m": float(last.v), "last_close": float(last.c)}
    except Exception as e:
        logging.debug(f"bars err {sym}: {e}")
        return None

def list_open_positions() -> List:
    try:
        return api.list_positions()
    except Exception as e:
        logging.debug(f"list_positions err: {e}")
        return []

def place_buy_notional(sym: str, dollars: float) -> Optional[str]:
    """Place market BUY by notional. Returns order id or None."""
    try:
        o = api.submit_order(
            symbol=sym,
            notional=str(dollars),
            side="buy",
            type="market",
            time_in_force="day"
        )
        logging.info(f"BUY sent: {sym} notional ~${dollars:,.0f}")
        return o.id
    except Exception as e:
        logging.error(f"BUY failed {sym}: {e}")
        return None

def fetch_fill_info(sym: str) -> Optional[Dict[str, float]]:
    """Return {'price': float, 'qty': float} for most recent filled buy."""
    try:
        orders = api.list_orders(status="all", limit=50, nested=False)
        fills = [o for o in orders if o.symbol == sym and o.side == "buy" and o.filled_at]
        if not fills:
            return None
        fills.sort(key=lambda o: o.filled_at, reverse=True)
        o = fills[0]
        price = float(o.filled_avg_price) if o.filled_avg_price else None
        qty   = float(o.filled_qty or o.qty) if (o.filled_qty or o.qty) else None
        if price and qty:
            return {"price": price, "qty": qty}
        return None
    except Exception as e:
        logging.debug(f"fetch_fill_info err {sym}: {e}")
        return None

def place_protection(sym: str, entry_price: float, qty: float):
    """Place trailing OR TP/SL using *qty* (fixes 'qty or notional is required')."""
    try:
        if USE_TRAILING_STOP:
            api.submit_order(
                symbol=sym,
                side="sell",
                type="trailing_stop",
                time_in_force="day",
                qty=str(int(qty)),
                trail_percent=str(TRAIL_PCT * 100.0)
            )
            logging.info(f"{sym}: trailing stop set at {TRAIL_PCT:.2%} (qty={int(qty)})")
        else:
            tp = round(entry_price * (1.0 + TAKE_PROFIT_PCT), 2)
            sl = round(entry_price * (1.0 - STOP_LOSS_PCT), 2)
            api.submit_order(symbol=sym, side="sell", type="limit",
                             time_in_force="day", qty=str(int(qty)), limit_price=str(tp))
            api.submit_order(symbol=sym, side="sell", type="stop",
                             time_in_force="day", qty=str(int(qty)), stop_price=str(sl))
            logging.info(f"{sym}: TP/SL placed TP={tp} SL={sl} (qty={int(qty)})")
    except Exception as e:
        logging.error(f"protective orders failed {sym}: {e}")
        # --- Fallback: put a plain STOP immediately to be safe ---
        try:
            fallback_sl = round(entry_price * (1.0 - STOP_LOSS_PCT), 2)
            api.submit_order(
                symbol=sym,
                side="sell",
                type="stop",
                time_in_force="day",
                qty=str(int(qty)),
                stop_price=str(fallback_sl)
            )
            logging.warning(f"{sym}: Fallback STOP placed @ {fallback_sl}")
        except Exception as ee:
            logging.error(f"{sym}: fallback stop failed: {ee}")

def flatten_all(reason: str):
    try:
        api.close_all_positions(cancel_orders=True)
        logging.info(f"Flattened all positions: {reason}")
    except Exception as e:
        logging.error(f"flatten error: {e}")

def reset_day():
    entered_today.clear()
    positions_today.clear()
    logging.info("New trading day -> cleared locks & markers.")

# =========================
# Boot
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

        # reset on new day
        today = datetime.now(timezone.utc).date()
        if last_calendar_day != today:
            last_calendar_day = today
            reset_day()

        if not clock.is_open:
            logging.info("Market closed. Sleeping…")
            time.sleep(max(30.0, SLEEP_SECONDS * 6))
            continue

        mins_to_bell = minutes_to_close(clock) or 0
        allow_new = mins_to_bell > MINUTES_BEFORE_CLOSE

        # refresh open positions
        open_positions = list_open_positions()
        positions_today = {p.symbol for p in open_positions}

        for sym in SYMBOLS:
            if not allow_new:
                continue
            if sym in positions_today:
                continue
            if sym in entered_today:
                continue

            info = get_1m_momentum_and_volume(sym)
            spread = get_spread_pct(sym)
            if info is None or spread is None:
                continue

            mom, vol1m, last_close = info["momentum"], info["vol1m"], info["last_close"]

            logging.info(
                f"CHK {sym} | mom={mom:.2%} need>{MOMENTUM_THRESHOLD:.2%} | "
                f"vol1m={int(vol1m):,} need>={VOLUME_1M:,} | spread={spread:.2%} max<{MAX_SPREAD_PCT:.2%}"
            )

            if mom < MOMENTUM_THRESHOLD:
                continue
            if vol1m < VOLUME_1M:
                continue
            if spread > MAX_SPREAD_PCT:
                continue

            # place BUY (notional)
            order_id = place_buy_notional(sym, PER_TRADE_DOLLARS)
            if not order_id:
                continue

            entered_today.add(sym)

            # wait a moment for fill, then fetch price & qty
            time.sleep(1.0)
            fill = fetch_fill_info(sym)
            if not fill:
                # fallback estimate if API didn't return fill yet
                est_qty = max(1, int(PER_TRADE_DOLLARS / max(0.01, last_close)))
                logging.warning(f"{sym}: fill info missing; using estimate qty={est_qty} price≈{last_close}")
                place_protection(sym, last_close, est_qty)
            else:
                place_protection(sym, fill["price"], fill["qty"])

        # near the bell: flatten safety
        if mins_to_bell <= MINUTES_BEFORE_CLOSE and open_positions:
            for p in open_positions:
                try:
                    api.close_position(p.symbol, cancel_orders=True)
                    logging.info(f"Flatten near close: {p.symbol}")
                except Exception as e:
                    logging.debug(f"flatten {p.symbol} err: {e}")

        time.sleep(SLEEP_SECONDS)

    except KeyboardInterrupt:
        logging.info("Interrupted — shutting down.")
        break
    except Exception as e:
        logging.error(f"Main loop error: {e}")
        time.sleep(SLEEP_SECONDS * CLOCK_SLOWDOWN_ON_ERR)
