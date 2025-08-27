# bot.py — كامل مع فحص الاشتراك وشراء تجريبي

import os, time, requests, json, traceback
from datetime import datetime, timezone

# ========== بيانات البيئة ==========
API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# ========== أدوات الطباعة ==========
def debug_print(msg):
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")

# ========== فحص الحساب ==========
def account_test():
    url = f"{BASE_URL}/v2/account"
    r = requests.get(url, headers=HEADERS, timeout=6)
    debug_print(f"Account check HTTP {r.status_code}")
    try:
        debug_print(f"Account: {r.json()}")
    except:
        debug_print(f"Account text: {r.text[:200]}")

# ========== فحص الاشتراك (Entitlements) ==========
def check_entitlements():
    url = f"{DATA_URL}/v1beta1/entitlements"
    r = requests.get(url, headers=HEADERS, timeout=6)
    debug_print(f"Entitlements HTTP {r.status_code}")
    try:
        ent = r.json()
    except:
        ent = {"text": r.text[:200]}
    debug_print(f"Entitlements: {json.dumps(ent, indent=2)}")

# ========== جلب آخر سعر ==========
def get_last_trade_price(symbol):
    # أولاً نجرب SIP
    url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest?feed=sip"
    r = requests.get(url, headers=HEADERS, timeout=6)
    if r.status_code == 200:
        try:
            return r.json()["trade"]["p"]
        except:
            return None
    elif r.status_code == 403:
        debug_print(f"{symbol}: SIP ممنوع، نحاول IEX")
        # جرب IEX
        url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest?feed=iex"
        r = requests.get(url, headers=HEADERS, timeout=6)
        if r.status_code == 200:
            try:
                return r.json()["trade"]["p"]
            except:
                return None
        else:
            debug_print(f"{symbol}: فشل IEX HTTP={r.status_code}")
            return None
    else:
        debug_print(f"{symbol}: فشل الجلب HTTP={r.status_code} body={r.text[:200]}")
        return None

# ========== أمر شراء تجريبي ==========
def place_bracket_buy(symbol, qty, price):
    url = f"{BASE_URL}/v2/orders"
    order = {
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc",
    }
    r = requests.post(url, headers=HEADERS, json=order, timeout=6)
    debug_print(f"Buy {symbol} HTTP {r.status_code}")
    try:
        return r.json()
    except:
        return {"text": r.text[:200]}

# ========== التشغيل الرئيسي ==========
def main():
    debug_print(f"Using KEY_ID={API_KEY[:4]}...{API_KEY[-4:]} BASE_URL={BASE_URL}")
    account_test()
    check_entitlements()

    symbols = ["AAPL", "MSFT", "NVDA"]

    while True:
        for sym in symbols:
            price = get_last_trade_price(sym)
            if price:
                debug_print(f"{sym}: السعر الحالي = {price}")
                # تجربة شراء إذا السعر موجود
                res = place_bracket_buy(sym, 1, price)
                debug_print(f"نتيجة الشراء: {res}")
            else:
                debug_print(f"{sym}: لا توجد بيانات.")
        time.sleep(15)  # انتظر 15 ثانية

if __name__ == "__main__":
    main()
