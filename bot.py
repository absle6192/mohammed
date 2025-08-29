import os
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
# Environment / API client
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Config
# =========================
SYMBOLS     = SYMBOLS = ["AAPL","MSFT","NVDA","AMZN","META","TSLA","GOOGL","AMD","NFLX","ORCL","AVGO","COIN"]  # change as you like
BUY_SIZE    = 5                         # shares per trade
BUY_TRIGGER = 0.0005                     # +0.3% 1-min momentum to buy
TRAIL_STOP  = 0.01                      # sell if -1% from post-entry highest price

# Tracking highest price per open position
highest_price = {}  # symbol -> float

# =========================
# Helpers
# =========================
def get_last_two_prices(symbol: str):
    """Return (prev_close, last_close) of the last 2 one-minute bars."""
    bars = api.get_bars(symbol, TimeFrame.Minute, limit=2)
    if len(bars) == 2:
        return bars[0].c, bars[1].c
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
# Trading actions
# =========================
def place_buy(symbol: str, qty: int):
    logging.info(f"Placing BUY for {qty} of {symbol}")
    api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="gtc"
    )
    # reset high tracking for the new position
    highest_price[symbol] = None

def place_sell(symbol: str, qty: int):
    logging.info(f"Placing SELL for {qty} of {symbol}")
    api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="market",
        time_in_force="gtc"
    )
    # allow immediate re-entry later
    highest_price.pop(symbol, None)

def manage_positions():
    """Apply trailing-stop logic on all open positions."""
    positions = api.list_positions()
    for pos in positions:
        symbol = pos.symbol
        qty = int(float(pos.qty))
        current_price = float(pos.current_price)

        # initialize or update highest price since entry
        if highest_price.get(symbol) is None:
            highest_price[symbol] = current_price
        if current_price > highest_price[symbol]:
            highest_price[symbol] = current_price

        drop_from_high = (highest_price[symbol] - current_price) / max(highest_price[symbol], 1e-9)
        logging.info(f"{symbol}: high={highest_price[symbol]:.4f}, now={current_price:.4f}, drop={drop_from_high:.2%}")

        # sell if price falls from the highest point by TRAIL_STOP
        if drop_from_high >= TRAIL_STOP:
            logging.info(f"Trailing stop hit for {symbol}, selling…")
            place_sell(symbol, qty)

# =========================
# Main loop
# =========================
if __name__ == "__main__":
    while True:
        try:
            # one active position per symbol
            open_positions = {p.symbol for p in api.list_positions()}

            # scan buy opportunities
            for symbol in SYMBOLS:
                if symbol in open_positions:
                    continue  # already holding this symbol
                if has_open_order(symbol):
                    continue  # avoid double-submitting

                prev_price, last_price = get_last_two_prices(symbol)
                if prev_price and last_price:
                    change = (last_price - prev_price) / prev_price
                    logging.info(f"{symbol} 1-min change: {change:.3%}")
                    if change >= BUY_TRIGGER:
                        place_buy(symbol, BUY_SIZE)

            # manage open positions with trailing stop
            manage_positions()

            time.sleep(60)  # run every minute
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(60)
