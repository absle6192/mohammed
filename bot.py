# bot.py
# -*- coding: utf-8 -*-

import os, time, traceback
from datetime import datetime as dt
import pandas as pd
import numpy as np
from alpaca_trade_api.rest import REST, TimeFrame, APIError

# =========================
# مفاتيح Alpaca & الإعدادات
# =========================
API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

if not API_KEY or not SECRET_KEY:
    raise RuntimeError("الرجاء ضبط مفاتيح Alpaca: ALPACA_API_KEY و ALPACA_SECRET_KEY")

# نمط مصدر البيانات: auto / iex / sip (الافتراضي iex)
DATA_FEED_MODE = os.getenv("ALPACA_DATA_FEED", "iex").lower()
use_iex_flag   = (DATA_FEED_MODE == "iex")

# إنشاء عميل Alpaca (بدون use_iex في الـ __init__ لدعم الإصدارات القديمة)
api = REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")
# محاولة إجبار IEX لأي نسخة
try:
    api.use_iex = True if use_iex_flag else False
except Exception:
    pass
try:
    if use_iex_flag:
        api._use_iex = True
except Exception:
    pass

# إعدادات عامة
DEFAULT_SYMBOLS   = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA"]
SYMBOLS           = [s.strip().upper() for s in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]
RISK_PER_TRADE    = float(os.getenv("RISK_PER_TRADE", 0.01))   # نسبة من رأس المال لكل صفقة
VOL_SPIKE_FACTOR  = float(os.getenv("VOL_SPIKE_FACTOR", 2.5))  # تضخم حجم التداول
ATR_MULT_TRAIL    = float(os.getenv("ATR_MULT_TRAIL", 2.0))    # معامل وقف متحرك
SCORE_THRESHOLD   = float(os.getenv("SCORE_THRESHOLD", 60))    # حد إشارة مركّب (اختياري)
PLACE_ORDERS      = os.getenv("PLACE_ORDERS", "false").lower() == "true"
LOOP_SLEEP        = int(os.getenv("LOOP_SLEEP", 60))           # ثانية

# ==========
# أدوات عامّة
# ==========
def _log(msg):
    print(f"[{dt.utcnow().isoformat()}Z] {msg}", flush=True)

def _force_iex():
    """إجبار التحويل إلى IEX في وقت التشغيل (لكل نسخ المكتبة)."""
    global api
    try:
        api = REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")
        try: api.use_iex = True
        except Exception: pass
        try: api._use_iex = True
        except Exception: pass
        _log("تم التحويل إلى IEX أثناء التشغيل.")
        return True
    except Exception as e:
        _log(f"فشل التحويل إلى IEX: {e}")
        return False

# =================
# جلب البيانات Bars
# =================
def _bars_to_df(symbol, bars):
    """تحويل نتيجة get_bars إلى DataFrame موحّد الأعمدة."""
    # بعض النسخ ترجع .df
    try:
        df = bars.df.copy()
        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol]
        cols = {c.lower(): c for c in df.columns}
        need = ["open","high","low","close","volume"]
        if all(k in cols for k in need):
            return df[[cols["open"], cols["high"], cols["low"], cols["close"], cols["volume"]]]\
                    .rename(columns={cols["open"]:"open", cols["high"]:"high", cols["low"]:"low",
                                     cols["close"]:"close", cols["volume"]:"volume"})
    except Exception:
        pass

    # وبعضها ترجع Iterable من Bar objects
    data = []
    try:
        for b in bars:
            t = getattr(b, "t", None)
            if t is None: t = getattr(b, "Timestamp", None)
            data.append({
                "ts": pd.Timestamp(t),
                "open":  float(getattr(b, "o", getattr(b, "open", 0.0))),
                "high":  float(getattr(b, "h", getattr(b, "high", 0.0))),
                "low":   float(getattr(b, "l", getattr(b, "low", 0.0))),
                "close": float(getattr(b, "c", getattr(b, "close", 0.0))),
                "volume":float(getattr(b, "v", getattr(b, "volume", 0.0))),
            })
    except Exception:
        pass

    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data).set_index("ts")
    return df[["open","high","low","close","volume"]].copy()

def get_bars_df(symbol, timeframe=TimeFrame.Minute, limit=120):
    """يحاول SIP أولاً؛ إذا رُفض الاشتراك يُحوّل تلقائياً إلى IEX (إلا لو DATA_FEED_MODE=sip)."""
    try:
        bars = api.get_bars(symbol, timeframe, limit=limit)
        return _bars_to_df(symbol, bars)
    except APIError as e:
        msg = str(e).lower()
        sip_denied = ("sip" in msg) and (("not permitted" in msg) or ("subscription" in msg))
        if sip_denied and DATA_FEED_MODE != "sip":
            _log(f"{symbol}: رُفض SIP — التحويل إلى IEX وإعادة المحاولة.")
            if _force_iex():
                bars = api.get_bars(symbol, timeframe, limit=limit)
                return _bars_to_df(symbol, bars)
        raise
    except Exception as e:
        _log(f"{symbol}: خطأ غير متوقّع في جلب البيانات: {e}")
        return pd.DataFrame()

# =====================
# مؤشرات وإشارات بسيطة
# =====================
def compute_indicators(df):
    if df.empty: return df
    # ATR
    tr1 = (df["high"] - df["low"]).abs()
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14, min_periods=1).mean()
    # تضخم الفوليوم
    vma = df["volume"].rolling(30, min_periods=1).mean()
    df["vol_spike"] = (df["volume"] > VOL_SPIKE_FACTOR * vma).astype(int)
    # زخم بسيط 5 شموع
    df["mom"] = (df["close"] / df["close"].shift(5) - 1.0) * 100.0
    # متوسطات بسيطة
    df["sma20"] = df["close"].rolling(20, min_periods=1).mean()
    df["sma50"] = df["close"].rolling(50, min_periods=1).mean()
    # درجة مركّبة اختيارية
    df["score"] = (40 * (df["mom"].clip(-2, 2) / 2.0) + 60 * df["vol_spike"]).clip(0, 100)
    return df

def trailing_exit_needed(df, atr_mult=ATR_MULT_TRAIL):
    if df.empty or "atr" not in df: return False
    closes = df["close"]; atr = df["atr"]
    hh = closes.cummax()
    trail = hh - atr_mult * atr
    return closes.iloc[-1] < trail.iloc[-1]

# ===================
# أحجام ومهام التداول
# ===================
def account_equity():
    try:
        eq = float(getattr(api.get_account(), "equity", "0") or 0)
        return eq if eq > 0 else 10000.0
    except Exception:
        return 10000.0

def position_size(symbol, last_price):
    budget = max(account_equity() * RISK_PER_TRADE, 50.0)
    qty = int(budget // max(last_price, 0.01))
    return max(qty, 1)

def get_open_position_qty(symbol):
    try:
        pos = api.get_position(symbol)
        return int(float(pos.qty))
    except Exception:
        return 0

def submit_buy(symbol, qty):
    if not PLACE_ORDERS:
        _log(f"[DRY-RUN] BUY {symbol} x{qty}")
        return
    api.submit_order(symbol=symbol, qty=qty, side="buy", type="market", time_in_force="day")
    _log(f"أُرسل أمر شراء: {symbol} x{qty}")

def submit_sell(symbol, qty):
    if not PLACE_ORDERS:
        _log(f"[DRY-RUN] SELL {symbol} x{qty}")
        return
    api.submit_order(symbol=symbol, qty=qty, side="sell", type="market", time_in_force="day")
    _log(f"أُرسل أمر بيع: {symbol} x{qty}")

# ================
# معالجة كل رمز
# ================
def process_symbol(symbol):
    df = get_bars_df(symbol, TimeFrame.Minute, limit=120)
    if df.empty:
        _log(f"{symbol}: لا توجد بيانات.")
        return

    df = compute_indicators(df)
    last = df.iloc[-1]
    last_price = float(last["close"])
    qty_open = get_open_position_qty(symbol)

    # إشارات بسيطة: تقاطع SMA + تضخم فوليوم + زخم + حد درجة
    buy_signal  = (last["sma20"] > last["sma50"]) and (last["vol_spike"] == 1) and (last["mom"] > 0) and (last["score"] >= SCORE_THRESHOLD)
    sell_signal = (qty_open > 0) and trailing_exit_needed(df)

    if buy_signal and qty_open == 0:
        qty = position_size(symbol, last_price)
        _log(f"{symbol}: BUY إشارة | السعر={last_price:.2f} | score={float(last['score']):.1f} | qty={qty}")
        submit_buy(symbol, qty)
    elif sell_signal:
        _log(f"{symbol}: SELL إشارة خروج (وقف متحرك) | السعر={last_price:.2f} | qty={qty_open}")
        submit_sell(symbol, qty_open)
    else:
        _log(f"{symbol}: لا إشارة | السعر={last_price:.2f} | score={float(last['score']):.1f} | pos={qty_open}")

# ===============
# الحلقة الرئيسية
# ===============
def main_loop():
    iex_state = bool(getattr(api, "use_iex", False) or getattr(api, "_use_iex", False))
    _log(f"بدء العامل. رموز={SYMBOLS} | PLACE_ORDERS={PLACE_ORDERS} | DATA_FEED_MODE={DATA_FEED_MODE} | IEX={iex_state} | BASE_URL={BASE_URL}")
    while True:
        try:
            for sym in SYMBOLS:
                try:
                    process_symbol(sym)
                except Exception as e:
                    _log(f"{sym}: خطأ أثناء المعالجة: {e}")
                    traceback.print_exc()
            time.sleep(LOOP_SLEEP)
        except KeyboardInterrupt:
            _log("تم إيقاف العامل يدويًا."); break
        except Exception as e:
            _log(f"خطأ عام في الحلقة: {e}")
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main_loop()
