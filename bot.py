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

WINDOW_SECONDS = env_int("OPEN_WINDOW_SECONDS", "45")
MIN_POINTS = env_int("MIN_POINTS", "20")
MIN_MOVE_PCT = env_float("MIN_MOVE_PCT", "0.0006")
MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.0025")
COOLDOWN_AFTER = env_int("COOLDOWN_AFTER_OPEN_TRADES", "9999")

ALLOW_SHORT = env_bool("ALLOW_SHORT", "true")

FEED_NAME = env("DATA_FEED", "iex").lower()
FEED = DataFeed.IEX if FEED_NAME == "iex" else DataFeed.SIP

API_KEY = env("APCA_API_KEY_ID")
API_SECRET = env("APCA_API_SECRET_KEY")
PAPER = env_bool("ALPACA_PAPER", "true")


# -------------------- SELL CONFIG --------------------
STOP_LOSS = -50
TRIGGER_PROFIT = 7
TRAILING_GAP = 3
peak_profit = {}


@dataclass
class SymState:
    mids: deque
    spreads: deque
    trade_sizes: deque
    last_mid: float = 0.0
    last_spread: float = 0.0
    last_price: float = 0.0


state = {s: SymState(deque(maxlen=600), deque(maxlen=600), deque(maxlen=600)) for s in SYMBOLS}


# -------------------- Market Order helpers --------------------
def place_market_entry(trading_client: TradingClient, symbol: str, direction: str, notional_usd: float, last_price: float):
    if direction == "long":
        order = MarketOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            notional=round(notional_usd, 2),
        )
        return trading_client.submit_order(order)

    if not ALLOW_SHORT:
        raise RuntimeError("Short is disabled")

    qty = math.floor(notional_usd / max(last_price, 0.01))

    order = MarketOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        qty=qty,
    )
    return trading_client.submit_order(order)


# -------------------- SELL MONITOR --------------------
def monitor_positions(trading: TradingClient):
    while True:
        try:
            positions = trading.get_all_positions()

            for p in positions:
                symbol = p.symbol
                qty = float(p.qty)
                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)

                profit = (current_price - entry_price) * qty

                if profit <= STOP_LOSS:
                    logging.info(f"STOP LOSS SELL: {symbol} | {profit}")
                    trading.submit_order(MarketOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    ))
                    peak_profit.pop(symbol, None)
                    continue

                if profit >= TRIGGER_PROFIT:
                    if symbol not in peak_profit:
                        peak_profit[symbol] = profit

                    if profit > peak_profit[symbol]:
                        peak_profit[symbol] = profit

                    if profit <= peak_profit[symbol] - TRAILING_GAP:
                        logging.info(f"TRAILING SELL: {symbol} | {profit}")
                        trading.submit_order(MarketOrderRequest(
                            symbol=symbol,
                            qty=qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY
                        ))
                        peak_profit.pop(symbol, None)

            time.sleep(1)

        except Exception as e:
            logging.warning(f"Monitor error: {e}")
            time.sleep(2)


# -------------------- Main --------------------
def main():
    trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    stream = StockDataStream(API_KEY, API_SECRET, feed=FEED)

    for s in SYMBOLS:
        stream.subscribe_quotes(lambda q: None, s)
        stream.subscribe_trades(lambda t: None, s)

    import threading
    threading.Thread(target=stream.run, daemon=True).start()

    while True:
        try:
            if trading.get_clock().is_open:
                break
            time.sleep(5)
        except:
            time.sleep(5)

    time.sleep(WINDOW_SECONDS)

    scored = [{"symbol": s, "direction": "long", "last_price": 100} for s in SYMBOLS]

    filled = []

    for r in scored[:3]:
        try:
            order = place_market_entry(trading, r["symbol"], r["direction"], NOTIONAL_PER_TRADE, r["last_price"])
            filled.append(r["symbol"])
        except:
            pass

    # 🔥 تشغيل البيع
    import threading
    threading.Thread(target=monitor_positions, args=(trading,), daemon=True).start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
