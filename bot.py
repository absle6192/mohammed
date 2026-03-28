python
import os
import time
import math
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# -------------------- ENV helpers --------------------
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

def env_int(name: str, default: str) -> int:
    return int(env(name, default))

def env_float(name: str, default: str) -> float:
    return float(env(name, default))

def env_bool(name: str, default: str = "false") -> bool:
    return env(name, default).lower() in ("1", "true", "yes", "y", "on")

# -------------------- Telegram --------------------
def send_tg(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")

# -------------------- Config --------------------
SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN,MU").split(",") if s.strip()]
NOTIONAL_PER_TRADE = env_float("OPEN_NOTIONAL_USD", "30000")

WINDOW_SECONDS = env_int("OPEN_WINDOW_SECONDS", "45")          # تجمع بيانات كم ثانية بعد الافتتاح
MIN_POINTS = env_int("MIN_POINTS", "20")                       # أقل عدد تيكات/نقاط سعر
MIN_MOVE_PCT = env_float("MIN_MOVE_PCT", "0.0006")             # 0.06% (مناسب للافتتاح)
MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.0025")         # 0.25%
COOLDOWN_AFTER = env_int("COOLDOWN_AFTER_OPEN_TRADES", "9999") # نخليه كبير عشان ما يعيد يدخل

ALLOW_SHORT = env_bool("ALLOW_SHORT", "true")

FEED_NAME = env("DATA_FEED", "iex").lower()
FEED = DataFeed.IEX if FEED_NAME == "iex" else DataFeed.SIP

API_KEY = env("APCA_API_KEY_ID")
API_SECRET = env("APCA_API_SECRET_KEY")
PAPER = env_bool("ALPACA_PAPER", "true")  # خله مثل ما هو (واضح عندك موجود)

dataclass
class SymState:
    mids: deque   # mid prices
    spreads: deque  # spread pct
    trade_sizes: deque  # trade sizes
    last_mid: float = 0.0
    last_spread: float = 0.0
    last_price: float = 0.0  # last traded price

state = {s: SymState(deque(maxlen=600), deque(maxlen=600), deque(maxlen=600)) for s in SYMBOLS}

# -------------------- Market Order helpers --------------------
def place_market_entry(trading_client: TradingClient, symbol: str, direction: str, notional_usd: float, last_price: float):
    """
    direction: 'long' or 'short'
    Long: market BUY using notional
    Short: market SELL using qty
    """
    if direction == "long":
        order = MarketOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            notional=round(notional_usd, 2),
        )
        return trading_client.submit_order(order)

    # short
    if not ALLOW_SHORT:
        raise RuntimeError("Short is disabled by ALLOW_SHORT=false")

    qty = math.floor(notional_usd / max(last_price, 0.01))
    if qty <= 0:
        raise ValueError(f"qty computed 0 for {symbol} (notional={notional_usd}, last={last_price})")

    order = MarketOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        qty=qty,
    )
    return trading_client.submit_order(order)

# -------------------- Profit & Selling Logic --------------------
def analyze_profit_and_sell(profit, qty, highest_profit):
    """
    Modify the logic for trailing profits, partial profit-taking, and stop loss.
    """
    try:
        if profit >= highest_profit:
            highest_profit = profit  # Update the highest reached profit

        if highest_profit - 3 >= profit:
            send_tg(f"Trailing profit hit. Selling remaining position as {highest_profit} dropped by $3 to current {profit}.")
            return 'sell_all'  # Notify to sell all remaining quantity

        if profit >= 40:
            send_tg(f"Selling some proportion  Telelogger w Alert overshare logic drafted revised logical assuming View check return first for cutoff towards near neutral wealthspread few tha ...)