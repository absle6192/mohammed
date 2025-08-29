# bot.py  — LITE + Dynamic Trailing
# Requires: alpaca-trade-api
# Behavior:
# - Scans a watchlist and buys when simple entry filters pass
# - Immediately attaches a TRAILING STOP (dynamic) that moves up with price
# - Optional hard take-profit (sell limit) if price hits a fixed % gain
# - If either TP or trailing stop fills, the other order gets cancelled

import os, time, math, logging
from datetime import datetime, timezone
from typing import Optional, Dict, List
import alpaca_trade_api as tradeapi

# =========================
# CONFIG
# =========================
API_KEY        = os.getenv("APCA_API_KEY_ID")
API_SECRET     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

SYMBOLS: List[str] = [
    "AAPL","MSFT","AMZN","GOOGL","NVDA","META","TSLA","KO","PEP","COST","V","UNH",
    "PG","JPM","HD","NFLX","CRM","MRK","TXN","IBM","INTC","CAT","GE","LLY","XOM","ORCL","BAC","WMT","TMO"
]

# --- Entry filters (lightweight) ---
DAILY_CHANGE_MIN = 0.02     # 2% day gain required (None means ignore day % filter)
ALLOW_BELOW_VWAP = True     # if False, require last >= vwap (needs minute bars)
MOMENTUM_1M_MIN  = 0.00     # +% change last ~1m; set negative to disable
MAX_SPREAD_PCT   = 0.40/100 # skip if spread too wide (e.g., >0.40%)

# --- Risk & exits ---
ALLOC_PER_TRADE_USD = 900.0  # position size cap per trade
TRAIL_PCT            = 0.8/100  # trailing stop distance (e.g., 0.8%)
HARD_TP_PCT          = 4.0/100  # optional take-profit (e.g., 4%); set None to disable
MIN_QTY              = 1        # avoid fractional qty<1

# --- Runtime ---
LOOP_SLEEP_SEC = 10      # scan cadence
CANCEL_CHECK_SEC = 2     # poll child orders after entry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

# =========================
# Helpers
# =========================
def now_et_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def get_last_price(symbol: str) -> Optional[float]:
    q = api.get_last_quote(symbol)
    # Fallback: use ask if good; else bid; else mid
    pxs = [q.askprice, q.bidprice, (q.askprice + q.bidprice)/2 if q.askprice and q.bidprice else None]
    for p in pxs:
        if p and p > 0:
            return p
    return None

def get_spread_pct(symbol: str) -> float:
    q = api.get_last_quote(symbol)
    if q.askprice and q.bidprice and q.askprice > 0:
        return (q.askprice - q.bidprice) / q.askprice
    return 9.99  # huge to force-skip if no data

def daily_change(symbol: str) -> Optional[float]:
    # Use previous close vs last trade
    t = api.get_last_trade(symbol)
    bars = api.get_bars(symbol, "1D", limit=2)
    if not bars or len(bars) == 0:
        return None
    prev_close = bars[-1].c if len(bars) == 1 else bars[-2].c
    if prev_close and prev_close > 0:
        return (t.price - prev_close) / prev_close
    return None

def momentum_1m(symbol: str) -> Optional[float]:
    bars = api.get_bars(symbol, "1Min", limit=2)
    if not bars or len(bars) < 2:
        return None
    old = bars[-2].c
    new = bars[-1].c
    if old and old > 0:
        return (new - old) / old
    return None

def vwap_ok(symbol: str) -> bool:
    # Simple intraday VWAP check using last minute bar
    bars = api.get_bars(symbol, "1Min", limit=1)
    if not bars:
        return True  # don’t block on missing data
    b = bars[-1]
    last = b.c
    vwap = b.vw if hasattr(b, "vw") and b.vw else None
    return True if (ALLOW_BELOW_VWAP or vwap is None or last >= vwap) else False

def can_enter(symbol: str) -> (bool, str):
    sp = get_spread_pct(symbol)
    if sp > MAX_SPREAD_PCT:
        return False, f"wide_spread {sp:.2%}"

    day = daily_change(symbol)  # may be None
    if DAILY_CHANGE_MIN is not None and day is not None and day < DAILY_CHANGE_MIN:
        return False, f"day% {day:.2%} < {DAILY_CHANGE_MIN:.2%}"

    if not vwap_ok(symbol):
        return False, "below_VWAP"

    mom = momentum_1m(symbol)  # may be None
    if mom is not None and mom < MOMENTUM_1M_MIN:
        return False, f"1m_mom {mom:.2%} < {MOMENTUM_1M_MIN:.2%}"

    return True, "ok"

def position_exists(symbol: str) -> bool:
    try:
        api.get_position(symbol)
        return True
    except Exception:
        return False

def open_qty(symbol: str) -> int:
    try:
        p = api.get_position(symbol)
        return int(float(p.qty))
    except Exception:
        return 0

def round_qty(dollars: float, price: float) -> int:
    if not price or price <= 0:
        return 0
    q = int(dollars // price)
    return max(q, 0)

# =========================
# Order placement
# =========================
def place_trailing_stop(symbol: str, qty: int, trail_pct: float):
    """
    Places a trailing stop SELL for full qty. It trails by a % of price.
    """
    api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="trailing_stop",
        time_in_force="day",
        trail_percent=round(trail_pct*100, 4)  # Alpaca expects percent number, e.g., 0.8 -> 0.8
    )

def place_take_profit(symbol: str, qty: int, entry_price: float, tp_pct: float):
    limit_price = round(entry_price * (1.0 + tp_pct), 2)
    api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="limit",
        time_in_force="day",
        limit_price=limit_price
    )
    return limit_price

def cancel_open_sells(symbol: str):
    for o in api.list_orders(status="open", side="sell"):
        if o.symbol == symbol:
            api.cancel_order(o.id)

def buy_with_dynamic_exits(symbol: str):
    if position_exists(symbol):
        return

    price = get_last_price(symbol)
    if not price:
        return

    qty = round_qty(ALLOC_PER_TRADE_USD, price)
    if qty < MIN_QTY:
        log.info(f"SKIP {symbol}: qty<{MIN_QTY} (price={price:.2f})")
        return

    # Market buy
    api.submit_order(symbol=symbol, qty=qty, side="buy", type="market", time_in_force="day")
    log.info(f"BUY {symbol} qty={qty} @~{price:.2f}")

    # Wait until position appears
    for _ in range(30):
        time.sleep(CANCEL_CHECK_SEC)
        if open_qty(symbol) >= qty:
            break

    # Attach trailing stop + (optional) take profit
    cancel_open_sells(symbol)  # safety
    place_trailing_stop(symbol, qty=open_qty(symbol), trail_pct=TRAIL_PCT)
    tp_price = None
    if HARD_TP_PCT is not None:
        tp_price = place_take_profit(symbol, qty=open_qty(symbol), entry_price=price, tp_pct=HARD_TP_PCT)

    log.info(f"ATTACH {symbol}: trailing={TRAIL_PCT:.2%}" +
             (f", hard_TP≈{tp_price}" if tp_price else ""))

    # Watch: if one exit fills, cancel the other (simple polling)
    for _ in range(600):  # ~20 minutes
        time.sleep(CANCEL_CHECK_SEC)
        # If position closed -> cancel any residual sells and stop watching
        if open_qty(symbol) == 0:
            cancel_open_sells(symbol)
            log.info(f"EXITED {symbol}: position closed; cleaned remaining orders.")
            return

# =========================
# MAIN LOOP
# =========================
def main():
    log.info("Starting LITE bot with dynamic trailing...")
    while True:
        try:
            for sym in SYMBOLS:
                if position_exists(sym):
                    continue
                ok, why = can_enter(sym)
                if ok:
                    buy_with_dynamic_exits(sym)
                else:
                    log.info(f"NO_ENTRY {sym}: {why}")
            time.sleep(LOOP_SLEEP_SEC)
        except Exception as e:
            log.exception(f"Loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
