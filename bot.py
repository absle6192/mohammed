# bot.py
import os
import time
import math
import requests
from datetime import datetime, UTC

# ============ الإعدادات من متغيرات البيئة ============
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "AMD,TSLA").split(",") if s.strip()]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

# تفعيل التداول (ورقي فقط)
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() == "true"

# حجم الصفقة بالدولار (يحوّل تلقائيًا إلى كمية أسهم)
DOLLAR_PER_TRADE = float(os.getenv("DOLLAR_PER_TRADE", "50"))

# إشارة الدخول (مومنتم): اشترِ إذا ارتفع السعر عن القراءة السابقة بهذه النسبة أو أكثر
ENTRY_UP_PCT = float(os.getenv("ENTRY_UP_PCT", "0.6"))  # مثال: 0.6% من القراءة السابقة

# إدارة المخاطر
TP_PCT = float(os.getenv("TP_PCT", "1.2"))   # هدف ربح: 1.2%
SL_PCT = float(os.getenv("SL_PCT", "0.6"))   # وقف خسارة: 0.6%

# تبريد بين الصفقات لنفس السهم (بالثواني)
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "300"))  # 5 دقائق

# مفاتيح Alpaca
ALPACA_KEY    = os.getenv("ALPACA_KEY_ID", "") or os.getenv("APCA_API_KEY_ID", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "") or os.getenv("APCA_API_SECRET_KEY", "")

if not ALPACA_KEY or not ALPACA_SECRET:
    print("تحذير: مفاتيح Alpaca غير موجودة. أضف ALPACA_KEY_ID و ALPACA_SECRET_KEY.", flush=True)

# ============ عناوين API ============
ALPACA_DATA_BASE = "https://data.alpaca.markets/v2/stocks"
TRADING_BASE = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

def log(msg: str):
    dt = datetime.now(UTC)
    print(f"[{dt.isoformat()}Z] {msg}", flush=True)

# ============ أدوات عامة ============
def is_market_open() -> bool:
    try:
        r = requests.get(f"{TRADING_BASE}/clock", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return bool(r.json().get("is_open", False))
    except Exception:
        pass
    return False

def get_position_qty(symbol: str) -> float:
    """يرجع كمية المركز المفتوح في السهم (0 إذا لا يوجد)."""
    try:
        r = requests.get(f"{TRADING_BASE}/positions/{symbol}", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            qty = data.get("qty")
            if qty is not None:
                return float(qty)
    except Exception:
        pass
    return 0.0

def get_last_trade_price(symbol: str):
    try:
        url = f"{ALPACA_DATA_BASE}/{symbol}/trades/latest"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        trade = r.json().get("trade")
        if not trade:
            return None
        return float(trade.get("p"))
    except Exception:
        return None

def place_bracket_buy(symbol: str, price: float, qty: int):
    """
    أمر شراء Market مع Bracket:
    - Take Profit بنسبة TP_PCT
    - Stop Loss بنسبة SL_PCT
    """
    tp = round(price * (1 + TP_PCT / 100), 2)
    sl_stop = round(price * (1 - SL_PCT / 100), 2)
    body = {
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": tp},
        "stop_loss": {"stop_price": sl_stop}
    }
    r = requests.post(f"{TRADING_BASE}/orders", headers=HEADERS, json=body, timeout=10)
    if r.status_code in (200, 201):
        log(f"تم إرسال أمر BUY (Bracket) لـ {symbol} - qty={qty} | TP={tp} | SL={sl_stop}")
        return r.json()
    else:
        log(f"فشل أمر BUY لـ {symbol}: {r.status_code} | {r.text[:200]}")
        return None

def dollars_to_qty(dollars: float, price: float) -> int:
    if price <= 0:
        return 0
    qty = int(dollars // price)
    return max(1, qty)

# ============ منطق الإشارة ============
last_price = {}       # آخر قراءة سعر لكل رمز
last_exec_time = {}   # آخر وقت تنفيذ صفقة لكل رمز (للتبريد)

def should_enter_long(symbol: str, price: float) -> bool:
    """
    ندخل شراء إذا price >= last_price * (1 + ENTRY_UP_PCT/100)
    ولا يوجد مركز حالي، وليس في فترة تبريد.
    """
    prev = last_price.get(symbol)
    if prev is None:
        return False  # نحتاج قراءة سابقة للمقارنة
    change_pct = (price / prev - 1) * 100.0
    if change_pct >= ENTRY_UP_PCT:
        # تبريد
        t_prev = last_exec_time.get(symbol, 0)
        if (time.time() - t_prev) < COOLDOWN_SEC:
            return False
        # عدم وجود مركز مفتوح
        if get_position_qty(symbol) > 0:
            return False
        return True
    return False

# ============ الحلقة الرئيسية ============
def main():
    log("بدء تشغيل البوت (نسخة التداول الآلي الورقي).")
    log(f"الأسهم: {', '.join(SYMBOLS)} | التحديث كل {POLL_SECONDS}s")
    log(f"الإشارة: دخول عند +{ENTRY_UP_PCT}% عن القراءة السابقة | TP={TP_PCT}% | SL={SL_PCT}%")
    if not ENABLE_TRADING:
        log("ملاحظة: ENABLE_TRADING=false (لن يتم إرسال أوامر).")

    while True:
        market_open = is_market_open()
        for sym in SYMBOLS:
            price = get_last_trade_price(sym)
            if price is None:
                log(f"{sym}: لا توجد بيانات.")
                continue

            log(f"{sym}: آخر سعر = {price}")
            if market_open and ENABLE_TRADING:
                if should_enter_long(sym, price):
                    qty = dollars_to_qty(DOLLAR_PER_TRADE, price)
                    if qty > 0:
                        res = place_bracket_buy(sym, price, qty)
                        if res is not None:
                            last_exec_time[sym] = time.time()

            # حدّث القراءة السابقة دائمًا
            last_price[sym] = price

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
