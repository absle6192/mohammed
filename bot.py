import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# ----------------- ENV helpers -----------------
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v and v.strip() else float(default)


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v and v.strip() else int(default)


def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ----------------- Telegram -----------------
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
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text[:200]}")


# ----------------- Symbols -----------------
def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS", "TSLA,AAPL,NVDA,AMD,AMZN,GOOGL,MU,MSFT")
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


# ----------------- Candle filter (LIGHT) -----------------
def candle_filter_light(df: pd.DataFrame) -> tuple[bool, str]:
    """
    فلتر خفيف جدًا:
    - آخر شمعة: Close > Open
    - جسم الشمعة ليس صغير جداً (body >= 35% من المدى)
    """
    if df is None or len(df) < 2:
        return False, "no_data"

    last = df.iloc[-1]
    o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng

    if c <= o:
        return False, "red_candle"
    if body_ratio < 0.35:
        return False, f"weak_body({body_ratio:.2f})"

    return True, "PASS"


# ----------------- Alpaca data -----------------
def build_data_client() -> StockHistoricalDataClient:
    api_key = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")
    return StockHistoricalDataClient(api_key, secret)


def get_bars(client: StockHistoricalDataClient, symbol: str, minutes: int) -> pd.DataFrame:
    """
    نجلب 1Min bars لآخر N دقائق.
    مهم: حسابات كثيرة ما تقدر تستخدم SIP، فنحاول IEX أولاً.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes + 5)

    # جرّب تمرير feed=IEX إذا متاح في إصدارك
    req_kwargs = dict(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        limit=minutes + 5,
    )

    # feed handling (compatible)
    try:
        from alpaca.data.enums import DataFeed  # type: ignore
        req_kwargs["feed"] = DataFeed.IEX
    except Exception:
        # بعض الإصدارات قد تقبل feed كسلسلة
        req_kwargs["feed"] = "iex"

    req = StockBarsRequest(**req_kwargs)

    bars = client.get_stock_bars(req).df
    if bars is None or len(bars) == 0:
        return pd.DataFrame()

    # إذا رجعت MultiIndex (symbol, timestamp)
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol)

    bars = bars.reset_index()

    # توحيد الأعمدة
    out = pd.DataFrame({
        "ts": bars["timestamp"],
        "open": bars["open"],
        "high": bars["high"],
        "low": bars["low"],
        "close": bars["close"],
        "volume": bars["volume"],
    }).dropna()

    return out


# ----------------- Signal logic -----------------
def compute_signal(df: pd.DataFrame, ma_minutes: int, recent_window_min: int) -> dict | None:
    if df is None or len(df) < max(ma_minutes, recent_window_min) + 2:
        return None

    closes = df["close"].astype(float)
    vols = df["volume"].astype(float)

    price = float(closes.iloc[-1])

    ma = float(closes.tail(ma_minutes).mean())
    diff = (price - ma) / ma if ma != 0 else 0.0

    recent = closes.tail(recent_window_min + 1)
    recent_move = float((recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0]) if float(recent.iloc[0]) != 0 else 0.0

    # volume spike baseline: متوسط حجم آخر (recent_window_min) شموع بدون آخر شمعة
    baseline = float(vols.tail(recent_window_min + 1).iloc[:-1].mean())
    last_vol = float(vols.iloc[-1])
    vol_ratio = (last_vol / baseline) if baseline > 0 else 0.0

    return {
        "price": price,
        "ma": ma,
        "diff": diff,
        "recent_move": recent_move,
        "last_vol": last_vol,
        "baseline": baseline,
        "vol_ratio": vol_ratio,
    }


def format_alert(symbol: str, sig: dict, candle_pass: bool, candle_msg: str, candle_mode: str) -> str:
    price = sig["price"]
    ma = sig["ma"]
    diff = sig["diff"]
    vol_ratio = sig["vol_ratio"]
    last_vol = sig["last_vol"]
    baseline = sig["baseline"]
    recent_move = sig["recent_move"]

    # اتجاه الإشارة (ALERT فقط)
    side = "LONG" if diff > 0 else "SHORT"

    candle_line = ""
    if candle_mode != "OFF":
        candle_line = f"\n🕯 Candle Filter ({candle_mode}):\n{'✅ PASS' if candle_pass else '❌ FAIL'} ({candle_msg})"

    txt = (
        f"📣 Signal: {side} | {symbol}\n"
        f"Price: {price:.2f}\n"
        f"MA({int(os.getenv('MA_MIN', '3'))}m): {ma:.2f}\n"
        f"Diff: {diff*100:.2f}%\n"
        f"Volume Spike: {last_vol:.0f} vs avg {baseline:.0f} (x{vol_ratio:.2f})\n"
        f"Recent Move ({int(os.getenv('RECENT_WINDOW_MIN', '10'))}m): {recent_move*100:.2f}%"
        f"{candle_line}\n"
        f"Time(UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
    )
    return txt


# ----------------- Main loop (ALERTS only) -----------------
def main() -> None:
    # settings (defaults)
    interval_sec = env_int("INTERVAL_SEC", 15)
    ma_min = env_int("MA_MIN", 3)
    recent_window_min = env_int("RECENT_WINDOW_MIN", 10)

    min_diff = env_float("MIN_DIFF_PCT", 0.0010)       # 0.10%
    max_diff = env_float("MAX_DIFF_PCT", 0.0030)       # 0.30%
    min_vol_ratio = env_float("MIN_VOL_RATIO", 1.4)    # 1.4x

    candle_mode = os.getenv("CANDLE_FILTER", "LIGHT").strip().upper()  # LIGHT / OFF

    symbols = parse_symbols()

    client = build_data_client()

    send_telegram(
        f"✅ Bot started (ALERTS)\n"
        f"symbols={','.join(symbols)} | interval={interval_sec}s | candle={candle_mode}\n"
        f"min_diff={min_diff} max_diff={max_diff} min_vol_ratio={min_vol_ratio}"
    )

    last_sent = {}  # symbol -> timestamp

    while True:
        for sym in symbols:
            try:
                df = get_bars(client, sym, minutes=max(60, recent_window_min + 10))
                if df.empty:
                    continue

                sig = compute_signal(df, ma_minutes=ma_min, recent_window_min=recent_window_min)
                if not sig:
                    continue

                diff_abs = abs(float(sig["diff"]))
                vol_ratio = float(sig["vol_ratio"])

                # شروط الإشارة الأساسية
                if diff_abs < min_diff or diff_abs > max_diff:
                    continue
                if vol_ratio < min_vol_ratio:
                    continue

                # Candle filter
                candle_pass, candle_msg = True, "OFF"
                if candle_mode != "OFF":
                    candle_pass, candle_msg = candle_filter_light(df)
                    if not candle_pass:
                        continue

                # منع تكرار نفس السهم بسرعة (تبريد 60 ثانية)
                now_ts = time.time()
                if sym in last_sent and (now_ts - last_sent[sym]) < 60:
                    continue

                msg = format_alert(sym, sig, candle_pass, candle_msg, candle_mode)
                send_telegram(msg)
                last_sent[sym] = now_ts

            except Exception as e:
                # خطأ واحد لا يطيّح البوت كله
                try:
                    send_telegram(f"⚠️ Bot error ({sym}): {type(e).__name__}: {str(e)[:180]}")
                except Exception:
                    pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
