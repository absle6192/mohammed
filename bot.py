import os
import time
import math
import requests
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# Try to use DataFeed if available (alpaca-py versions differ)
try:
    from alpaca.data.enums import DataFeed  # type: ignore
except Exception:
    DataFeed = None  # fallback


# ----------------- env helpers -----------------
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return float(default)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return int(default)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS", "").strip()
    if not raw:
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU", "MSFT"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


# ----------------- telegram -----------------
def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:200]}")


# ----------------- alpaca data -----------------
def build_data_client() -> StockHistoricalDataClient:
    key = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")
    # IMPORTANT: Do NOT pass feed=... here (caused your error before).
    return StockHistoricalDataClient(key, secret)


def get_1m_bars(client: StockHistoricalDataClient, symbol: str, limit: int = 30):
    req_kwargs = dict(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        limit=limit,
    )
    # Put feed on request if supported, NOT on client init
    if DataFeed is not None:
        req_kwargs["feed"] = DataFeed.IEX  # avoids SIP subscription issue
    else:
        # Some versions accept string feed
        req_kwargs["feed"] = "iex"

    req = StockBarsRequest(**req_kwargs)
    bars = client.get_stock_bars(req).data.get(symbol, [])
    return bars


# ----------------- signal logic (simple) -----------------
def analyze_symbol(bars) -> dict | None:
    if not bars or len(bars) < 10:
        return None

    # Latest close
    last = bars[-1]
    price = float(getattr(last, "close"))

    # MA over last 3 minutes (like your telegram MA(3m))
    ma_window = env_int("MA_MIN", 3)
    if len(bars) < ma_window:
        return None
    ma = sum(float(getattr(b, "close")) for b in bars[-ma_window:]) / ma_window

    diff_pct = (price - ma) / ma if ma != 0 else 0.0

    # Volume spike vs baseline
    base_n = env_int("VOL_BASELINE_BARS", 20)
    base_n = min(base_n, len(bars) - 1)
    base_vol = [float(getattr(b, "volume")) for b in bars[-(base_n + 1):-1]]
    avg_vol = (sum(base_vol) / len(base_vol)) if base_vol else 0.0
    last_vol = float(getattr(last, "volume"))
    vol_mult = (last_vol / avg_vol) if avg_vol > 0 else 0.0

    # Recent move (last 10 bars by default)
    recent_n = env_int("RECENT_BARS", 10)
    recent_n = min(recent_n, len(bars) - 1)
    old = bars[-(recent_n + 1)]
    old_close = float(getattr(old, "close"))
    recent_move = (price - old_close) / old_close if old_close != 0 else 0.0

    return {
        "price": price,
        "ma": ma,
        "diff_pct": diff_pct,
        "avg_vol": avg_vol,
        "last_vol": last_vol,
        "vol_mult": vol_mult,
        "recent_move": recent_move,
    }


def pick_signal(stats: dict) -> str | None:
    min_diff = env_float("MIN_DIFF_PCT", 0.0010)      # 0.10%
    min_vol = env_float("MIN_VOL_MULT", 1.4)          # 1.4x
    max_recent = env_float("MAX_RECENT_MOVE_PCT", 0.0030)  # 0.30%

    diff = stats["diff_pct"]
    volm = stats["vol_mult"]
    recent = abs(stats["recent_move"])

    if volm < min_vol:
        return None
    if recent > max_recent:
        return None

    if diff >= min_diff:
        return "LONG"
    if diff <= -min_diff:
        return "SHORT"
    return None


def format_alert(symbol: str, side: str, stats: dict) -> str:
    ts = now_utc().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📣 Signal: {side} | {symbol}\n"
        f"Price: {stats['price']:.2f}\n"
        f"MA({env_int('MA_MIN', 3)}m): {stats['ma']:.2f}\n"
        f"Diff: {stats['diff_pct']*100:.2f}%\n"
        f"Volume Spike: {int(stats['last_vol'])} vs avg {int(stats['avg_vol'])} (x{stats['vol_mult']:.2f})\n"
        f"Recent Move({env_int('RECENT_BARS', 10)} bars): {stats['recent_move']*100:.2f}%\n"
        f"Time(UTC): {ts}"
    )


def main():
    symbols = parse_symbols()
    interval = env_int("INTERVAL_SEC", 15)

    client = build_data_client()

    # Startup message (NO trading.paper هنا، ولا TradingClient أصلاً)
    send_telegram(f"✅ Bot started (ALERTS) | symbols={','.join(symbols)} | interval={interval}s | feed=IEX")

    last_sent: dict[str, float] = {}
    cooldown = env_int("COOLDOWN_SEC", 60)

    while True:
        for sym in symbols:
            try:
                bars = get_1m_bars(client, sym, limit=30)
                stats = analyze_symbol(bars)
                if not stats:
                    continue

                side = pick_signal(stats)
                if not side:
                    continue

                now_ts = time.time()
                prev = last_sent.get(sym, 0.0)
                if now_ts - prev < cooldown:
                    continue

                send_telegram(format_alert(sym, side, stats))
                last_sent[sym] = now_ts

            except Exception as e:
                # Send a short error once per symbol cooldown to avoid spam
                now_ts = time.time()
                prev = last_sent.get(f"err:{sym}", 0.0)
                if now_ts - prev > cooldown:
                    send_telegram(f"⚠️ Bot error ({sym}): {type(e).__name__}: {str(e)[:180]}")
                    last_sent[f"err:{sym}"] = now_ts

        time.sleep(interval)


if __name__ == "__main__":
    main()
