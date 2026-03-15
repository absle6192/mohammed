import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

last_price = None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    requests.post(url, json=data)


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

        global last_price

        if last_price is not None:

            if price > last_price:

                tp = round(price + 20, 2)
                sl = round(price - 10, 2)

                send_telegram(
f"""📈 LONG NQ

Entry: {price}
TP: {tp}
SL: {sl}"""
                )

            elif price < last_price:

                tp = round(price - 20, 2)
                sl = round(price + 10, 2)

                send_telegram(
f"""📉 SHORT NQ

Entry: {price}
TP: {tp}
SL: {sl}"""
                )

        last_price = price

        time.sleep(60)

    except Exception as e:

        send_telegram(f"⚠️ Error: {e}")
        time.sleep(20)
