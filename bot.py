import os, time, datetime as dt, traceback, math
import pandas as pd, numpy as np
from alpaca_trade_api.rest import REST, TimeFrame

# ===== الاتصال بـ Alpaca =====
API_KEY    = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

api = REST(API_KEY, SECRET_KEY, BASE_URL)

# ===== إعدادات عامة =====
SYMBOLS = ["AAPL","MSFT","NVDA","AMD","TSLA"]
RISK_PER_TRADE = 0.01
VOL_SPIKE_FACTOR = 2.5
ATR_MULT_TRAIL = 2.0
SCORE_THRESHOLD = 60

def log(msg):
    now = dt.datetime.utcnow().strftime("[%Y-%m-%d %H:%M:%S UTC]")
    print(now, msg, flush=True)

# ===== مؤشرات =====
def rsi(series, period=14):
    delta = series.diff()
    up, down = delta.clip(lower=0), -delta.clip(upper=0)
    roll_up = up.ewm(span=period).mean()
    roll_down = down.ewm(span=period).mean()
    rs = roll_up / (roll_down + 1e-9)
    return 100 - (100/(1+rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast).mean()
    ema_slow = series.ewm(span=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal).mean()
    return macd_line, signal_line

def atr(df, period=14):
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        (df['high'] - df['low']),
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ===== جلب البيانات =====
def fetch_data(symbol, limit=200):
    end = dt.datetime.utcnow()
    start = end - dt.timedelta(minutes=limit)
    bars = api.get_bars(symbol, TimeFrame.Minute, start.isoformat()+'Z', end.isoformat()+'Z').df
    return bars if not bars.empty else None

# ===== تحليل سهم =====
def analyze_symbol(symbol):
    df = fetch_data(symbol)
    if df is None or len(df) < 50: return None

    df['RSI'] = rsi(df['close'])
    df['MACD'], df['MACDsig'] = macd(df['close'])
    df['ATR'] = atr(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    whale = last['volume'] > df['volume'].rolling(20).mean().iloc[-1] * VOL_SPIKE_FACTOR
    breakout = last['close'] > df['high'].rolling(20).max().iloc[-2]
    macd_bull = prev['MACD'] <= prev['MACDsig'] and last['MACD'] > last['MACDsig']
    entry_long = last['RSI'] > 50 and macd_bull

    score = 0
    if whale: score += 30
    if breakout: score += 20
    if macd_bull: score += 20
    if entry_long: score += 30

    return {"symbol": symbol, "score": score, "price": float(last['close']), "atr": float(last['ATR'] or 0.0)}

# ===== تنفيذ أمر شراء =====
def place_trade(symbol, qty, price):
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force="day"
        )
        log(f"✅ شراء {symbol} qty={qty} @ {price}")
    except Exception as e:
        log(f"❌ خطأ تنفيذ {symbol}: {e}")

# ===== إدارة المراكز =====
def manage_positions():
    try:
        positions = api.list_positions()
        for p in positions:
            symbol = p.symbol
            qty = float(p.qty)
            df = fetch_data(symbol, 50)
            if df is None: continue
            last = df.iloc[-1]
            atr_val = float(atr(df).iloc[-1] or 0.0)
            stop_price = last['close'] - ATR_MULT_TRAIL * atr_val
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                type="stop",
                time_in_force="day",
                stop_price=round(stop_price,2)
            )
            log(f"🔄 تحديث ستوب {symbol} عند {stop_price}")
    except Exception as e:
        log(f"⚠️ خطأ إدارة المراكز: {e}")

# ===== ملخص نهاية اليوم =====
def eod_summary():
    try:
        acct = api.get_account()
        print("\n📊 ملخص اليوم 📊")
        print("رصيد الحساب:", acct.cash)
        print("القوة الشرائية:", acct.buying_power)
        print("القيمة الإجمالية:", acct.equity)
    except Exception as e:
        print("❌ خطأ ملخص:", e)

# ===== الحلقة الرئيسية =====
def main_loop(minutes=480):
    end_time = time.time() + minutes*60
    while time.time() < end_time:
        try:
            clock = api.get_clock()
            if clock.is_open:
                candidates = []
                for sym in SYMBOLS:
                    sig = analyze_symbol(sym)
                    if sig and sig['score'] >= SCORE_THRESHOLD:
                        candidates.append(sig)
                if candidates:
                    best = max(candidates, key=lambda x: x['score'])
                    cash = float(api.get_account().cash)
                    qty = int((cash * RISK_PER_TRADE) / best['price'])
                    if qty > 0:
                        place_trade(best['symbol'], qty, best['price'])
                manage_positions()
            else:
                log("⏳ السوق مقفل...")
            time.sleep(60)
        except Exception as e:
            log(f"❌ خطأ عام: {e}\n{traceback.format_exc()}")
            time.sleep(30)
    eod_summary()

if __name__ == "__main__":
    main_loop()
