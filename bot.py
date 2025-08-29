import os
import time
import logging
from time import monotonic
from alpaca_trade_api.rest import REST, TimeFrame

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

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Config (tuning)
# =========================
# Static fallback list (used if DYNAMIC_SYMBOLS=False or picker fails)
SYMBOLS = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","NFLX","AVGO"
]

BUY_SIZE    = 5          # shares per trade
BUY_TRIGGER = 0.0005     # >= +0.05% 1-min momentum to buy
TRAIL_STOP  = 0.01       # sell if price drops 1% from post-entry highest
STOP_LOSS   = 0.005      # sell if price drops 0.5% from entry

# Dynamic universe selection
DYNAMIC_SYMBOLS = True   # turn on/off dynamic picking
SYMBOL_COUNT    = 10     # how many symbols to track dynamically
REFRESH_MINUTES = 15     # re-pick universe every N minutes

# Track highest price since entry per symbol
highest_price = {}  # symbol -> float

# =========================
# Helpers
# =========================
def get_last_two_prices(symbol: str):
    """Return (prev_close, last_close) of the last 2 one-minute bars."""
    bars = api.get_bars(symbol, TimeFrame.Minute, limit=2)
    if len(bars) == 2:
        return float(bars[0].c), float(bars[1].c)
    return None, None

def has_open_order(symbol: str) -> bool:
    """True if there is any pending open order for the symbol."""
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                return True
        return False
    except Exception as e:
        logging.error(f"has_open_order error: {e}")
        return False

# =========================
# Dynamic symbol picking
# =========================
def list_tradable_us_symbols(limit_universe: int = 200):
    """Return a coarse universe of liquid, tradable US symbols."""
    syms = []
    try:
        assets = api.list_assets(status="active")
        for a in assets:
            if (
                a.tradable
                and a.exchange in ("NASDAQ", "NYSE", "ARCA")
                and a.symbol.isalpha()
                and len(a.symbol) <= 5
            ):
                syms.append(a.symbol)
            if len(syms) >= limit_universe:
                break
    except Exception as e:
        logging.error(f"list_tradable_us_symbols error: {e}")
    return syms

def pick_dynamic_symbols(n: int = 10):
    """
    Pick top 'n' movers by current 1-min % change, boosted by last bar volume.
    Only positive movers are considered.
    """
    base = list_tradable_us_symbols(limit_universe=200)
    scored = []
    for sym in base:
        try:
            bars = api.get_bars(sym, TimeFrame.Minute, limit=2)
            if len(bars) != 2:
                continue
            prev_c = float(bars[0].c)
            last_c = float(bars[1].c)
            last_v = float(bars[1].v)
            if prev_c <= 0:
                continue
            change = (last_c - prev_c) / prev_c  # 1-min % change
            if change <= 0:
                continue  # focus on upward movers
            # score: momentum × (1 + volume factor capped at 2x)
            score = change * (1.0 + min(last_v / 1_000_000.0, 2.0))
            scored.append((score, sym))
        except Exception:
            continue
    scored.sort(reverse=True)
    picked = [sym for _, sym in scored[:n]]
    return picked if picked else SYMBOLS[:n]

# =========================
# Trading actions
# =========================
def place_buy(symbol: str, qty: int = None):
    if qty is None:
        qty = BUY_SIZE
    logging.info(f"Placing BUY for {qty} of {symbol}")
    api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="gtc"
    )
    highest_price[symbol] = None  # reset high tracking

def place_sell(symbol: str, qty: int):
    logging.info(f"Placing SELL for {qty} of {symbol}")
    api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="market",
        time_in_force="gtc"
    )
    highest_price.pop(symbol, None)  # allow re-entry later

# =========================
# Position management (Trailing + Fixed Stop)
# =========================
def manage_positions():
    positions = api.list_positions()
    for pos in positions:
        symbol = pos.symbol
        qty = int(float(pos.qty))
        current_price = float(pos.current_price)
        entry_price = float(pos.avg_entry_price)

        # initialize / update highest post-entry price
        if highest_price.get(symbol) is None:
            highest_price[symbol] = current_price
        if current_price > highest_price[symbol]:
            highest_price[symbol] = current_price

        # % drop from highest since entry (trailing)
        drop_from_high = (highest_price[symbol] - current_price) / max(highest_price[symbol], 1e-9)
        # % drawdown from entry (fixed stop-loss)
        drawdown_from_entry = (entry_price - current_price) / max(entry_price, 1e-9)

        logging.info(
            f"{symbol}: high={highest_price[symbol]:.4f}, now={current_price:.4f}, "
            f"trail_drop={drop_from_high:.2%}, entry_dd={drawdown_from_entry:.2%}"
        )

        # Trailing stop condition
        if drop_from_high >= TRAIL_STOP:
            logging.info(f"Trailing stop hit for {symbol} -> SELL")
            place_sell(symbol, qty)
            continue

        # Fixed stop-loss condition
        if drawdown_from_entry >= STOP_LOSS:
            logging.info(f"Fixed stop-loss hit for {symbol} -> SELL")
            place_sell(symbol, qty)
            continue

# =========================
# Main loop
# =========================
if __name__ == "__main__":
    # Initial dynamic pick
    if DYNAMIC_SYMBOLS:
        try:
            SYMBOLS = pick_dynamic_symbols(SYMBOL_COUNT)
            logging.info(f"Dynamic SYMBOLS = {SYMBOLS}")
        except Exception as e:
            logging.error(f"Initial dynamic pick failed: {e}")

    last_refresh_t = monotonic()

    while True:
        try:
            # Periodic refresh of dynamic universe
            if DYNAMIC_SYMBOLS and (monotonic() - last_refresh_t) >= REFRESH_MINUTES * 60:
                new_syms = pick_dynamic_symbols(SYMBOL_COUNT)
                if new_syms:
                    SYMBOLS = new_syms
                    logging.info(f"Refreshed SYMBOLS = {SYMBOLS}")
                last_refresh_t = monotonic()

            open_positions = {p.symbol for p in api.list_positions()}

            for symbol in SYMBOLS:
                # one active position per symbol
                if symbol in open_positions:
                    continue
                # avoid submitting if an order is pending
                if has_open_order(symbol):
                    continue

                prev_price, last_price = get_last_two_prices(symbol)
                if prev_price and last_price:
                    change = (last_price - prev_price) / prev_price
                    logging.info(f"{symbol} 1-min change: {change:.3%}")
                    if change >= BUY_TRIGGER:
                        place_buy(symbol, BUY_SIZE)

            # manage stops for open positions
            manage_positions()

            time.sleep(60)  # 1-minute cadence (because we're using minute bars)
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(60)
