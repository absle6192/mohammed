# bot.py – clean header
import os
import time
import requests
from datetime import datetime, UTC
import json, traceback

# دالة طباعة مخصصة للـ Debug
def debug_print(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")

# ---- Alpaca env (لا تغيّر الأسماء) ----
API_KEY     = os.getenv("APCA_API_KEY_ID")
API_SECRET  = os.getenv("APCA_API_SECRET_KEY")
BASE_URL    = (os.getenv("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets")
DATA_URL    = "https://data.alpaca.markets"

# رؤوس الطلبات الموحدة
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# حماية من نسيان المفاتيح
if not API_KEY or not API_SECRET:
    raise Exception("⚠️ مفاتيح Alpaca غير موجودة بالمتغيرات البيئية")

# دالة تجيب آخر سعر تداول لسهم
def get_last_trade_price(symbol):
    url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest"
    r = requests.get(url, headers=HEADERS, timeout=5)
    if r.status_code != 200:
        debug_print(f"{symbol}: ❌ خطأ HTTP {r.status_code} | {r.text[:100]}")
        return None
    data = r.json()
    trade = data.get("trade")
    if trade and "p" in trade:
        return trade["p"]
    return None

# تحويل دولار إلى كمية أسهم (مثال)
def dollars_to_qty(dollars):
    return int(dollars)  # للتبسيط، عدلها كما يناسبك

# تنفيذ أمر شراء (مثال)
def place_bracket_buy(symbol, price, qty):
    try:
        debug_print(f"{symbol}: 🟢 أمر شراء {qty} @ {price}")
        return {"id": "mock-order-id"}  # للتمثيل فقط
    except Exception as e:
        debug_print(f"{symbol}: ❌ خطأ أثناء الشراء: {e}")
        return None

# دالة لفحص السوق (مثال)
def is_market_open():
    # تقدر تعدلها حسب API Alpaca
    return True

# قائمة الأسهم اليدوية (لو AUTO_SELECT=False)
SYMBOLS_MANUAL = ["AAPL", "MSFT", "NVDA"]

# إعدادات
AUTO_SELECT = False
ENABLE_TRADING = True
DOLLAR_PER_TRADE = 1000
POLL_SECONDS = 5

# تخزين آخر تنفيذ
last_exec_time = {}
last_price = {}

def main():
    while True:
        market_open = is_market_open()

        if AUTO_SELECT:
            # هنا منطق اختيار الأسهم تلقائيًا
            selected = SYMBOLS_MANUAL  # بدّلها لاحقًا بدالة pick_top_symbols
        else:
            selected = SYMBOLS_MANUAL

        # نفذ المنطق على القائمة المختارة
        for sym in selected:
            price = get_last_trade_price(sym)

            if price is None:
                debug_print(f"{sym}: ⚠️ API ما رجّع بيانات (None)")
                debug_print(f"{sym}: لا توجد بيانات.")
                continue
            else:
                debug_print(f"{sym}: ✅ السعر الحالي = {price}")

            log(f"{sym}: آخر سعر = {price}")

            if market_open and ENABLE_TRADING:
                qty = dollars_to_qty(DOLLAR_PER_TRADE)
                if qty > 0:
                    res = place_bracket_buy(sym, price, qty)
                    if res is not None:
                        last_exec_time[sym] = time.time()

            # تحديث آخر سعر
            last_price[sym] = price

        time.sleep(POLL_SECONDS)

def log(msg):
    print(msg)

if __name__ == "__main__":
    main()
