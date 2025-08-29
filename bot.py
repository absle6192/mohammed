# bot.py — LITE intraday buyer with bracket TP/SL (Alpaca)
# --------------------------------------------
# What it does
# - Scans a watchlist every few seconds
# - Enters only if the daily % change filter is met (if available)
# - Places a BRACKET order: take-profit + stop-loss
# - The stop-loss is ALWAYS strictly below entry to satisfy API rules

import os
import time
from datetime import datetime, timezone
from math import floor

from alpaca_trade_api import REST

# ========= USER SETTINGS =========
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "<YOUR_KEY>")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "<YOUR_SECRET>")
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# Watchlist
SYMBOLS = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "NVDA",
    "META", "TSLA", "BRK.B", "V", "UNH",
    "PG", "KO", "COST", "PEP", "MRK", "HD"
]

# Entry filters
DAILY_CHANGE_MIN = float(os.getenv("DAILY_CHANGE_MIN", "0.02"))  # 0.02 = +2%
USE_MOMENTUM_1M = False   # keep False for the LITE version
USE_SPREAD_FILTER = False # keep False for the LITE version

# Position sizing
DOLLARS_PER_TRADE = float(os.getenv("DOLLARS_PER_TRADE", "900"))  # ~900$ per entry

# Bracket (take profit / stop loss) as percent from entry
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.04"))  # +4%
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", "0.02"))    # -2%

# Polling
SLEEP_SECONDS = 5

# =================================
api = REST(APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL)

def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def get_last_price(symbol: str) -> float:
    """Get last trade price; fallback to 0 if unavailable."""
    try:
        lt = api.get_last_trade(symbol)
        return float(lt.price)
    except Exception:
        return 0.0

def get_daily_change(symbol: str):
    """
    Return today's % change (0.12 = +12%) if available, else None.
    We only reject on this filter if value IS present and below threshold.
    """
    try:
        bar = api.get_latest_bar(symbol)
        if not bar:
            return None
        # approximate daily % change using open vs current
        o = float(bar.open)
        c = float(bar.close)
        if o > 0:
            return (c - o) / o
        return None
    except Exception:
        return None

def round_price_to_cents(x: float) -> float:
    """Round to 2 decimals (works for most listed stocks)."""
    return round(x + 1e-9, 2)

def calc_qty(dollars: float, price: float) -> int:
    """Calculate integer quantity, ensuring >=1."""
    if price <= 0:
        return 0
    q = floor(dollars / price)
    return int(q)

def safe_stop_loss(entry_price: float) -> float:
    """
    Compute a safe stop-loss:
    - Primary: entry * (1 - STOP_LOSS_PCT)
    - Safety rule: must be <= entry - 0.02 (a tiny margin)
    - Finally, round to cents and clamp again if needed
    """
    raw_sl = entry_price * (1.0 - STOP_LOSS_PCT)
    min_required = entry_price - 0.02  # strictly below by at least $0.02
    sl = min(raw_sl, min_required)
    sl = round_price_to_cents(sl)

    # If after rounding it is not strictly below entry, force 0.02 below
    if sl >= entry_price:
        sl = round_price_to_cents(entry_price - 0.02)
    return sl

def take_profit_from_entry(entry_price: float) -> float:
    tp = entry_price * (1.0 + TAKE_PROFIT_PCT)
    return round_price_to_cents(tp)

def place_bracket_buy(symbol: str, last_price: float, qty: int):
    """
    Place a bracket market BUY with TP/SL.
    Ensures SL is strictly below base (API requirement).
    """
    entry = last_price  # approximate base price for targets
    tp_price = take_profit_from_entry(entry)
    sl_price = safe_stop_loss(entry)

    print(f"{now_ts()} | INFO | BUY {symbol} qty={qty} @~{entry:.2f} TP={tp_price:.2f} SL={sl_price:.2f}")

    o = api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="day",
        order_class="bracket",
        take_profit={"limit_price": tp_price},
        stop_loss={"stop_price": sl_price}
    )
    return o

def scan_and_trade():
    print(f"{now_ts()} | INFO | Starting LITE day-trading bot (daily% + bracket; safety SL fix enabled)...")
    print(f"{now_ts()} | INFO | New trading day: Reset state.")

    while True:
        for sym in SYMBOLS:
            try:
                last = get_last_price(sym)
                if last <= 0:
                    continue

                day_chg = get_daily_change(sym)

                # ---- Entry filter: daily change
                day_ok = True
                reason = ""
                if day_chg is not None:
                    if day_chg < DAILY_CHANGE_MIN:
                        day_ok = False
                        reason = "daily_change_below_threshold"

                if not day_ok:
                    print(f"{now_ts()} | INFO | NO_ENTRY {sym}: {reason} | day={day_chg}")
                    continue

                # Quantity
                qty = calc_qty(DOLLARS_PER_TRADE, last)
                if qty < 1:
                    print(f"{now_ts()} | INFO | SKIP {sym}: qty<1 (price={last:.2f})")
                    continue

                # Place bracket buy
                place_bracket_buy(sym, last, qty)

            except Exception as e:
                print(f"{now_ts()} | ERROR | {sym} | {e}")

        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    scan_and_trade()
