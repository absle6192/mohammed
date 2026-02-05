import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient


# =========================
# Helpers
# =========================
def _getenv_any(*names: str, default: str | None = None) -> str | None:
    """Return first non-empty env var among names."""
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def env_required_any(*names: str) -> str:
    v = _getenv_any(*names)
    if not v:
        raise RuntimeError(f"Missing env var. Set one of: {', '.join(names)}")
    return v


def env_float(name: str, default: str) -> float:
    v = os.getenv(name, default)
    try:
        return float(str(v).strip())
    except Exception:
        raise RuntimeError(f"Invalid float for {name}: {v}")


def env_int(name: str, default: str) -> int:
    v = os.getenv(name, default)
    try:
        return int(str(v).strip())
    except Exception:
        raise RuntimeError(f"Invalid int for {name}: {v}")


def env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS") or os.getenv("TICKERS") or ""
    raw = raw.strip()
    if not raw:
        # fallback list (عدّلها من Render)
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


def send_telegram(text: str) -> None:
    token = env_required_any("TELEGRAM_BOT_TOKEN")
    chat_id = env_required_any("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:200]}")


# =========================
# Alpaca clients (IMPORTANT FIX)
# =========================
def build_clients() -> tuple[StockHistoricalDataClient, TradingClient]:
    """
    يقبل المفاتيح بأي صيغة من هذي:
    - الجديد: ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL
    - القديم (اللي عندك): APCA_API_KEY_ID / APCA_API_SECRET_KEY / APCA_API_BASE_URL
    """

    api_key = env_required_any("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = env_required_any("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

    # base_url ما يحتاجه TradingClient عادة (paper param يكفي)، بس نخليه للوضوح
    base_url = _getenv_any("ALPACA_BASE_URL", "APCA_API_BASE_URL", default="https://paper-api.alpaca.markets")

    paper_flag = env_bool("ALPACA_PAPER", "false") or ("paper" in base_url)

    hist = StockHistoricalDataClient(api_key, secret)
    trading = TradingClient(api_key, secret, paper=paper_flag)

    return hist, trading


# =========================
# Strategy (alerts only)
# =========================
def get_bars(hist: StockHistoricalDataClient, symbol: str, minutes: int) -> list:
    # آخر N دقائق (1Min bars)
    end = now_utc()
    start = end - timedelta(minutes=minutes + 5)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        limit=minutes + 5,
        adjustment="raw",
    )
    bars = hist.get_stock_bars(req).data.get(symbol, [])
    return bars


def safe_spread_pct(last_price: float) -> float:
    # ما عندنا bid/ask هنا، فنحط 0 كافتراضي (أو تقدر تربطه بـ quote لاحقاً)
    # نخلي المتغير موجود عشان لا يطيّح الكود
    return 0.0


def compute_signal(symbol: str, bars: list, min_diff_pct: float, min_vol_ratio: float,
                   vol_avg_window: int, max_jump_pct: float) -> tuple[str | None, dict]:
    """
    يرجع ("LONG"/"SHORT"/None, info)
    """
    if len(bars) < max(6, vol_avg_window + 2):
        return None, {}

    closes = [float(b.close) for b in bars]
    vols = [float(b.volume) for b in bars]

    last = closes[-1]
    ma5 = sum(closes[-5:]) / 5.0

    diff_pct = (last - ma5) / ma5 if ma5 != 0 else 0.0

    recent_vol = vols[-1]
    avg_vol = sum(vols[-vol_avg_window:]) / float(vol_avg_window)

    vol_ratio = (recent_vol / avg_vol) if avg_vol > 0 else 0.0

    # jump: حركة آخر 5 دقائق
    prev = closes[-6]
    jump_pct = abs((last - prev) / prev) if prev != 0 else 0.0

    side = None
    if diff_pct >= min_diff_pct and vol_ratio >= min_vol_ratio and jump_pct <= max_jump_pct:
        side = "LONG"
    elif diff_pct <= -min_diff_pct and vol_ratio >= min_vol_ratio and jump_pct <= max_jump_pct:
        side = "SHORT"

    info = {
        "price": last,
        "ma5": ma5,
        "diff_pct": diff_pct,
        "recent_vol": recent_vol,
        "avg_vol": avg_vol,
        "vol_ratio": vol_ratio,
        "jump_pct": jump_pct,
        "time_utc": now_utc().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return side, info


# =========================
# Main loop
# =========================
def main() -> None:
    # Modes: ALERTS فقط (بدون تداول)
    mode = (os.getenv("MODE") or "ALERTS").strip().upper()
    symbols = parse_symbols()

    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.4")
    min_diff_pct = env_float("MIN_DIFF_PCT", "0.0010")        # 0.10%
    vol_avg_window = env_int("VOL_AVG_WINDOW", "20")
    max_jump_pct = env_float("MAX_JUMP_PCT", "0.0030")         # 0.30%

    interval_sec = env_int("INTERVAL_SEC", "15")
    cooldown_sec = env_int("COOLDOWN_SEC", "60")

    hist, _trading = build_clients()

    send_telegram(f"✅ Bot started ({mode}) | symbols={','.join(symbols)} | interval={interval_sec}s")

    last_sent: dict[tuple[str, str], float] = {}

    while True:
        try:
            for sym in symbols:
                bars = get_bars(hist, sym, minutes=30)
                side, info = compute_signal(
                    sym, bars,
                    min_diff_pct=min_diff_pct,
                    min_vol_ratio=min_vol_ratio,
                    vol_avg_window=vol_avg_window,
                    max_jump_pct=max_jump_pct,
                )
                if not side:
                    continue

                key = (sym, side)
                now_ts = time.time()
                if now_ts - last_sent.get(key, 0) < cooldown_sec:
                    continue
                last_sent[key] = now_ts

                msg = (
                    f"📣 Signal: {side} | {sym}\n"
                    f"Price: {info['price']:.2f}\n"
                    f"MA(5m): {info['ma5']:.2f}\n"
                    f"Diff: {info['diff_pct']*100:.2f}%\n"
                    f"Vol spike: {int(info['recent_vol'])} vs avg {int(info['avg_vol'])} (x{info['vol_ratio']:.2f})\n"
                    f"Jump(5m): {info['jump_pct']*100:.2f}%\n"
                    f"Time(UTC): {info['time_utc']}"
                )
                send_telegram(msg)

        except Exception as e:
            # لا نطيح الخدمة، نرسل الخطأ و نكمل
            try:
                send_telegram(f"⚠️ Bot error: {type(e).__name__}: {str(e)[:180]}")
            except Exception:
                pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
