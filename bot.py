import os
import time
import math
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient


# ----------------------------
# helpers
# ----------------------------
def env_required(name: str) -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_str(name: str, default: str) -> str:
    v = os.getenv(name, default)
    return str(v).strip()


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return int(default)
    return int(str(v).strip())


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return float(default)
    return float(str(v).strip())


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS")
    if not raw or raw.strip() == "":
        # fallback safe list
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU", "MSFT"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


def send_telegram(text: str) -> None:
    token = env_required("TELEGRAM_BOT_TOKEN")
    chat_id = env_required("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text[:200]}")


# ----------------------------
# alpaca clients
# ----------------------------
def build_clients():
    api_key = env_required("APCA_API_KEY_ID")
    secret = env_required("APCA_API_SECRET_KEY")

    # TradingClient in alpaca-py supports paper=True/False
    paper = env_bool("PAPER", True)
    trading = TradingClient(api_key, secret, paper=paper)

    # Data client (DO NOT pass feed here in alpaca-py==0.27.0)
    data = StockHistoricalDataClient(api_key, secret)

    return data, trading, paper


# ----------------------------
# signal logic (simple & stable)
# ----------------------------
def compute_signal(df: pd.DataFrame):
    """
    df: minute bars with columns: close, volume (and datetime index)
    Returns dict or None
    """
    if df is None or len(df) < 25:
        return None

    close = df["close"].astype(float)
    vol = df["volume"].astype(float)

    price = float(close.iloc[-1])

    # MA over last 3 minutes
    ma_3 = float(close.tail(3).mean())

    diff_pct = (price - ma_3) / ma_3 if ma_3 != 0 else 0.0

    # baseline volume: average of previous 20 minutes (excluding last bar)
    baseline = float(vol.iloc[-21:-1].mean())
    last_vol = float(vol.iloc[-1])
    vol_ratio = (last_vol / baseline) if baseline > 0 else 0.0

    # thresholds (defaults chosen to be mild)
    min_diff = env_float("MIN_DIFF_PCT", 0.0010)      # 0.10%
    min_vol_ratio = env_float("MIN_VOL_RATIO", 1.35)  # x1.35

    direction = None
    if abs(diff_pct) >= min_diff and vol_ratio >= min_vol_ratio:
        direction = "LONG" if diff_pct > 0 else "SHORT"

    if not direction:
        return None

    return {
        "direction": direction,
        "price": price,
        "ma_3": ma_3,
        "diff_pct": diff_pct,
        "last_vol": last_vol,
        "baseline": baseline,
        "vol_ratio": vol_ratio,
    }


def format_msg(symbol: str, sig: dict) -> str:
    direction = sig["direction"]
    arrow = "🟢" if direction == "LONG" else "🔴"

    return (
        f"📣 Signal: {direction} | {symbol} {arrow}\n"
        f"Price: {sig['price']:.2f}\n"
        f"MA(3m): {sig['ma_3']:.2f}\n"
        f"Diff: {sig['diff_pct']*100:.2f}%\n"
        f"Volume Spike: {int(sig['last_vol'])} vs avg {int(sig['baseline'])} (x{sig['vol_ratio']:.2f})\n"
        f"Time(UTC): {now_utc_str()}"
    )


# ----------------------------
# main loop
# ----------------------------
def main():
    symbols = parse_symbols()
    interval_sec = env_int("INTERVAL_SEC", 15)

    # IMPORTANT: use IEX to avoid SIP subscription error
    data_feed = env_str("DATA_FEED", "iex").lower().strip()  # "iex"

    data, trading, paper = build_clients()

    # startup telegram
    send_telegram(
        "✅ Bot started (ALERTS)\n"
        f"symbols={','.join(symbols)} | interval={interval_sec}s | feed={data_feed.upper()} | paper={paper}\n"
        f"Time(UTC): {now_utc_str()}"
    )

    last_sent = {}  # symbol -> (direction, ts)

    while True:
        try:
            for sym in symbols:
                # request last ~60 minutes of 1-min bars
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    limit=60,
                    feed=data_feed,  # feed goes HERE (not in client init)
                )
                bars = data.get_stock_bars(req).df

                if bars is None or len(bars) == 0:
                    continue

                # when requesting single symbol, index may be multiindex; normalize
                if isinstance(bars.index, pd.MultiIndex):
                    bars_sym = bars.xs(sym)
                else:
                    bars_sym = bars

                sig = compute_signal(bars_sym)
                if not sig:
                    continue

                # anti-spam: don’t send same direction for same symbol within 2 minutes
                key = sym
                prev = last_sent.get(key)
                now_ts = time.time()
                if prev:
                    prev_dir, prev_ts = prev
                    if prev_dir == sig["direction"] and (now_ts - prev_ts) < 120:
                        continue

                send_telegram(format_msg(sym, sig))
                last_sent[key] = (sig["direction"], now_ts)

            time.sleep(interval_sec)

        except Exception as e:
            # send ONE error message, then sleep a bit to avoid spam
            err_txt = f"⚠️ Bot error: {type(e).__name__}: {str(e)}"
            try:
                send_telegram(err_txt)
            except Exception:
                pass
            # also print stack for Render logs
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    main()
