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


def parse_symbols(raw: str) -> list[str]:
    # split + strip + remove empties + de-dup while preserving order
    items = []
    seen = set()
    for s in raw.split(","):
        s = s.strip().upper()
        if not s:
            continue
        if s not in seen:
            seen.add(s)
            items.append(s)
    return items


def send_telegram(msg: str):
    # لا تخلي فشل التليجرام يطيّح البوت
    try:
        token = env("TELEGRAM_BOT_TOKEN")
        chat_id = env("TELEGRAM_CHAT_ID")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=10)
    except Exception:
        # نتجاهل أي خطأ في التليجرام عشان البوت يكمل
        pass


# ========= main =========
def main():
    symbols = parse_symbols(env("SYMBOLS"))

    # ✅ إضافة MU تلقائياً لو ناقص (عشان طلبته)
    if "MU" not in symbols:
        symbols.append("MU")

    interval = int(os.getenv("INTERVAL_SEC", "15"))

    client = StockHistoricalDataClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
    )

    send_telegram(
        "✅ Bot started (ALERTS ONLY)\n"
        f"📊 Monitoring: {', '.join(symbols)}\n"
        f"⏱ Interval: {interval}s\n"
        f"🕒 Time(UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )

    # نخزن آخر اتجاه لكل سهم عشان ما نرسل تكرار
    # values: "UP" / "DOWN" / None
    last_dir: dict[str, str | None] = {s: None for s in symbols}

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

                direction = None
                if last.close > prev.close:
                    direction = "UP"
                elif last.close < prev.close:
                    direction = "DOWN"
                else:
                    # مساوي: لا نرسل شيء
                    continue

                # أرسل فقط إذا تغير الاتجاه أو أول مرة
                if last_dir.get(sym) != direction:
                    last_dir[sym] = direction

                    arrow = "📈" if direction == "UP" else "📉"
                    send_telegram(
                        f"{arrow} {sym} {direction}\n"
                        f"Price: {last.close}\n"
                        f"Prev:  {prev.close}\n"
                        f"Time(UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
                    )

            time.sleep(interval)

        except Exception as e:
            send_telegram(f"⚠️ Bot error:\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
