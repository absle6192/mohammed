import os
import time
import requests

# جلب مفتاح API من البيئة (Alpaca)
API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")

# رابط بيانات السوق من Alpaca
BASE_URL = "https://data.alpaca.markets"

# قائمة الأسهم (٨ أسهم)
SYMBOLS = ["TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD"]

# دالة تجيب آخر سعر
def get_last_price(symbol):
    url = f"{BASE_URL}/v2/stocks/{symbol}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": API_KEY,
        "APCA-API-SECRET-KEY": API_SECRET
    }
    try:
        r = requests.get(url, headers=headers)
        data = r.json()
        return data.get("trade", {}).get("p", None)  # p = price
    except Exception as e:
        print(f"⚠️ خطأ في {symbol}: {e}")
        return None

def main():
    last_prices = {s: None for s in SYMBOLS}
    print("🚀 بدأ تشغيل مراقبة الأسعار لأسهم البوت ...")

    while True:
        for symbol in SYMBOLS:
            price = get_last_price(symbol)
            if price:
                if last_prices[symbol] is None:
                    last_prices[symbol] = price
                else:
                    diff = price - last_prices[symbol]
                    if diff > 0:
                        print(f"📈 {symbol} ارتفع: +{diff:.2f}$ (السعر الآن {price}$)")
                    elif diff < 0:
                        print(f"📉 {symbol} نزل: {diff:.2f}$ (السعر الآن {price}$)")
                    last_prices[symbol] = price
        time.sleep(5)  # يحدث كل 5 ثواني

if __name__ == "__main__":
    main()
