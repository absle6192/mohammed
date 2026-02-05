import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# -----------------------------
# ENV helpers
# -----------------------------
def env_get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()

def env_bool(name: str, default: str = "false") -> bool:
    v = env_get(name, default) or default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def env_float(name: str, default: str) -> float:
    return float(env_get(name, default) or default)

def env_int(name: str, default: str) -> int:
    return int(env_get(name, default) or default)

def env_required_any(*names: str) -> str:
    for n in names:
        v = env_get(n)
        if v:
            return v
    raise RuntimeError(f"Missing env var. Set one of: {', '.join(names)}")

# -----------------------------
# Telegram
# -----------------------------
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
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")

# -----------------------------
# Symbols
# -----------------------------
def parse_symbols() -> List[str]:
    raw = env_get("SYMBOLS") or env_get("TICKERS") or ""
    raw = raw.strip()
    if not raw:
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU", "MSFT"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]

# -----------------------------
# Candle filter (LIGHT)
# -----------------------------
def candle_pass_light(o: float, h: float, l: float, c: float, side: str) -> bool:
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    # لازم جسم واضح
    if body_ratio < 0.35:
        return False

    if side == "LONG":
        # لازم إغلاق فوق الافتتاح
        if c <= o:
            return False
        # لا نبي ظل علوي كبير
        if upper_ratio > 0.45:
            return False
        return True

    if side == "SHORT":
        if c >= o:
            return False
        if lower_ratio > 0.45:
            return False
        return True

    return False

# -----------------------------
# Alpaca clients (FIXED)
# - يدعم ALPACA_* أو APCA_*
# - بدون base_url (تسبب الخطأ)
# - يجبر feed=IEX لتفادي SIP
# -----------------------------
def build_clients() -> Tuple[StockHistoricalDataClient, TradingClient]:
    api_key = env_required_any("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret  = env_required_any("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

    paper = env_bool("ALPACA_PAPER", "true")
    # TradingClient: لا نستخدم base_url نهائيًا
    trading = TradingClient(api_key, secret, paper=paper)

    hist = StockHistoricalDataClient(api_key, secret)
    return hist, trading

# -----------------------------
# Signal logic
# -----------------------------
def fmt_pct(x: float) -> str:
    return f"{x*100:.2f}%"

def main():
    mode = (env_get("MODE", "ALERTS") or "ALERTS").strip().upper()
    auto_trade = env_bool("AUTO_TRADE", "off")  # لازم ON عشان يتداول
    interval_sec = env_int("INTERVAL_SEC", "15")

    ma_minutes = env_int("MA_MIN", "3")  # MA(3m)
    recent_window_min = env_int("RECENT_WINDOW_MIN", "10")

    min_diff_pct = env_float("MIN_DIFF_PCT", "0.0010")      # 0.10%
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.4")       # x1.4
    max_recent_move_pct = env_float("MAX_RECENT_MOVE_PCT", "0.0030")  # 0.30%

    usd_per_trade = env_float("USD_PER_TRADE", "2000")
    candle_filter = (env_get("CANDLE_FILTER", "LIGHT") or "LIGHT").strip().upper()

    # IMPORTANT: اجبار IEX
    data_feed = (env_get("DATA_FEED", "IEX") or "IEX").strip().upper()

    symbols = parse_symbols()
    hist, trading = build_clients()

    send_telegram(
        f"✅ Bot started ({mode}) | symbols={','.join(symbols)} | interval={interval_sec}s | "
        f"feed={data_feed} | paper={trading.paper} | candle={candle_filter} | auto_trade={auto_trade}"
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=max(recent_window_min, ma_minutes) + 5)

            for sym in symbols:
                req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    start=start,
                    end=now,
                    feed=data_feed.lower(),  # "iex" مهم
                )

                bars = hist.get_stock_bars(req).data.get(sym, [])
                if len(bars) < max(ma_minutes + 2, 6):
                    continue

                # آخر شمعة
                b_last = bars[-1]
                o, h, l, c = float(b_last.open), float(b_last.high), float(b_last.low), float(b_last.close)
                v_last = float(b_last.volume)

                # MA(3m) من آخر ma_minutes إغلاقات
                closes = [float(b.close) for b in bars[-ma_minutes:]]
                ma = sum(closes) / len(closes)

                diff = (c - ma) / ma if ma != 0 else 0.0

                # متوسط الفوليوم baseline (آخر recent_window_min بدون آخر شمعة)
                recent = bars[-(recent_window_min + 1):-1] if len(bars) >= recent_window_min + 1 else bars[:-1]
                vols = [float(b.volume) for b in recent] if recent else [v_last]
                vol_avg = sum(vols) / len(vols) if vols else v_last
                vol_ratio = (v_last / vol_avg) if vol_avg > 0 else 0.0

                # Recent move (تقريب: من أول close في نافذة recent إلى آخر close)
                recent2 = bars[-(recent_window_min + 1):]
                c0 = float(recent2[0].close)
                recent_move = (c - c0) / c0 if c0 != 0 else 0.0

                # side decision
                side = None
                if diff >= min_diff_pct:
                    side = "LONG"
                elif diff <= -min_diff_pct:
                    side = "SHORT"
                else:
                    continue

                # فلتر الحركة الأخيرة
                if abs(recent_move) > max_recent_move_pct:
                    continue

                # فلتر الفوليوم
                if vol_ratio < min_vol_ratio:
                    continue

                # Candle filter
                candle_ok = True
                if candle_filter == "LIGHT":
                    candle_ok = candle_pass_light(o, h, l, c, side)
                if not candle_ok:
                    continue

                strength = "متوسطة ✅ (OK)" if vol_ratio < (min_vol_ratio + 1.0) else "قوية 🔥"
                text = (
                    f"📣 Signal: {side} | {sym}\n"
                    f"Price: {c:.2f}\n"
                    f"MA({ma_minutes}m): {ma:.2f}\n"
                    f"Diff: {fmt_pct(diff)}\n"
                    f"Volume Spike (baseline): {int(v_last)} vs avg {int(vol_avg)} (x{vol_ratio:.2f})\n"
                    f"Recent Move ({recent_window_min}m): {fmt_pct(recent_move)}\n"
                    f"🕯️ Candle Filter ({candle_filter}): {'PASS ✅' if candle_ok else 'FAIL ❌'}\n"
                    f"⭐ Strength: {strength}\n"
                    f"Time (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}"
                )
                send_telegram(text)

                # تداول؟ فقط لو AUTO_TRADE=on و MODE=TRADE
                if mode == "TRADE" and auto_trade:
                    qty = max(int(usd_per_trade / c), 1)
                    order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
                    req_order = MarketOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=order_side,
                        time_in_force=TimeInForce.DAY,
                    )
                    trading.submit_order(req_order)
                    send_telegram(f"✅ Order sent: {side} {sym} qty={qty} (paper={trading.paper})")

        except Exception as e:
            # لو طلع SIP error أو أي خطأ، نرسل رسالة واحدة
            send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")

        time.sleep(interval_sec)

if __name__ == "__main__":
    main()
