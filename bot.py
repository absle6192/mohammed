import os
import time
import math
import requests
from datetime import datetime, timezone
from typing import Tuple

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


# =====================
# TELEGRAM
# =====================
def send_telegram(msg: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": msg,
        "disable_web_page_preview": True
    }, timeout=10)


# =====================
# ALPACA CLIENTS (✅ الصحيح)
# =====================
def build_clients() -> Tuple[StockHistoricalDataClient, TradingClient]:
    api_key = os.getenv("APCA_API_KEY_ID")
    secret  = os.getenv("APCA_API_SECRET_KEY")
    base_url = os.getenv("APCA_API_BASE_URL")

    if not api_key or not secret:
        raise RuntimeError("Missing Alpaca API keys")

    paper = os.getenv("ALPACA_PAPER", "true").lower() in ("1", "true", "yes")

    hist = StockHistoricalDataClient(api_key, secret)
    trade = TradingClient(api_key, secret, paper=paper, base_url=base_url)

    return hist, trade


# =====================
# SETTINGS
# =====================
SYMBOLS = os.getenv("SYMBOLS", "TSLA,AAPL").split(",")
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "2000"))
SLEEP_SEC = int(os.getenv("INTERVAL_SEC", "15"))


# =====================
# MAIN LOGIC
# =====================
def main():
    hist, trade = build_clients()
    send_telegram("✅ البوت اشتغل بنجاح")

    while True:
        try:
            for sym in SYMBOLS:
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    limit=5
                )
                bars = hist.get_stock_bars(req).df
                if bars.empty:
                    continue

                last = bars.iloc[-1]
                prev = bars.iloc[-2]

                if last.close > prev.close:
                    qty = max(1, int(USD_PER_TRADE / last.close))
                    order = MarketOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY
                    )
                    trade.submit_order(order)
                    send_telegram(f"📈 BUY {sym} @ {last.close:.2f}")

            time.sleep(SLEEP_SEC)

        except Exception as e:
            send_telegram(f"❌ خطأ: {e}")
            time.sleep(10)


# =====================
if __name__ == "__main__":
    main()
