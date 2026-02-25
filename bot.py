import os
import asyncio
import logging
import requests
from collections import deque
from datetime import datetime, timezone

import pandas as pd

from alpaca.data.live import StockDataStream

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ===================== helpers =====================
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

def env_int(name: str, default: str) -> int:
    return int(env(name, default))

def env_float(name: str, default: str) -> float:
    return float(env(name, default))

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram Error: {e}")

def calculate_rsi(series: pd.Series, window: int) -> float | None:
    # RSI بسيط على سلسلة أسعار لحظية
    if series is None or len(series) < window + 2:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    v = rsi.iloc[-1]
    if pd.isna(v):
        return None
    return float(v)

def mean_last_n(series: pd.Series, n: int) -> float | None:
    if series is None or len(series) < n:
        return None
    v = series.iloc[-n:].mean()
    if pd.isna(v):
        return None
    return float(v)

# ===================== main =====================
async def main():
    # --- ENV ---
    API_KEY = env("APCA_API_KEY_ID")
    SECRET_KEY = env("APCA_API_SECRET_KEY")

    TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = env("TELEGRAM_CHAT_ID")

    TICKERS = [t.strip().upper() for t in env("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]
    FEED = env("DATA_FEED", "iex").lower()  # iex غالباً للحسابات المجانية

    # --- لحظي 30 ثانية ---
    PRICE_WINDOW_SEC = env_int("PRICE_WINDOW_SEC", "30")     # آخر كم ثانية نبني عليها RSI/MA
    RSI_WINDOW = env_int("RSI_WINDOW", "14")                 # عدد نقاط RSI (على سلسلة لحظية)
    MA_POINTS = env_int("MA_POINTS", "10")                   # MA على آخر كم نقطة (تقريبًا آخر ~10 تحديثات)
    MIN_POINTS = env_int("MIN_POINTS", "20")                 # أقل نقاط قبل إرسال إشارات

    # --- شروطك ---
    RSI_MAX_LONG = env_float("RSI_MAX_LONG", "68")
    RSI_MIN_SHORT = env_float("RSI_MIN_SHORT", "35")

    # --- فلتر سبريد (مهم للسكالب) ---
    MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.004")    # 0.4% افتراضيًا

    # --- منع تكرار ---
    COOLDOWN_SEC = env_int("COOLDOWN_SEC", "120")            # افتراضي 2 دقيقة (غيّره لو تبي)

    # تخزين الأسعار اللحظية (آخر 30 ثانية) لكل سهم
    price_buf: dict[str, deque] = {sym: deque() for sym in TICKERS}  # (ts_epoch, price)

    # آخر Quote لكل سهم (عشان spread)
    last_quote: dict[str, dict] = {sym: {"bid": None, "ask": None, "mid": None, "spread_pct": None} for sym in TICKERS}

    # آخر وقت تنبيه لكل سهم
    last_alert_ts: dict[str, float] = {sym: 0.0 for sym in TICKERS}

    stream = StockDataStream(API_KEY, SECRET_KEY, feed=FEED)

    def now_epoch() -> float:
        return datetime.now(timezone.utc).timestamp()

    def prune(sym: str, now_ts: float):
        dq = price_buf[sym]
        cutoff = now_ts - PRICE_WINDOW_SEC
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def series_from_buf(sym: str) -> pd.Series | None:
        dq = price_buf[sym]
        if len(dq) == 0:
            return None
        # فقط الأسعار بالترتيب
        return pd.Series([p for _, p in dq], dtype="float64")

    async def on_quote(q):
        sym = q.symbol
        bid = getattr(q, "bid_price", None)
        ask = getattr(q, "ask_price", None)
        if bid is None or ask is None:
            return
        bid = float(bid)
        ask = float(ask)
        if bid <= 0 or ask <= 0:
            return

        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else None

        last_quote[sym] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct
        }

    async def on_trade(t):
        sym = t.symbol
        # نفضل mid من quote (أسرع للسكالب) وإذا ما توفر نستخدم trade price
        mid = last_quote[sym]["mid"]
        price = float(mid) if mid is not None else float(getattr(t, "price", None) or 0.0)
        if price <= 0:
            return

        ts = now_epoch()
        # حدث البفر
        price_buf[sym].append((ts, price))
        prune(sym, ts)

        # فلتر: لازم نقاط كفاية
        if len(price_buf[sym]) < MIN_POINTS:
            return

        # فلتر سبريد
        sp = last_quote[sym]["spread_pct"]
        if sp is not None and sp > MAX_SPREAD_PCT:
            return

        # حساب RSI و MA لحظي
        s = series_from_buf(sym)
        if s is None:
            return

        rsi = calculate_rsi(s, RSI_WINDOW)
        ma = mean_last_n(s, MA_POINTS)
        if rsi is None or ma is None:
            return

        price_now = float(s.iloc[-1])

        # منع التكرار
        if ts - last_alert_ts[sym] < COOLDOWN_SEC:
            return

        msg = None

        # LONG: السعر فوق MA و RSI أقل من الحد
        if price_now > ma and rsi < RSI_MAX_LONG:
            msg = (
                f"🚀 *إشارة LONG لحظية: {sym}*\n"
                f"💰 السعر: {price_now:.2f}\n"
                f"📊 RSI({PRICE_WINDOW_SEC}s): {rsi:.2f}\n"
                f"📈 MA(آخر {MA_POINTS} نقاط): {ma:.2f}\n"
                + (f"🧾 Spread: {(sp*100):.2f}%\n" if sp is not None else "")
                + "⚡ تنبيه لحظي (سكالب)"
            )

        # SHORT: السعر تحت MA و RSI أعلى من الحد
        elif price_now < ma and rsi > RSI_MIN_SHORT:
            msg = (
                f"📉 *إشارة SHORT لحظية: {sym}*\n"
                f"💰 السعر: {price_now:.2f}\n"
                f"📊 RSI({PRICE_WINDOW_SEC}s): {rsi:.2f}\n"
                f"📉 MA(آخر {MA_POINTS} نقاط): {ma:.2f}\n"
                + (f"🧾 Spread: {(sp*100):.2f}%\n" if sp is not None else "")
                + "⚡ تنبيه لحظي (سكالب)"
            )

        if msg:
            send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
            last_alert_ts[sym] = ts
            logging.info(f"Alert sent for {sym} | price={price_now:.2f} rsi={rsi:.2f} ma={ma:.2f}")

    # اشترك: Quotes + Trades
    stream.subscribe_quotes(on_quote, *TICKERS)
    stream.subscribe_trades(on_trade, *TICKERS)

    send_tg_msg(
        TG_TOKEN, TG_CHAT_ID,
        f"📡 *WebSocket لحظي شغال*\n"
        f"• نافذة الأسعار: {PRICE_WINDOW_SEC}s\n"
        f"• RSI: {RSI_WINDOW} نقاط\n"
        f"• MA: آخر {MA_POINTS} نقاط\n"
        f"• Max Spread: {MAX_SPREAD_PCT*100:.2f}%\n"
        f"• Cooldown: {COOLDOWN_SEC}s"
    )

    await stream.run()

if __name__ == "__main__":
    asyncio.run(main())
