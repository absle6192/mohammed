# bot.py
# -*- coding: utf-8 -*-

import os, time, traceback
from datetime import datetime as dt
import pandas as pd
import numpy as np
from alpaca_trade_api.rest import REST, TimeFrame, APIError

# ===== الإعدادات العامة =====
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA"]
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]

RISK_PER_TRADE   = float(os.getenv("RISK_PER_TRADE", "0.01"))
VOL_SPIKE_FACTOR = float(os.getenv("VOL_SPIKE_FACTOR", "2.5"))
ATR_MULT_TRAIL   = float(os.getenv("ATR_MULT_TRAIL", "2.0"))
SCORE_THRESHOLD  = float(os.getenv("SCORE_THRESHOLD", "60"))
PLACE_ORDERS     = os.getenv("PLACE_ORDERS", "false").lower() == "true"
LOOP_SLEEP       = int(os.getenv("LOOP_SLEEP", "30"))

# ===== مفاتيح Alpaca =====
API_KEY = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL") or os.getenv("APCA_API_BASE_URL") or "https://paper-api.alpaca.markets"

if not API_KEY or not SECRET_KEY:
    raise RuntimeError("الرجاء ضبط مفاتيح Alpaca في المتغيرات البيئية.")

# نمط البيانات: auto (افتراضي) أو iex أو sip
DATA_FEED_MODE = os.getenv("ALPACA_DATA_FEED", "auto").lower()
use_iex_flag = True if DATA_FEED_MODE == "iex" else False

# إنشاء العميل مع دعم IEX
api = REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2", use_iex=use_iex_flag)

def _log(msg):
    print(f"[{dt.utcnow().isoformat()}Z] {msg}", flush=True)

def _switch_to_iex_runtime():
    """التحويل إلى IEX أثناء التشغيل وإعادة إنشاء العميل."""
    global api
    _log("تم التحويل إلى IEX أثناء التشغيل.")
    api = REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2", use_iex=True)

# ===== أدوات البيانات =====
def get_bars_auto(symbol, timeframe=TimeFrame.Minute, limit=120):
    """يحاول يجلب من SIP؛ لو اتمنع لعدم الاشتراك يرجع IEX تلقائيًا (إلا إذا أجبرت sip)."""
    try:
        return api.get_bars(symbol, timeframe, limit=limit)
    except APIError as e:
        msg = str(e).lower()
        sip_denied = ("sip" in msg and "not permitted" in msg) or ("subscription" in msg and "sip" in msg)
        if sip_denied and DATA_FEED_MODE != "sip":
            _log(f"{symbol}: رفض SIP — التحويل إلى IEX وإعادة المحاولة.")
            _switch_to_iex_runtime()
            return api.get_bars(symbol, timeframe, limit=limit)
        raise

def compute_indicators(df):
    tr1 = (df['high'] - df['low']).abs()
    tr2 = (df['high'] - df['close'].shift(1)).abs()
    tr3 = (df['low']  - df['close'].shift(1)).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=1).mean()

    v_ma = df['volume'].rolling(30, min_periods=1).mean()
    vol_spike = (df['volume'] > VOL_SPIKE_FACTOR * v_ma).astype(int)

    mom = (df['close'] / df['close'].shift(5) - 1) * 100.0

    score = (40 * (mom.clip(-2, 2) / 2.0) + 60 * vol_spike).clip(0, 100)
    out = df.copy()
    out['atr'] = atr
    out['vol_spike'] = vol_spike
    out['mom'] = mom
    out['score'] = score
    return out

def last_price_from_bars(bars_df):
    return float(bars_df['close'].iloc[-1])

def position_size(symbol, last_price):
    try:
        acct = api.get_account()
        equity = float(getattr(acct, "equity", "0") or 0)
        if equity <= 0:
            equity = 10000.0
    except Exception:
        equity = 10000.0
    budget = max(equity * RISK_PER_TRADE, 50.0)
    qty = int(budget // max(last_price, 0.01))
    return max(qty, 1)

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

def get_open_position_qty(symbol):
    try:
        pos = api.get_position(symbol)
        return int(float(pos.qty))
    except Exception:
        return 0

def trailing_exit_needed(df, trail_mult=ATR_MULT_TRAIL):
    closes = df['close']; atr = df['atr']
    hh = closes.cummax()
    trail = hh - trail_mult * atr
    return closes.iloc[-1] < trail.iloc[-1]

def process_symbol(symbol):
    bars = get_bars_auto(symbol, TimeFrame.Minute, limit=120)
    if len(bars) == 0:
        _log(f"{symbol}: لا توجد بيانات.")
        return
    data = [{
        "ts": pd.Timestamp(getattr(b, "t")).timestamp(),
        "open": float(b.o), "high": float(b.h), "low": float(b.l),
        "close": float(b.c), "volume": float(b.v),
    } for b in bars]
    df = pd.DataFrame(data).set_index(pd.to_datetime([d["ts"] for d in data], unit="s"))
    df = df.drop(columns=['ts']); df.columns = ['open','high','low','close','volume']

    ind = compute_indicators(df)
    last = ind.iloc[-1]
    last_price = float(last['close'])
    score = float(last['score'])
    qty_open = get_open_position_qty(symbol)

    buy_signal  = (last['vol_spike'] == 1) and (last['mom'] > 0) and (score >= SCORE_THRESHOLD)
    sell_signal = qty_open > 0 and trailing_exit_needed(ind)

    if buy_signal and qty_open == 0:
        qty = position_size(symbol, last_price)
        _log(f"{symbol}: إشارة شراء | السعر={last_price:.2f} | score={score:.1f} | qty={qty}")
        submit_buy(symbol, qty)
    elif sell_signal:
        _log(f"{symbol}: إشارة خروج (وقف متحرك) | السعر={last_price:.2f}")
        submit_sell(symbol, qty_open)
    else:
        _log(f"{symbol}: لا إشارة | السعر={last_price:.2f} | score={score:.1f} | pos={qty_open}")

def main_loop():
    _log(f"بدء العامل. رموز: {SYMBOLS} | PLACE_ORDERS={PLACE_ORDERS} | DATA_FEED_MODE={DATA_FEED_MODE} | BASE_URL={BASE_URL}")
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
