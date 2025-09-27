# alert_orderflow.py
# Minimal Alpaca market watcher with safe ASCII-only headers & UTF-8 stdout.

import os
import sys
import time
import json
import requests

# ----- stdout in UTF-8 (safe on Render) -----
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ----- helpers -----
RTL_MARKS = ("\u200f", "\u200e")  # invisible RTL/LTR marks

def clean_header_value(val: str) -> str:
    """
    Remove invisible marks and non-ASCII from header values to avoid
    urllib3/http.client latin-1 encode errors.
    """
    if not isinstance(val, str):
        val = str(val)
    for m in RTL_MARKS:
        val = val.replace(m, "")
    # keep headers strictly ASCII
    val = val.encode("ascii", "ignore").decode("ascii")
    return val.strip()

def safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except Exception:
        # show a short preview to debug invalid JSON without unicode noise
        text = (resp.text or "")[:200].encode("ascii", "ignore").decode("ascii")
        print(f"[WARN] Invalid JSON (status {resp.status_code}): {text}")
        return {}

# ----- config from environment -----
# For market data, use data.alpaca.markets
BASE_URL  = os.getenv("APCA_API_BASE_URL", "https://data.alpaca.markets").strip()
API_KEY   = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET= os.getenv("APCA_API_SECRET_KEY", "").strip()

# Clean any hidden chars that might have slipped into env values
BASE_URL   = clean_header_value(BASE_URL)
API_KEY    = clean_header_value(API_KEY)
API_SECRET = clean_header_value(API_SECRET)

# HTTP headers (ASCII-only)
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# Symbols to watch (ASCII only)
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

# ----- data fetch -----
def get_last_price(symbol: str):
    """
    Fetch last trade price from Alpaca v2 market data.
    Endpoint: /v2/stocks/{symbol}/trades/latest
    """
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 401:
            print(f"[AUTH] Check API keys (401) for {symbol}")
            return None
        if r.status_code == 404:
            print(f"[NOT FOUND] {symbol} not available (404)")
            return None
        r.raise_for_status()
        data = safe_json(r)
        # Expected shape: {"symbol":"TSLA","trade":{"p": <price>, ...}}
        price = None
        trade = data.get("trade") or {}
        if isinstance(trade, dict):
            price = trade.get("p")
        if price is None:
            print(f"[PARSE] Missing price in response for {symbol}")
        return price
    except requests.Timeout:
        print(f"[TIMEOUT] {symbol}")
    except requests.RequestException as e:
        # Keep message ASCII-only
        msg = str(e).encode("ascii", "ignore").decode("ascii")
        print(f"[HTTP ERROR] {symbol}: {msg}")
    except Exception as e:
        msg = str(e).encode("ascii", "ignore").decode("ascii")
        print(f"[ERROR] {symbol}: {msg}")
    return None

# ----- main loop -----
def main():
    print("[START] Orderflow watcher started.")
    last_prices = {s: None for s in SYMBOLS}

    while True:
        for s in SYMBOLS:
            price = get_last_price(s)
            if price is not None:
                # Basic change detection
                if last_prices[s] is None:
                    print(f"[INIT] {s} = {price}")
                elif price != last_prices[s]:
                    print(f"[MOVE] {s}: {last_prices[s]} -> {price}")
                last_prices[s] = price
        # Poll interval (seconds)
        time.sleep(5)

if __name__ == "__main__":
    main()
