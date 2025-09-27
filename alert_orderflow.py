# no unicode logs, ASCII only
import os, sys, time, requests

def log(msg: str) -> None:
    # write ASCII-only to stdout (strip anything non-ASCII)
    sys.stdout.buffer.write((msg.encode("ascii", "ignore") + b"\n"))
    sys.stdout.flush()

# --- Config from env ---
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://data.alpaca.markets").rstrip("/")
API_KEY = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

def get_last_price(symbol: str):
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 401:
            log(f"AUTH ERROR {symbol}")
            return None
        if r.status_code == 404:
            log(f"NOT FOUND {symbol}")
            return None
        r.raise_for_status()
        data = r.json()
        trade = data.get("trade") or {}
        return trade.get("p")
    except Exception as e:
        # also force ASCII for the exception text
        log(f"FETCH ERROR {symbol}: {str(e).encode('ascii','ignore').decode('ascii')}")
        return None

def main():
    last = {s: None for s in SYMBOLS}
    log("ORDERFLOW WATCHER STARTED")

    while True:
        for s in SYMBOLS:
            price = get_last_price(s)
            if price is None:
                continue
            log(f"{s} PRICE {price}")
            if last[s] is not None and price != last[s]:
                log(f"ALERT {s} {last[s]} -> {price}")
            last[s] = price
        time.sleep(15)

if __name__ == "__main__":
    main()
