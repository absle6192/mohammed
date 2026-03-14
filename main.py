import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, json=data)

def get_price():
    url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    response = requests.get(url)
    data = response.json()
    return data["price"]

while True:
    try:
        price = get_price()
        message = f"💰 BTC Price: {price}"
        print(message)
        send_telegram(message)
        time.sleep(60)

    except Exception as e:
        print("❌ Error:", e)
        time.sleep(10)
