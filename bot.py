import os
import time
import requests
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# ========= helpers =========
def env(name, default=None):
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()


def send_telegram(msg: str):
    try:
        token = env("TELEGRAM_BOT_TOKEN")
        chat_id = env("TELEGRAM_CHAT_ID")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        }

        r = requests.post(url, json=payload, timeout=10)
        print("TELEGRAM STATUS:", r.status_code, r.text)

    except Exception as e:
        print("TELEGRAM ERROR:", e)


# ========= main =========
def main():
    symbols = env("SYMBOLS").split(",")
    interval = int(os.getenv("INTERVAL_SEC", "15"))

    client = StockHistoricalDataClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
    )

    # 🔔 رسالة تشغيل البوت
    send_telegram(
        "✅ Bot started (ALERTS ONLY)\n"
        f"📊 Symbols: {', '.join(symbols)}\n"
        f"⏱ Interval: {interval}s\n"
        f"🕒 Time: {datetime.now(timezone.utc)}"
    )

    print("BOT STARTED | symbols:", symbols)

    while True:
        try:
            for sym in symbols:
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    limit=3
                )

                bars = client.get_stock_bars(req).data.get(sym, [])

                if len(bars) < 2:
                    continue

                last = bars[-1]
                prev = bars[-2]

                if last.close > prev.close:
                    send_telegram(
                        f"📈 {sym} UP\n"
                        f"Price: {last.close}\n"
                        f"Time: {datetime.now(timezone.utc)}"
                    )

            time.sleep(interval)

        except Exception as e:
            send_telegram(f"⚠️ Bot error:\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
