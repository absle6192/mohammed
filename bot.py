import os
import time
import requests
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient


# =====================
# Helpers
# =====================
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_bool(name: str, default="false") -> bool:
    return env(name, default).lower() in ("1", "true", "yes", "on")


def send_telegram(text: str):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=10)


# =====================
# Clients
# =====================
def build_clients():
    api_key = env("ALPACA_API_KEY")
    secret  = env("ALPACA_SECRET_KEY")

    paper = env_bool("ALPACA_PAPER", "true")

    data = StockHistoricalDataClient(api_key, secret)

    trading = TradingClient(
        api_key,
        secret,
        paper=paper
    )

    return data, trading, paper


# =====================
# Main
# =====================
def main():
    data, trading, paper = build_clients()

    symbols = env("SYMBOLS", "TSLA,AAPL,NVDA").split(",")
    interval = int(env("INTERVAL_SEC", "15"))

    send_telegram(
        "✅ Bot started (ALERTS)\n"
        f"symbols={','.join(symbols)}\n"
        f"interval={interval}s\n"
        f"feed=IEX\n"
        f"paper={paper}"
    )

    while True:
        for symbol in symbols:
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Minute,
                    limit=5
                )
                bars = data.get_stock_bars(req).df
                if bars is None or bars.empty:
                    continue

                last = bars.iloc[-1]
                price = float(last["close"])

                send_telegram(f"📊 {symbol} price: {price}")

            except Exception as e:
                send_telegram(f"⚠️ Bot error:\n{e}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
