import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("TWELVE_API_KEY")

last_price = None


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


def get_price():

    try:
        url = f"https://api.twelvedata.com/price?symbol=NQ&apikey={API_KEY}"
        r = requests.get(url)
        data = r.json()

        return float(data["price"])

    except:
        return None


send_telegram("🚀 NQ Bot Started")

while True:

    try:

        price = get_price()

        if price is None:
            time.sleep(30)
            continue

        send_telegram(f"💰 NQ Price: {price}")

        if last_price:

            if price > last_price:

                send_telegram(f"""
📈 LONG NQ

Entry: {price}
TP: {price+20}
SL: {price-10}
""")

            elif price < last_price:

                send_telegram(f"""
📉 SHORT NQ

Entry: {price}
TP: {price-20}
SL: {price+10}
""")

        last_price = price

        time.sleep(60)

    except:
        time.sleep(30)
