import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message})

def get_price():
    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=NQ=F"
    data = requests.get(url).json()
    return data["quoteResponse"]["result"][0]["regularMarketPrice"]

while True:
    try:
        price = get_price()
        message = f"NQ Price: {price}"
        print(message)
        send_telegram(message)
        time.sleep(60)
    except Exception as e:
        print("Error:", e)
        time.sleep(10)
