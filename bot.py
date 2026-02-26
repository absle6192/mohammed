import os
import logging
import requests
from collections import deque
from datetime import datetime, timezone

import pandas as pd
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed  # ✅ مهم

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

def env_bool(name: str, default: str = "false") -> bool:
    v = env(name, default).strip().lower()
    return v in ("1", "true", "yes", "y", "on")

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
def main():
    # --- ENV ---
    API_KEY = env("APCA_API_KEY_ID")
    SECRET_KEY = env("APCA_API_SECRET_KEY")

    TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = env("TELEGRAM_CHAT_ID")

    TICKERS = [t.strip().upper() for t in env("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]

    # ✅ FIX: feed لازم يكون DataFeed مو نص
    feed_str = env("DATA_FEED", "iex").strip().lower()
    if feed_str == "iex":
        FEED = DataFeed.IEX
    elif feed_str == "sip":
        FEED = DataFeed.SIP
    else:
        FEED = DataFeed.IEX  # fallback
        feed_str = "iex"

    PRICE_WINDOW_SEC = env_int("PRICE_WINDOW_SEC", "30")
    RSI_WINDOW = env_int("RSI_WINDOW", "14")
    MA_POINTS = env_int("MA_POINTS", "10")
    MIN_POINTS = env_int("MIN_POINTS", "20")

    CONFIRM_SEC = env_int("CONFIRM_SEC", "2")

    RSI_MAX_LONG = env_float("RSI_MAX_LONG", "68")
    RSI_MIN_SHORT = env_float("RSI_MIN_SHORT", "35")

    MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.004")
    COOLDOWN_SEC = env_int("COOLDOWN_SEC", "120")

    # ===================== Precision Filters (اختيارية) =====================
    # 1) لازم السعر يبتعد عن MA بنسبة معينة (افتراضي 0 = ما يتغير شيء)
    MA_DISTANCE_PCT = env_float("MA_DISTANCE_PCT", "0.0")  # مثال: 0.0008 = 0.08%

    # 2) لازم يتحقق “حركة” داخل نافذة PRICE_WINDOW_SEC (افتراضي 0 = ما يتغير شيء)
    MIN_MOVE_PCT = env_float("MIN_MOVE_PCT", "0.0")  # مثال: 0.0012 = 0.12%

    # 3) هامش إضافي للـ RSI (افتراضي 0 = ما يتغير شيء)
    MIN_RSI_BUFFER = env_float("MIN_RSI_BUFFER", "0.0")  # مثال: 2.0 أو 4.0

    # 4) تأكيد أقوى: لازم نفس الإشارة تستمر إلى نهاية CONFIRM_SEC
    STRICT_CONFIRM = env_bool("STRICT_CONFIRM", "true")  # true أفضل، وتبقى آمنة

    price_buf: dict[str, deque] = {sym: deque() for sym in TICKERS}  # (ts_epoch, price)
    last_quote: dict[str, dict] = {sym: {"bid": None, "ask": None, "mid": None, "spread_pct": None} for sym in TICKERS}
    last_alert_ts: dict[str, float] = {sym: 0.0 for sym in TICKERS}

    # بدل pending_since فقط: نخزن معها نوع الإشارة
    pending_since: dict[str, float | None] = {sym: None for sym in TICKERS}
    pending_signal: dict[str, str | None] = {sym: None for sym in TICKERS}

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
        return pd.Series([p for _, p in dq], dtype="float64")

    def compute_move_pct(s: pd.Series) -> float | None:
        # نسبة الحركة من أول نقطة بالنافذة لآخر نقطة
        if s is None or len(s) < 2:
            return None
        first = float(s.iloc[0])
        last = float(s.iloc[-1])
        if first <= 0:
            return None
        return (last - first) / first

    def compute_signal(sym: str, ts: float):
        if len(price_buf[sym]) < MIN_POINTS:
            return None, None, None, None, None, None

        sp = last_quote[sym]["spread_pct"]
        if sp is not None and sp > MAX_SPREAD_PCT:
            return None, None, None, None, sp, None

        s = series_from_buf(sym)
        if s is None:
            return None, None, None, None, sp, None

        rsi = calculate_rsi(s, RSI_WINDOW)
        ma = mean_last_n(s, MA_POINTS)
        if rsi is None or ma is None:
            return None, None, rsi, ma, sp, None

        price_now = float(s.iloc[-1])
        move_pct = compute_move_pct(s)

        # ---- Filters اختيارية (Defaults = ما تغيّر شيء) ----
        # فلتر مسافة السعر عن MA
        if MA_DISTANCE_PCT > 0 and ma > 0:
            dist_pct = abs(price_now - ma) / ma
            if dist_pct < MA_DISTANCE_PCT:
                return None, price_now, rsi, ma, sp, move_pct

        # فلتر حد أدنى للحركة داخل النافذة
        if MIN_MOVE_PCT > 0 and move_pct is not None:
            if abs(move_pct) < MIN_MOVE_PCT:
                return None, price_now, rsi, ma, sp, move_pct

        # ---- Signal logic (نفس منطقك الأساسي) ----
        # LONG: فوق MA + RSI أقل من الحد (مع buffer اختياري)
        if price_now > ma and rsi < (RSI_MAX_LONG - MIN_RSI_BUFFER):
            return "LONG", price_now, rsi, ma, sp, move_pct

        # SHORT: تحت MA + RSI أعلى من الحد (مع buffer اختياري)
        if price_now < ma and rsi > (RSI_MIN_SHORT + MIN_RSI_BUFFER):
            return "SHORT", price_now, rsi, ma, sp, move_pct

        return None, price_now, rsi, ma, sp, move_pct

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
        sp = (ask - bid) / mid if mid > 0 else None
        if sym not in last_quote:
            return
        last_quote[sym] = {"bid": bid, "ask": ask, "mid": mid, "spread_pct": sp}

        ts = now_epoch()
        price_buf[sym].append((ts, float(mid)))
        prune(sym, ts)

        if ts - last_alert_ts[sym] < COOLDOWN_SEC:
            pending_since[sym] = None
            pending_signal[sym] = None
            return

        signal, price_now, rsi, ma, spread_pct, move_pct = compute_signal(sym, ts)
        if signal is None:
            pending_since[sym] = None
            pending_signal[sym] = None
            return

        # بداية التأكيد
        if pending_since[sym] is None:
            pending_since[sym] = ts
            pending_signal[sym] = signal
            return

        # تأكيد أقوى: لازم نفس الإشارة تظل ثابتة
        if STRICT_CONFIRM and pending_signal[sym] != signal:
            pending_since[sym] = ts
            pending_signal[sym] = signal
            return

        if ts - pending_since[sym] < CONFIRM_SEC:
            return

        # إرسال الإشعار
        if signal == "LONG":
            msg = (
                f"🚀 *إشارة LONG مبكرة: {sym}*\n"
                f"💰 السعر: {price_now:.2f}\n"
                f"📊 RSI({RSI_WINDOW}): {rsi:.2f}\n"
                f"📈 MA(آخر {MA_POINTS}): {ma:.2f}\n"
                + (f"📏 Move({PRICE_WINDOW_SEC}s): {(move_pct*100):.2f}%\n" if move_pct is not None else "")
                + (f"🧾 Spread: {(spread_pct*100):.2f}%\n" if spread_pct is not None else "")
                + f"⚡ تأكيد: {CONFIRM_SEC}s"
            )
        else:
            msg = (
                f"📉 *إشارة SHORT مبكرة: {sym}*\n"
                f"💰 السعر: {price_now:.2f}\n"
                f"📊 RSI({RSI_WINDOW}): {rsi:.2f}\n"
                f"📉 MA(آخر {MA_POINTS}): {ma:.2f}\n"
                + (f"📏 Move({PRICE_WINDOW_SEC}s): {(move_pct*100):.2f}%\n" if move_pct is not None else "")
                + (f"🧾 Spread: {(spread_pct*100):.2f}%\n" if spread_pct is not None else "")
                + f"⚡ تأكيد: {CONFIRM_SEC}s"
            )

        send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
        last_alert_ts[sym] = ts
        pending_since[sym] = None
        pending_signal[sym] = None
        logging.info(f"Early alert sent for {sym} | signal={signal} price={price_now:.2f} rsi={rsi:.2f} ma={ma:.2f}")

    async def on_trade(t):
        sym = t.symbol
        if sym not in price_buf:
            return
        mid = last_quote[sym]["mid"]
        price = float(mid) if mid is not None else float(getattr(t, "price", None) or 0.0)
        if price <= 0:
            return
        ts = now_epoch()
        price_buf[sym].append((ts, float(price)))
        prune(sym, ts)

    stream.subscribe_quotes(on_quote, *TICKERS)
    stream.subscribe_trades(on_trade, *TICKERS)

    send_tg_msg(
        TG_TOKEN, TG_CHAT_ID,
        f"📡 *WebSocket شغال (تبكير إشارات)*\n"
        f"• Feed: {feed_str}\n"
        f"• نافذة الأسعار: {PRICE_WINDOW_SEC}s\n"
        f"• RSI: {RSI_WINDOW} نقاط\n"
        f"• MA: آخر {MA_POINTS} نقاط\n"
        f"• Min Points: {MIN_POINTS}\n"
        f"• Max Spread: {MAX_SPREAD_PCT*100:.2f}%\n"
        f"• Confirm: {CONFIRM_SEC}s\n"
        f"• Cooldown: {COOLDOWN_SEC}s\n"
        f"— تحسين الدقة (اختياري) —\n"
        f"• MA Dist: {MA_DISTANCE_PCT*100:.3f}%\n"
        f"• Min Move: {MIN_MOVE_PCT*100:.3f}%\n"
        f"• RSI Buffer: {MIN_RSI_BUFFER:.2f}\n"
        f"• Strict Confirm: {STRICT_CONFIRM}"
    )

    stream.run()

if __name__ == "__main__":
    main()
