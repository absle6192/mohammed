import os
import sys
import time
import json
from typing import Optional, Dict, Any

import requests


# ---------- Output encoding: force UTF-8 ----------
try:
    # Render (and some environments) sometimes default to latin-1
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------- Configuration ----------
API_KEY = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

# Use Alpaca Market Data v2 API (not the trading/paper URL)
DATA_BASE = "https://data.alpaca.markets/v2"

# Symbols to watch
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

# Polling seconds between requests (avoid rate limits)
POLL_SECONDS = 10

# Requests settings
TIMEOUT = (5, 15)  # (connect timeout, read timeout)
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Accept": "application/json",
    "User-Agent": "orderflow-watcher/1.0",
}


# ---------- Helpers ----------
def log(msg: str) -> None:
    """Simple UTF-8 safe log."""
    try:
        print(msg, flush=True)
    except Exception:
        # Fallback to ASCII-only (strip non-ascii) if something goes wrong
        safe = msg.encode("ascii", "ignore").decode("ascii")
        print(safe, flush=True)


def parse_json_safely(resp: requests.Response) -> Optional[Dict[str, Any]]:
    """
    Try to parse JSON. If it fails, log a short snippet of text body.
    Returns dict or None.
    """
    # Some errors return text/html or plain text
    ctype = resp.headers.get("Content-Type", "")
    try:
        data = resp.json()
        return data
    except Exception:
        snippet = resp.text[:200].replace("\n", " ")
        log(f"PARSE ERROR {resp.status_code}: invalid JSON (Content-Type={ctype}) body_snippet='{snippet}'")
        return None


def get_latest_trade_price(symbol: str) -> Optional[float]:
    """
    Fetch latest trade price for a symbol using Alpaca Market Data v2.
    Handles both response shapes:
      { "trade": { "p": ... } }  OR  { "data": { "trade": { "p": ... } } }
    Returns price or None.
    """
    url = f"{DATA_BASE}/stocks/{symbol}/trades/latest"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        # Explicit auth/HTTP errors
        if resp.status_code == 401:
            log(f"AUTH ERROR {symbol}: check APCA_API_KEY_ID / APCA_API_SECRET_KEY")
            return None
        if resp.status_code == 403:
            log(f"FORBIDDEN {symbol}: plan may not include this endpoint")
            return None
        if resp.status_code == 404:
            log(f"NOT FOUND {symbol}: symbol or endpoint")
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        log(f"REQUEST ERROR {symbol}: {e}")
        return None

    data = parse_json_safely(resp)
    if not data:
        return None

    # Try both shapes safely
    trade = None
    if isinstance(data, dict):
        if "trade" in data and isinstance(data["trade"], dict):
            trade = data["trade"]
        elif "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            if "trade" in inner and isinstance(inner["trade"], dict):
                trade = inner["trade"]

    if not trade:
        log(f"UNEXPECTED SHAPE {symbol}: {json.dumps(data)[:200]}")
        return None

    price = trade.get("p")
    if isinstance(price, (int, float)):
        return float(price)

    log(f"MISSING PRICE {symbol}: {json.dumps(trade)[:200]}")
    return None


def main() -> None:
    if not API_KEY or not API_SECRET:
        log("CONFIG ERROR: Missing APCA_API_KEY_ID or APCA_API_SECRET_KEY in environment.")
        return

    log("ORDERFLOW WATCHER STARTED")
    last_prices: Dict[str, Optional[float]] = {s: None for s in SYMBOLS}

    while True:
        for symbol in SYMBOLS:
            price = get_latest_trade_price(symbol)
            if price is None:
                # Already logged detailed reason
                continue

            # Print current price
            log(f"{symbol} last trade price: {price}")

            # Simple change notification (only when it changes)
            if last_prices[symbol] is None:
                last_prices[symbol] = price
            elif price != last_prices[symbol]:
                log(f"{symbol} price changed: {last_prices[symbol]} -> {price}")
                last_prices[symbol] = price

            # Be gentle with API
            time.sleep(0.5)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
