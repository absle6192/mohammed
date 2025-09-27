# alert_orderflow.py
import os
import sys
import time
import requests

# Ensure UTF-8 stdout (and we keep all logs in plain English/ASCII)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass  # works on Python 3.7+; safe to ignore elsewhere

# ===== Configuration from environment =====
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://data.alpaca.markets")
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

# The 8 legacy symbols you asked to track
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# ---- Small ASCII-only logger (prevents codec errors) ----
def log(msg: str) -> None:
    # keep logs English/ASCII to avoid any terminal codec issues
    sys.stdout.write((msg + "\n").encode("ascii", "ignore").decode("ascii"))
    sys.stdout.flush()

# ---- Fetch latest trade price for a symbol ----
def get_last_price(symbol: str):
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 401:
            log(f"AUTH ERROR {symbol}: check API keys")
            return None
        if r.status_code == 404:
            log(f"NOT FOUND {symbol}: symbol may be invalid")
            return None

        r.raise_for_status()
        data = r.json()
        # Expected shape: {"symbol":"TSLA","trade":{"p": price, ...}}
        trade = data.get("trade", {})
        price = trade.get("p")
        if price is None:
            log(f"NO PRICE {symbol}: missing field 'trade.p'")
            return None
        return float(price)
    except Exception as e:
        log(f"FETCH ERROR {symbol}: {e}")
        return None

# ---- Main loop ----
def main():
    log("ORDERFLOW WATCHER STARTED")
    last_prices = {s: None for s in SYMBOLS}
    change_threshold = 0.005  # 0.5% change to alert

    while True:
        for symbol in SYMBOLS:
            price = get_last_price(symbol)
            if price is None:
                continue

            prev = last_prices[symbol]
            if prev is None:
                log(f"{symbol}: {price:.2f}")
            else:
                change = price - prev
                pct = (change / prev) if prev != 0 else 0.0
                log(f"{symbol}: {price:.2f}  (Δ {change:+.2f}, {pct*100:+.2f}%)")
                if abs(pct) >= change_threshold:
                    log(f"ALERT {symbol}: move >= {change_threshold*100:.1f}%")

            last_prices[symbol] = price
            time.sleep(0.8)  # gentle pacing per symbol

        time.sleep(10)  # pause between full cycles

if __name__ == "__main__":
    main()
