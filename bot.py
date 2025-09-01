# bot.py  — Alpaca paper trading bot (SnapshotV2 safe)

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timezone

from alpaca_trade_api.rest import REST, TimeFrame

# ============================
# Logging
# ============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ============================
# Env & constants
# ============================
API_KEY     = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET  = os.getenv("APCA_API_SECRET_KEY", "").strip()
BASE_URL    = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip()

# Symbols list (edit if you like)
SYMBOLS: List[str] = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "NFLX", "BA", "MU", "JPM", "PEP",
]

# Sizing & risk (simple demo values)
RISK_PCT_PER_TRADE = 0.01  # 1% of buying power per trade
MAX_POSITIONS = 5
POLL_SECONDS = 15

# ============================
# Helpers
# ============================

def make_client() -> REST:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing API keys. Ensure APCA_API_KEY_ID and APCA_API_SECRET_KEY are set.")
    return REST(API_KEY, API_SECRET, BASE_URL)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_snapshot_vwap(client: REST, symbol: str) -> Optional[float]:
    """
    SnapshotV2 has no 'daily_vwap' attribute.
    We read 'daily_bar' and use its 'vwap' if available.
    If 'vwap' is None, approximate via (H+L+C)/3.
    """
    snap = client.get_snapshot(symbol)

    daily_bar = getattr(snap, "daily_bar", None)
    if daily_bar is None:
        logging.error("No daily_bar in snapshot for %s; skipping", symbol)
        return None

    vwap = getattr(daily_bar, "vwap", None)
    if vwap is None:
        # Fallback approximation
        try:
            vwap = (daily_bar.h + daily_bar.l + daily_bar.c) / 3.0
        except Exception:
            logging.exception("Failed to approximate VWAP for %s", symbol)
            return None

    return float(vwap)

def get_last_price_from_snapshot(client: REST, symbol: str) -> Optional[float]:
    snap = client.get_snapshot(symbol)
    trade = getattr(snap, "latest_trade", None)
    if trade and getattr(trade, "p", None) is not None:
        return float(trade.p)
    # Fallback to minute bar close if latest trade is missing
    minute_bar = getattr(snap, "minute_bar", None)
    if minute_bar and getattr(minute_bar, "c", None) is not None:
        return float(minute_bar.c)
    logging.error("No price info in snapshot for %s", symbol)
    return None

def get_buying_power(client: REST) -> float:
    acct = client.get_account()
    return float(acct.buying_power)

def get_position_qty(client: REST, symbol: str) -> int:
    try:
        pos = client.get_position(symbol)
        return int(float(pos.qty))
    except Exception:
        return 0

def open_positions_count(client: REST) -> int:
    try:
        positions = client.list_positions()
        return len(positions)
    except Exception:
        return 0

def submit_market_buy(client: REST, symbol: str, usd_amount: float) -> None:
    if usd_amount <= 0:
        return
    # Use notional to size by USD
    client.submit_order(
        symbol=symbol,
        notional=round(usd_amount, 2),
        side="buy",
        type="market",
        time_in_force="day",
    )
    logging.info("BUY %s notional=%.2f", symbol, usd_amount)

def submit_market_sell_all(client: REST, symbol: str) -> None:
    qty = get_position_qty(client, symbol)
    if qty > 0:
        client.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day",
        )
        logging.info("SELL %s qty=%d", symbol, qty)

# ============================
# Simple strategy:
#   - If last_price > VWAP: buy a slice (if capacity allows)
#   - If last_price < VWAP: close any open position
# ============================

def trade_once(client: REST) -> None:
    try:
        bp = get_buying_power(client)
    except Exception as e:
        logging.error("Failed to fetch account/buying power: %s", e)
        return

    max_new_positions = max(0, MAX_POSITIONS - open_positions_count(client))
    usd_per_trade = max(0.0, bp * RISK_PCT_PER_TRADE)

    for symbol in SYMBOLS:
        try:
            vwap = get_snapshot_vwap(client, symbol)
            last = get_last_price_from_snapshot(client, symbol)
            if vwap is None or last is None:
                continue

            if last > vwap * 1.001:  # tiny buffer
                if get_position_qty(client, symbol) == 0 and max_new_positions > 0 and usd_per_trade > 0:
                    submit_market_buy(client, symbol, usd_per_trade)
                    max_new_positions -= 1
            elif last < vwap * 0.999:
                # below VWAP -> flat
                submit_market_sell_all(client, symbol)
        except Exception as e:
            logging.error("Error on %s: %s", symbol, e)

def main() -> None:
    logging.info("Starting bot | base_url=%s | symbols=%s", BASE_URL, ",".join(SYMBOLS))
    client = make_client()

    # Optional: quick connectivity sanity
    try:
        acct = client.get_account()
        logging.info("Account ok: buying_power=%.2f, status=%s", float(acct.buying_power), acct.status)
    except Exception as e:
        logging.error("Account check failed: %s", e)
        return

    while True:
        loop_start = time.time()
        trade_once(client)
        elapsed = time.time() - loop_start
        sleep_for = max(1.0, POLL_SECONDS - elapsed)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()
