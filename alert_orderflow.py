# -*- coding: utf-8 -*-
import os
import sys
import time
import requests

# Force UTF-8 output
sys.stdout.reconfigure(encoding="utf-8")

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = "https://data.alpaca.markets/v2"

# Stocks to monitor
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

def get_last_price(symbol):
    url = f"{BASE_URL}/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET
    }
    try:
        r = requests.get(url, headers=headers)
        data = r.json()
        return data.get("trade", {}).get("p")
    except Exception as e:
        print(f"Error fetching {symbol}: {e}", flush=True)
        return None

def main():
    last_prices = {s: None for s in SYMBOLS}
    print("Starting live stock price monitoring...", flush=True)

    while True:
        for symbol in SYMBOLS:
            price = get_last_price(symbol)
            if price:
                print(f"{symbol} current price: {price}", flush=True)
                if last_prices[symbol] is not None and price != last_prices[symbol]:
                    print(f"Price changed for {symbol}: {last_prices[symbol]} -> {price}", flush=True)
                last_prices[symbol] = price
        time.sleep(5)

if __name__ == "__main__":
    main()
