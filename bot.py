import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

import pytz

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# =========================
# Helpers (env)
# =========================
def env_any(*names: str, default: str | None = None) -> str:
    """Return first existing non-empty env var from names."""
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    if default is not None:
        return default
    raise RuntimeError(f"Missing env var (any of): {', '.join(names)}")


def env_float(name: str, default: str) -> float:
    v = os.getenv(name, default)
    return float(str(v).strip())


def env_int(name: str, default: str) -> int:
    v = os.getenv(name, default)
    return int(str(v).strip())


def env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =========================
# Telegram
# =========================
def send_telegram(text: str) -> None:
    token = env_any("TELEGRAM_BOT_TOKEN")
    chat_id = env_any("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    # لا نطيّح البوت بسبب تليجرام
    if r.status_code != 200:
        print("Telegram error:", r.status_code, r.text[:200])


# =========================
# Config
# =========================
def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS") or os.getenv("TICKERS") or ""
    raw = raw.strip()
    if not raw:
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU", "MSFT"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


# =========================
# Alpaca clients (DATA ONLY)
# =========================
def build_data_client() -> StockHistoricalDataClient:
    # يقبل الجديد أو القديم
    api_key = env_any("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = env_any("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

    # IMPORTANT: force IEX feed to avoid SIP subscription error
    # This alone fixes: "subscription does not permit querying recent SIP data"
    return StockHistoricalDataClient(api_key, secret, feed="iex")


# =========================
# Strategy (simple alert)
# =========================
def get_last_bar(client: StockHistoricalDataClient, symbol: str, minutes_back: int = 30):
    end = now_utc()
    start = end - timedelta(minutes=minutes_back)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="iex",  # force IEX here too
    )
    bars = client.get_stock_bars(req).data.get(symbol, [])
    if not bars:
        return None
    return bars[-1], bars


def format_signal(symbol: str, price: float, ma: float, diff_pct: float, vol: int, vol_avg: float, ts_utc: str) -> str:
    arrow = "⬆️" if diff_pct > 0 else "⬇️"
    side = "LONG" if diff_pct > 0 else "SHORT"
    strength = "OK"
    return (
        f"📣 Signal: {side} | {symbol}\n"
        f"Price: {price:.2f}\n"
        f"MA(5m): {ma:.2f}\n"
        f"Diff: {diff_pct*100:.2f}% {arrow}\n"
        f"Vol spike: {vol} vs avg {vol_avg:.0f}\n"
        f"Time(UTC): {ts_utc}"
    )


def main():
    # ---- user params ----
    symbols = parse_symbols()
    interval_sec = env_int("INTERVAL_SEC", "15")

    # thresholds
    min_diff = env_float("MIN_DIFF_PCT", "0.0010")        # 0.10%
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.4")     # volume spike factor
    recent_window_min = env_int("RECENT_WINDOW_MIN", "10")
    max_recent_move = env_float("MAX_RECENT_MOVE_PCT", "0.0030")  # 0.30%

    client = build_data_client()

    send_telegram(f"✅ Bot started (ALERTS) | symbols={','.join(symbols)} | interval={interval_sec}s | feed=IEX")

    last_sent = {}  # symbol -> last sent utc minute key

    while True:
        try:
            for sym in symbols:
                res = get_last_bar(client, sym, minutes_back=40)
                if not res:
                    continue
                last_bar, bars = res

                # last close price
                price = float(last_bar.close)

                # MA over last 5 minutes closes
                closes = [float(b.close) for b in bars[-5:]] if len(bars) >= 5 else [float(b.close) for b in bars]
                ma = sum(closes) / max(1, len(closes))
                diff = (price - ma) / ma if ma != 0 else 0.0

                # recent move (last N minutes range)
                window = bars[-recent_window_min:] if len(bars) >= recent_window_min else bars
                if window:
                    w_first = float(window[0].close)
                    w_last = float(window[-1].close)
                    recent_move = (w_last - w_first) / w_first if w_first != 0 else 0.0
                else:
                    recent_move = 0.0

                # volume spike baseline (avg of last 20 mins)
                vols = [int(getattr(b, "volume", 0) or 0) for b in bars]
                vol = int(getattr(last_bar, "volume", 0) or 0)
                baseline = vols[-20:] if len(vols) >= 20 else vols
                vol_avg = (sum(baseline) / max(1, len(baseline))) if baseline else 0.0
                vol_ratio = (vol / vol_avg) if vol_avg > 0 else 0.0

                # filters
                if abs(diff) < min_diff:
                    continue
                if vol_ratio < min_vol_ratio:
                    continue
                if abs(recent_move) > max_recent_move:
                    continue

                # avoid spamming: one per minute per symbol
                minute_key = now_utc().strftime("%Y-%m-%d %H:%M")
                if last_sent.get(sym) == minute_key:
                    continue
                last_sent[sym] = minute_key

                msg = format_signal(
                    sym,
                    price=price,
                    ma=ma,
                    diff_pct=diff,
                    vol=vol,
                    vol_avg=vol_avg,
                    ts_utc=now_utc().strftime("%Y-%m-%d %H:%M:%S"),
                )
                send_telegram(msg)

        except Exception as e:
            # لا نطيّح البوت، بس نبلّغ ونكمل
            send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
