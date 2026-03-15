import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

last_price = None


def send_telegram(message):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}

    try:
        requests.post(url, json=data, timeout=10)
    except:
        print("Telegram connection error")


def get_price():

    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=NQ=F"

    r = requests.get(url, timeout=10)
    data = r.json()

    return data["quoteResponse"]["result"][0]["regularMarketPrice"]


send_telegram("🚀 NQ Trading Bot Started")

while True:

    try:

        price = get_price()

        send_telegram(f"💰 NQ Price: {price}")

        if last_price is not None:

            if price > last_price:

                tp = price + 20
                sl = price - 10

                send_telegram(
f"""📈 LONG NQ

Entry: {price}
TP: {tp}
SL: {sl}
"""
)

            elif price < last_price:

                tp = price - 20
                sl = price + 10

                send_telegram(
f"""📉 SHORT NQ

Entry: {price}
TP: {tp}
SL: {sl}
"""
)

        last_price = price

        time.sleep(60)

    except Exception as e:

        print("Error:", e)
        time.sleep(15)
