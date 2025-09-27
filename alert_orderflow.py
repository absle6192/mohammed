import os
import sys
import time
import requests


# Ensure stdout uses UTF-8 (avoids 'latin-1' codec errors)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ========= Config from environment =========
# For market data, the correct base is data.alpaca.markets (not paper-api)
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://data.alpaca.markets")
API_KEY = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

# Symbols to watch
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

# Common request headers
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}


def fetch_last_trade_price(symbol: str):
    """
    Fetch the latest trade price for a symbol using Alpaca Market Data v2.
    Endpoint: /v2/stocks/{symbol}/trades/latest
    Returns a float price or None on error.
    """
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 401:
            print(f"AUTH ERROR {symbol}: check API keys or permissions")
            return None
        if r.status_code == 404:
            print(f"NOT FOUND {symbol}: check symbol or endpoint")
            return None

        r.raise_for_status()
        data = r.json() or {}

        # Expected shape: {"symbol":"AAPL","trade":{"p": 225.12, ...}}
        trade = data.get("trade") or {}
        price = trade.get("p")
        if price is None:
            # Some older responses use "price"
            price = trade.get("price")

        if isinstance(price, (int, float)):
            return float(price)

        print(f"PARSE ERROR {symbol}: unexpected payload")
        return None

    except requests.Timeout:
        print(f"TIMEOUT {symbol}: request took too long")
        return None
    except requests.RequestException as e:
        print(f"HTTP ERROR {symbol}: {e}")
        return None
    except ValueError:
        print(f"PARSE ERROR {symbol}: invalid JSON")
        return None
    except Exception as e:
        print(f"UNEXPECTED ERROR {symbol}: {e}")
        return None


def main():
    print("ORDERFLOW WATCHER STARTED")
    # Track last seen price to print only on change
    last_price = {s: None for s in SYMBOLS}

    while True:
        for symbol in SYMBOLS:
            price = fetch_last_trade_price(symbol)
            if price is None:
                continue

            if last_price[symbol] is None or price != last_price[symbol]:
                print(f"{symbol} last price: {price}")
                last_price[symbol] = price

        # Be gentle with the API
        time.sleep(5)


if __name__ == "__main__":
    main()
