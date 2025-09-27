import os
import time
import requests

# =========================
# إعدادات
# =========================
API_KEY = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL = "https://data.alpaca.markets/v2"

# أسهم البوت القديم
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

# =========================
# جلب آخر سعر
# =========================
def get_last_price(symbol):
    url = f"{BASE_URL}/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET,
    }
    try:
        r = requests.get(url, headers=headers)
        data = r.json()
        return data.get("trade", {}).get("p", None)
    except Exception as e:
        print(f"⚠️ خطأ في {symbol}: {e}")
        return None

# =========================
# البرنامج الرئيسي
# =========================
def main():
    last_prices = {s: None for s in SYMBOLS}
    print("🚀 تشغيل مراقبة الأسعار لأسهم البوت")

    while True:
        for symbol in SYMBOLS:
            price = get_last_price(symbol)
            if price:
                print(f"{symbol} السعر الحالي: {price}")

                if last_prices[symbol] is not None and price != last_prices[symbol]:
                    print(f"🔔 {symbol}: تغير السعر من {last_prices[symbol]} → {price}")

                last_prices[symbol] = price
        time.sleep(5)  # يحدث كل 5 ثواني

if __name__ == "__main__":
    main()
