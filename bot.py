import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def pct(a: float, b: float) -> float:
    # (a-b)/b
    if b == 0:
        return 0.0
    return (a - b) / b


def fmt_pct(x: float) -> str:
    return f"{x*100:.2f}%"


def main():
    base_url = env("APCA_API_BASE_URL")
    key_id = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")

    tickers = [t.strip().upper() for t in env("TICKERS").split(",") if t.strip()]
    interval_sec = int(env("INTERVAL_SEC", "20"))
    lookback_min = int(env("LOOKBACK_MIN", "5"))
    thresh_pct = float(env("THRESH_PCT", "0.003"))     # 0.003 = 0.30%
    volume_mult = float(env("VOLUME_MULT", "1.8"))     # 1.8x volume spike
    cooldown_min = int(env("COOLDOWN_MIN", "10"))

    client = StockHistoricalDataClient(key_id, secret)

    # cooldown memory
    last_signal_time: dict[str, datetime] = {}
    last_signal_side: dict[str, str] = {}  # "LONG" / "SHORT"

    send_telegram(f"✅ Bot started. Watching: {', '.join(tickers)}")

    while True:
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=lookback_min + 2)  # buffer
            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame.Minute,
                start=start,
                end=now,
                feed="iex",  # works for most accounts; if you have SIP you can change
            )
            bars = client.get_stock_bars(req).df  # MultiIndex: (symbol, timestamp)
            if bars is None or len(bars) == 0:
                time.sleep(interval_sec)
                continue

            for sym in tickers:
                try:
                    df = bars.xs(sym, level=0).copy()
                except Exception:
                    continue

                # keep only last N minutes
                df = df.sort_index()
                df = df.tail(lookback_min)

                if len(df) < max(3, lookback_min - 1):
                    continue

                # Price now = last close
                price_now = float(df["close"].iloc[-1])

                # Simple average of closes (you can change to VWAP)
                ma = float(df["close"].mean())

                # volume spike: last volume vs avg volume
                vol_last = float(df["volume"].iloc[-1])
                vol_avg = float(df["volume"].mean())
                vol_ok = (vol_avg > 0) and (vol_last >= vol_avg * volume_mult)

                d = pct(price_now, ma)

                side = None
                if d >= thresh_pct and vol_ok:
                    side = "LONG"
                elif d <= -thresh_pct and vol_ok:
                    side = "SHORT"

                if side is None:
                    continue

                # cooldown
                last_t = last_signal_time.get(sym)
                if last_t and (now - last_t) < timedelta(minutes=cooldown_min):
                    continue

                # avoid repeating same side too often
                if last_signal_side.get(sym) == side and last_t and (now - last_t) < timedelta(minutes=cooldown_min * 2):
                    continue

                msg = (
                    f"📣 Signal: {side}  |  {sym}\n"
                    f"Price: {price_now:.2f}\n"
                    f"MA({lookback_min}m): {ma:.2f}\n"
                    f"Diff: {fmt_pct(d)}\n"
                    f"Vol spike: {vol_last:.0f} vs avg {vol_avg:.0f} (x{(vol_last/vol_avg if vol_avg else 0):.2f})\n"
                    f"Time(UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                send_telegram(msg)

                last_signal_time[sym] = now
                last_signal_side[sym] = side

        except Exception as e:
            # log to telegram مرة واحدة كل فترة لو تبي - هنا نخليها مختصرة
            try:
                send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
