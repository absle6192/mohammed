# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")

import os
import time
import logging
from typing import Optional, Dict
import requests

# -------- Settings --------
BASE_URL = "https://data.alpaca.markets/v2"
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]
POLL_SECONDS = 15            # check frequency
PCT_ALERT = 0.30             # alert if move >= 0.30%

API_KEY = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("alert")

def get_latest_price(symbol: str) -> Optional[float]:
    """Return latest trade price for symbol, or None on error."""
    url = f"{BASE_URL}/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        price = data.get("trade", {}).get("p")
        return float(price) if price is not None else None
    except Exception as e:
        log.warning("Error fetching %s: %s", symbol, e)
        return None

def main():
    # sanity check for keys
    if not API_KEY or not API_SECRET:
        log.error("Missing API keys. Please set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")
        time.sleep(60)
        return

    last: Dict[str, Optional[float]] = {s: None for s in SYMBOLS}
    log.info("Started orderflow alerts for: %s", ", ".join(SYMBOLS))

    while True:
        for sym in SYMBOLS:
            price = get_latest_price(sym)
            if price is None:
                continue

            prev = last[sym]
            last[sym] = price

            if prev is None:
                log.info("%s | price: %.2f", sym, price)
                continue

            change_pct = ((price - prev) / prev) * 100.0
            if abs(change_pct) >= PCT_ALERT:
                direction = "UP" if change_pct > 0 else "DOWN"
                log.info("%s | %s %.2f%% | prev: %.2f -> now: %.2f",
                         sym, direction, change_pct, prev, price)
            else:
                log.debug("%s | %.2f (%.2f%%)", sym, price, change_pct)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
