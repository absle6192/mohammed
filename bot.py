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
    return str(v).strip()


def send_telegram(msg: str) -> bool:
    """
    Returns True if message sent successfully, else False.
    Writes failure reason to stdout so Render Application logs show it.
    """
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "disable_web_page_preview": True,
        # إذا تبي بدون صوت، حط TELEGRAM_SILENT=1 في env
        "disable_notification": env("TELEGRAM_SILENT", "0") in ("1", "true", "True"),
    }

    last_err = None
    for attempt in range(1, 4):  # 3 retries
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return True

            # Telegram returns useful JSON error
            last_err = f"HTTP {r.status_code} | {r.text}"
            print(f"[TELEGRAM] send failed attempt={attempt}: {last_err}", flush=True)
            time.sleep(1.5)

        except Exception as e:
            last_err = repr(e)
            print(f"[TELEGRAM] exception attempt={attempt}: {last_err}", flush=True)
            time.sleep(1.5)

    print(f"[TELEGRAM] giving up: {last_err}", flush=True)
    return False


# ========= main =========
def main():
    symbols = [s.strip().upper() for s in env("SYMBOLS").split(",") if s.strip()]
    interval = int(env("INTERVAL_SEC", "15"))

    print(f"[BOOT] starting... symbols={symbols} interval={interval}s", flush=True)

    client = StockHistoricalDataClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
    )

    # ✅ Startup ping (the one you expect)
    ok = send_telegram(
        "✅ Bot started (ALERTS ONLY)\n"
        f"symbols={','.join(symbols)}\n"
        f"interval={interval}s\n"
        f"time(utc)={datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )
    print(f"[BOOT] startup telegram sent={ok}", flush=True)

    while True:
        try:
            for sym in symbols:
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    limit=3,
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
                        f"Time(UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
                    )

            time.sleep(interval)

        except Exception as e:
            print(f"[ERROR] loop exception: {repr(e)}", flush=True)
            send_telegram(f"⚠️ Bot error:\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
