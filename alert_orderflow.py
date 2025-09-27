import os
import sys
import time
import requests

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = "https://data.alpaca.markets/v2"

SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

def log(msg: str):
    # Write bytes to stdout to avoid any encoding errors
    sys.stdout.buffer.write((str(msg) + "\n").encode("utf-8", "ignore"))
    sys.stdout.flush()

def get_last_price(symbol: str):
    url = f"{BASE_URL}/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY or "",
        "APCA-API-SECRET-KEY": API_SECRET or "",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            # Show short server message for debugging
            txt = r.text[:200].replace("\n", " ")
            log(f"{symbol} bad status {r.status_code}: {txt}")
            return None
        data = r.json()
        return data.get("trade", {}).get("p")
    except Exception as e:
        log(f"Error fetching {symbol}: {e}")
        return None

def main():
    if not API_KEY or not API_SECRET:
        log("Missing APCA_API_KEY_ID or APCA_API_SECRET_KEY.")
    last_prices = {s: None for s in SYMBOLS}
    log("Starting live stock price monitoring...")

    while True:
        for symbol in SYMBOLS:
            price = get_last_price(symbol)
            if price is not None:
                prev = last_prices[symbol]
                log(f"{symbol} current price: {price}")
                if prev is not None and price != prev:
                    log(f"Price changed for {symbol}: {prev} -> {price}")
                last_prices[symbol] = price
        time.sleep(5)

if __name__ == "__main__":
    main()
