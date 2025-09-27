# alert_orderflow.py
import os
import sys
import time
import json
import logging
from typing import Optional, Dict, List

import requests

# --- Force UTF-8 for stdout even if the host is latin-1 ---
try:
    sys.stdout.reconfigure(encoding="utf-8")  # Python 3.7+
except Exception:
    pass

# --- Safe print: strip non-ASCII so we never crash on encoding ---
def safe_print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    text_ascii = text.encode("ascii", "ignore").decode("ascii")
    print(text_ascii, **kwargs)

# --- Config ---
BASE_URL = os.getenv("APCA_API_BASE_URL", "").rstrip("/")
API_KEY = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")

SYMBOLS: List[str] = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

POLL_SECONDS = 15  # how often to poll

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("alert")

def get_last_trade(symbol: str) -> Optional[Dict]:
    """Return last trade dict from Alpaca data API, or None."""
    if not BASE_URL or not API_KEY or not API_SECRET:
        log.error("Missing Alpaca environment variables.")
        return None

    # v2 last trade endpoint: /v2/stocks/{symbol}/trades/latest
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # expected shape: {"symbol":"TSLA","trade":{...}}
        return data.get("trade")
    except requests.RequestException as e:
        # log only ASCII
        safe_print(f"WARNING Error fetching {symbol}: {e}")
        return None
    except json.JSONDecodeError:
        safe_print(f"WARNING Error decoding JSON for {symbol}")
        return None
    except Exception as e:
        safe_print(f"WARNING Unexpected error for {symbol}: {e}")
        return None

def main():
    last_prices: Dict[str, Optional[float]] = {s: None for s in SYMBOLS}
    safe_print("Starting monitor for symbols:", ", ".join(SYMBOLS))

    while True:
        for symbol in SYMBOLS:
            trade = get_last_trade(symbol)
            if trade is None:
                continue
            price = trade.get("p")  # price field in latest trade payload
            if isinstance(price, (int, float)):
                prev = last_prices[symbol]
                last_prices[symbol] = float(price)
                safe_print(f"{symbol} latest price:", price)

                if prev is not None:
                    delta = price - prev
                    pct = (delta / prev) * 100 if prev != 0 else 0.0
                    # Simple alert threshold: >= 1% move since last check
                    if abs(pct) >= 1.0:
                        direction = "UP" if pct > 0 else "DOWN"
                        safe_print(
                            f"ALERT {symbol}: {direction} {pct:.2f}% | prev={prev:.4f} -> now={price:.4f}"
                        )
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
