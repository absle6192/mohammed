import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("TWELVE_API_KEY")
SYMBOL = os.getenv("SYMBOL")

last_price = None
last_signal = None


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message
        })
    except Exception as e:
        print("Telegram error:", e)


def get_price():
    try:
        url = f"https://api.twelvedata.com/price?symbol={SYMBOL}=F&apikey={API_KEY}"
        r = requests.get(url)
        data = r.json()

        if "price" in data:
            return float(data["price"])
        else:
            print("API error:", data)
            return None

    except Exception as e:
        print("Price error:", e)
        return None


send_telegram(f"🚀 {SYMBOL} Trading Bot Started")

while True:

    try:

        price = get_price()

        if price is None:
            time.sleep(30)
            continue

        print("Current price:", price)

        if last_price is not None:

            if price > last_price and last_signal != "LONG":

                last_signal = "LONG"

                send_telegram(f"""
📈 LONG SIGNAL

Symbol: {SYMBOL}
Entry: {price}
Take Profit: {price + 20}
Stop Loss: {price - 10}
""")

            elif price < last_price and last_signal != "SHORT":

                last_signal = "SHORT"

                send_telegram(f"""
📉 SHORT SIGNAL

Symbol: {SYMBOL}
Entry: {price}
Take Profit: {price - 20}
Stop Loss: {price + 10}
""")

        last_price = price

        time.sleep(60)

    except Exception as e:
        print("Loop error:", e)
        time.sleep(30)
