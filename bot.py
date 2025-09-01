# bot.py
# ——————————————————————————————————————————
# Day-trade bot (paper) with 1% TP/SL, daily profit cap, auto-sell
# ——————————————————————————————————————————

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timedelta, timezone
from alpaca_trade_api.rest import REST, TimeFrame

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# API credentials (Paper)
# =========================
API_KEY    = "PKN91JQOM2KTA1IGQ5FQ"
API_SECRET = "UHiuozuHZwm3s2rfKIAP5bqcG2AfYHERxMFn51IF"
BASE_URL   = "https://paper-api.alpaca.markets"

# Sanity check
print("BASE_URL =", BASE_URL)
print("KEY_PREFIX =", API_KEY[:6], "SECRET_LEN =", len(API_SECRET))

# API client
api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Risk / Sizing
# =========================
TAKE_PROFIT_PCT   = 0.01   # 1% profit
STOP_LOSS_PCT     = 0.01   # 1% stop loss

DAILY_PROFIT_TARGET_USD = 133.0
DAILY_MAX_LOSS_USD      = 50.0

RISK_PCT_OF_BP    = 0.01
FIXED_DOLLARS_PER_TRADE = 1000.0
MAX_SHARES_PER_TRADE    = 10
MAX_PRICE_PER_SHARE     = 600.0
DAILY_MAX_SPEND         = 5000.0

LOOP_SLEEP_SECONDS = 30

MOMENTUM_THRESHOLD = 0.003
VOLUME_SPIKE_MULT  = 1.2
SPREAD_CENTS_LIMIT = 0.05
SPREAD_PCT_LIMIT   = 0.002
USE_TRAILING_STOP  = True
TRAIL_PCT          = 0.015
MAX_HOLD_MINUTES   = 25
FLATTEN_BEFORE_CLOSE_MIN = 5

# =========================
# Symbols
# =========================
SYMBOLS: List[str] = [
    "AAPL","MSFT","NVDA","TSLA","AMZN",
    "NFLX","BA","MU","PEP","JPM"
]

# =========================
# Data container
# =========================
@dataclass
class Snapshot:
    last_price: float
    bid: float
    ask: float
    vwap: Optional[float]
    min1_change_pct: Optional[float]
    high_5m: Optional[float]
    vol_1m: Optional[float]
    vol_5m_avg: Optional[float]

# =========================
# Helpers
# =========================
def get_snapshot(symbol: str) -> Optional[Snapshot]:
    try:
        snap = api.get_snapshot(symbol)
        minute_bar = api.get_bars(symbol, TimeFrame.Minute, limit=5)
        min1_change_pct = None
        if len(minute_bar) >= 2:
            min1_change_pct = (minute_bar[-1].c - minute_bar[-2].c) / minute_bar[-2].c

        return Snapshot(
            last_price = snap.latest_trade.p,
            bid        = snap.latest_quote.bp,
            ask        = snap.latest_quote.ap,
            vwap       = snap.daily_vwap,
            min1_change_pct = min1_change_pct,
            high_5m    = max([b.h for b in minute_bar]) if minute_bar else None,
            vol_1m     = minute_bar[-1].v if minute_bar else None,
            vol_5m_avg = sum([b.v for b in minute_bar])/len(minute_bar) if minute_bar else None
        )
    except Exception as e:
        logging.error(f"Snapshot error {symbol}: {e}")
        return None

# =========================
# Main loop
# =========================
def run_loop():
    logging.info("Starting bot (paper mode)...")
    while True:
        for sym in SYMBOLS:
            snap = get_snapshot(sym)
            if not snap: 
                continue
            logging.info(f"{sym}: {snap.last_price}")
        time.sleep(LOOP_SLEEP_SECONDS)

if __name__ == "__main__":
    run_loop()
