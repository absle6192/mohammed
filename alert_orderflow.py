import os, sys, time, requests, io, unicodedata

# Force UTF-8 for stdout/stderr no matter what
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

def log(msg: str):
    """Print safe ASCII/UTF-8 logs without bidi/zero-width chars."""
    if not isinstance(msg, str):
        msg = str(msg)
    # remove control/format chars (includes U+200F and friends)
    msg = "".join(ch for ch in msg if ch.isprintable() and unicodedata.category(ch) != "Cf")
    try:
        sys.stdout.write(msg + "\n")
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
    sys.stdout.flush()

# ===== Config from env =====
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
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 401:
            log(f"AUTH ERROR {symbol}")
            return None
        if r.status_code == 404:
            log(f"NOT FOUND {symbol}")
            return None
        r.raise_for_status()
        data = r.json()
        return data.get("trade", {}).get("p")
    except Exception as e:
        log(f"FETCH ERROR {symbol}: {e}")
        return None

def main():
    log("ORDERFLOW WATCHER STARTED")
    while True:
        for symbol in SYMBOLS:
            price = get_last_price(symbol)
            if price:
                log(f"CURRENT PRICE {symbol}: {price}")
        time.sleep(10)

if __name__ == "__main__":
    main()
