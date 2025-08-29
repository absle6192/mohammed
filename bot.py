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
SYMBOLS = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","NFLX",
    "AVGO","ORCL","CRM","COIN","SHOP","UBER"
]  # add/remove symbols as you like

BUY_SIZE    = 5        # shares per trade (change as you want)
BUY_TRIGGER = 0.0005   # >= +0.05% 1-min momentum to buy
TRAIL_STOP  = 0.01     # sell if price drops 1% from post-entry highest
STOP_LOSS   = 0.005    # sell if price drops 0.5% from entry (fixed SL)

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
    # reset high tracking for new position
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
    while True:
        try:
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

            # manage trailing/fixed stops for open positions
            manage_positions()

            time.sleep(60)  # run every minute (keep it 60s for now)
        except Exception as e:
            logging.error(f"Loop error: {e}")
            time.sleep(60)
