import os
import time
import requests
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


# =========================
# Helpers
# =========================

def env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and (v is None or str(v).strip() == ""):
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip() if v is not None else None


def env_float(name, default, required=False):
    return float(env(name, default, required))


def env_int(name, default, required=False):
    return int(env(name, default, required))


def now_utc():
    return datetime.now(timezone.utc)


# =========================
# Telegram
# =========================

def send_telegram(text: str):
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    chat_id = env("TELEGRAM_CHAT_ID", required=True)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    r = requests.post(url, json=payload, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.text}")


# =========================
# Config
# =========================

SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", "TSLA,AAPL,NVDA,AMD,AMZN,GOOGL,MU,MSFT").split(",")]

INTERVAL_SEC = env_int("INTERVAL_SEC", "15")
MIN_VOL_RATIO = env_float("MIN_VOL_RATIO", "1.4")
MAX_MOVE = env_float("MAX_JUMP_PCT", "0.0030")
MIN_MOVE = env_float("MIN_DIFF_PCT", "0.0010")

MODE = env("MODE", "ALERTS").upper()


# =========================
# Alpaca Data Client
# =========================

def build_data_client():
    api_key = env("APCA_API_KEY_ID", required=True)
    secret = env("APCA_API_SECRET_KEY", required=True)

    # ❗ لا feed هنا
    return StockHistoricalDataClient(api_key, secret)


# =========================
# Logic
# =========================

def check_symbol(client: StockHistoricalDataClient, symbol: str):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        limit=6,
        feed=DataFeed.IEX,   # ✅ هنا فقط
    )

    bars = client.get_stock_bars(req).data.get(symbol, [])
    if len(bars) < 6:
        return

    last = bars[-1]
    prev = bars[-2]

    price = last.close
    ma = sum(b.close for b in bars[-5:]) / 5
    diff_pct = (price - ma) / ma

    vol_avg = sum(b.volume for b in bars[-6:-1]) / 5
    vol_ratio = last.volume / max(vol_avg, 1)

    if abs(diff_pct) < MIN_MOVE:
        return
    if abs(diff_pct) > MAX_MOVE:
        return
    if vol_ratio < MIN_VOL_RATIO:
        return

    direction = "LONG" if diff_pct > 0 else "SHORT"

    msg = (
        f"📣 Signal: {direction} | {symbol}\n"
        f"Price: {price:.2f}\n"
        f"MA(5m): {ma:.2f}\n"
        f"Diff: {diff_pct*100:.2f}%\n"
        f"Volume spike: x{vol_ratio:.2f}\n"
        f"Time (UTC): {now_utc().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    send_telegram(msg)


# =========================
# Main
# =========================

def main():
    client = build_data_client()

    send_telegram(
        f"✅ Bot started (ALERTS)\n"
        f"symbols={','.join(SYMBOLS)} | interval={INTERVAL_SEC}s | feed=IEX"
    )

    while True:
        for sym in SYMBOLS:
            try:
                check_symbol(client, sym)
            except Exception as e:
                send_telegram(f"⚠️ Bot error ({sym}): {e}")

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
