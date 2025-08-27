# bot.py — clean & ready for Alpaca real-time

import os
import time
import json
import math
import requests
from datetime import datetime

# ---------- Debug printing ----------
def debug_print(msg: str) -> None:
    """Timestamped console print for logs."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")

def log(msg: str) -> None:
    print(msg)

# ---------- Config (env) ----------
API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
# Trading base URL: leave default for Paper Trading
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
# Market data URL (do not change)
DATA_URL   = "https://data.alpaca.markets"

if not API_KEY or not API_SECRET:
    raise Exception("APCA_API_KEY_ID / APCA_API_SECRET_KEY are missing in environment variables")

# Headers
COMMON_HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}
JSON_HEADERS = {
    **COMMON_HEADERS,
    "Content-Type": "application/json",
}

# ---------- Helpers ----------
def round_price(p: float) -> float:
    """Round to 2 decimals for stocks."""
    return float(f"{p:.2f}")

def dollars_to_qty(dollars: float, price: float) -> int:
    """Convert budget (USD) to integer share quantity."""
    if not price or price <= 0:
        return 0
    qty = int(dollars // price)
    return max(qty, 0)

# ---------- Market status ----------
def is_market_open() -> bool:
    """Check if US market is currently open via Alpaca clock."""
    try:
        url = f"{BASE_URL}/v2/clock"
        r = requests.get(url, headers=COMMON_HEADERS, timeout=5)
        if r.status_code != 200:
            debug_print(f"Clock HTTP {r.status_code} | {r.text[:120]}")
            return True  # fail-open so bot still runs
        data = r.json()
        return bool(data.get("is_open", True))
    except Exception as e:
        debug_print(f"Clock exception: {e}")
        return True

# ---------- Market data (real-time) ----------
def get_last_trade_price(symbol: str) -> float | None:
    """
    Returns last trade price using Alpaca Market Data (requires Algo Trader Plus / Unlimited).
    """
    try:
        url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest"
        r = requests.get(url, headers=COMMON_HEADERS, timeout=5)
        if r.status_code != 200:
            debug_print(f"{symbol}: ❌ Alpaca HTTP {r.status_code} | {r.text[:120]}")
            return None

        data = r.json()
        trade = data.get("trade")
        if trade and "p" in trade:
            price = float(trade["p"])
            debug_print(f"{symbol}: (Alpaca) price = {price}")
            return price

        debug_print(f"{symbol}: (Alpaca) no price in response")
        return None

    except Exception as e:
        debug_print(f"{symbol}: ❌ Alpaca Exception: {e}")
        return None

# ---------- Place order (bracket) ----------
def place_bracket_buy(symbol: str, price: float, qty: int) -> dict | None:
    """
    Places a simple bracket buy: market buy with take-profit and stop-loss.
    Paper trading only with the default BASE_URL.
    """
    if qty <= 0:
        debug_print(f"{symbol}: qty <= 0, skip placing order")
        return None

    # 0.5% take-profit and 0.5% stop-loss as a demo; adjust to your logic
    tp_price = round_price(price * 1.005)
    sl_price = round_price(price * 0.995)

    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": tp_price},
        "stop_loss":   {"stop_price": sl_price},
    }

    try:
        url = f"{BASE_URL}/v2/orders"
        r = requests.post(url, headers=JSON_HEADERS, data=json.dumps(payload), timeout=8)
        if r.status_code not in (200, 201):
            debug_print(f"{symbol}: ❌ order HTTP {r.status_code} | {r.text[:160]}")
            return None
        data = r.json()
        debug_print(f"{symbol}: 🟢 order placed id={data.get('id')} qty={qty}")
        return data
    except Exception as e:
        debug_print(f"{symbol}: ❌ order exception: {e}")
        return None

# ---------- Settings ----------
SYMBOLS_MANUAL = ["MSFT", "NVDA", "AAPL"]  # edit as you like
ENABLE_TRADING = True                      # set False to dry-run
DOLLAR_PER_TRADE = float(os.getenv("DOLLAR_PER_TRADE", "1000"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))

# ---------- Main loop ----------
def main() -> None:
    debug_print("Bot started.")
    while True:
        market_open = is_market_open()
        selected = SYMBOLS_MANUAL

        # Iterate symbols
        for sym in selected:
            price = get_last_trade_price(sym)

            if price is None:
                debug_print(f"{sym}: ⚠️ API returned None")
                debug_print(f"{sym}: لا توجد بيانات.")
                continue
            else:
                debug_print(f"{sym}: ✅ السعر الحالي = {price}")

            log(f"{sym}: آخر سعر = {price}")

            if market_open and ENABLE_TRADING:
                qty = dollars_to_qty(DOLLAR_PER_TRADE, price)
                if qty > 0:
                    res = place_bracket_buy(sym, price, qty)
                    if res is not None:
                        # you can store last_exec_time if you want
                        pass

        time.sleep(POLL_SECONDS)

# ---------- One-shot probe (optional) ----------
def probe(symbol: str = "AAPL") -> None:
    """Quick check to verify your subscription/keys return 200 with a trade price."""
    url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest"
    r = requests.get(url, headers=COMMON_HEADERS, timeout=5)
    debug_print(f"Probe {symbol}: HTTP {r.status_code}")
    try:
        debug_print(f"Body: {r.json()}")
    except Exception:
        debug_print(f"Body(text): {r.text[:200]}")

if __name__ == "__main__":
    # Uncomment next line once to verify 200/price in logs, then comment it back.
    # probe("AAPL")
    main()
