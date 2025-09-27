# alert_orderflow.py
import os
import time
import json
import requests
import sys

# Make stdout UTF-8 friendly (safe even if already UTF-8)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://data.alpaca.markets")
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

def get_last_price(symbol: str):
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 401:
            print(f"AUTH ERROR {symbol}")
            return None
        if r.status_code == 404:
            print(f"NOT FOUND {symbol}")
            return None
        r.raise_for_status()
        data = r.json()
        # Expect: {"trade": {"p": price, ...}}
        trade = data.get("trade", {})
        price = trade.get("p")
        if price is None:
            print(f"NO PRICE {symbol} payload={json.dumps(data)[:120]}")
            return None
        return float(price)
    except requests.exceptions.RequestException as e:
        print(f"HTTP ERROR {symbol}: {e}")
    except (ValueError, TypeError) as e:
        print(f"PARSE ERROR {symbol}: {e}")
    return None

def main():
    if not API_KEY or not API_SECRET:
        print("MISSING ALPACA KEYS")
        return

    last = {s: None for s in SYMBOLS}
    print("ORDERFLOW WATCHER STARTED")

    while True:
        for sym in SYMBOLS:
            px = get_last_price(sym)
            if px is None:
                continue

            # Log simple ASCII line
            print(f"{sym} last={px}")

            # Simple change alert
            if last[sym] is not None and px != last[sym]:
                print(f"ALERT {sym} changed: {last[sym]} -> {px}")

            last[sym] = px

            # Be gentle with API
            time.sleep(0.8)

        # Small pause between full rounds
        time.sleep(2)

if __name__ == "__main__":
    main()
