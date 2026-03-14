import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

last_price = None

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, json=data)

def get_price():
    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=NQ=F"
    data = requests.get(url).json()
    price = data["quoteResponse"]["result"][0]["regularMarketPrice"]
    return price

while True:
    try:
        global last_price

        price = get_price()

        if last_price is not None:

            if price > last_price:

                message = f"""
📈 BUY NQ

Entry: {price}
SL: {price - 20}
TP: {price + 40}
"""

                send_telegram(message)

            elif price < last_price:

                message = f"""
📉 SELL NQ

Entry: {price}
SL: {price + 20}
TP: {price - 40}
"""

                send_telegram(message)

        last_price = price

        time.sleep(60)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)
