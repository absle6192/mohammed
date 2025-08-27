import os
import time
import logging
import requests
from typing import Optional, List, Dict

# ================ Logging ================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ================ ENV ====================
API_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
TRADING_BASE = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip()
DATA_BASE = os.getenv("APCA_DATA_BASE_URL", "https://data.alpaca.markets").strip()

SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,AMZN").split(",")]

BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.003"))
ORDER_NOTIONAL = float(os.getenv("ORDER_NOTIONAL", "1000"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.01"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.005"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))

HDR = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET
}

# ================ Functions ================
def get_account():
    url = f"{TRADING_BASE}/v2/account"
    r = requests.get(url, headers=HDR)
    r.raise_for_status()
    return r.json()

def get_price(symbol: str) -> Optional[float]:
    url = f"{DATA_BASE}/v2/stocks/{symbol}/quotes/latest"
    r = requests.get(url, headers=HDR)
    if r.status_code != 200:
        logging.warning(f"Failed to fetch price for {symbol}: {r.text}")
        return None
    data = r.json()
    return float(data["quote"]["ap"]) if "quote" in data and "ap" in data["quote"] else None

def submit_order(symbol: str, side: str, notional: float):
    url = f"{TRADING_BASE}/v2/orders"
    order = {
        "symbol": symbol,
        "notional": notional,
        "side": side,
        "type": "market",
        "time_in_force": "day"
    }
    r = requests.post(url, headers=HDR, json=order)
    if r.status_code != 200:
        logging.error(f"Order failed: {r.text}")
    else:
        logging.info(f"Order success: {r.json()}")

def run_bot():
    logging.info("Starting trading bot...")
    try:
        acc = get_account()
        logging.info(f"Account status: {acc['status']}, equity: {acc['equity']}")
    except Exception as e:
        logging.error(f"Error fetching account: {e}")
        return

    while True:
        for symbol in SYMBOLS:
            price = get_price(symbol)
            if not price:
                continue

            logging.info(f"{symbol} price: {price}")
            # Example condition (dummy strategy)
            if price > 100:  # Replace with your condition
                submit_order(symbol, "buy", ORDER_NOTIONAL)
            else:
                logging.info(f"No trade for {symbol}")

        time.sleep(SCAN_INTERVAL)

# ================ Main ====================
if __name__ == "__main__":
    run_bot()
