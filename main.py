import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

last_price = None


def send_telegram(message):

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=10)

    except Exception as e:
        print("Telegram error:", e)


def get_price():

    try:

        url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=NQ=F"

        r = requests.get(url, timeout=10)

        data = r.json()

        result = data["quoteResponse"]["result"]

        if len(result) == 0:
            return None

        return result[0]["regularMarketPrice"]

    except Exception as e:

        print("Price error:", e)

        return None


send_telegram("🚀 NQ Trading Bot Started")

while True:

    try:

        price = get_price()

        if price is None:
            print("No price received")
            time.sleep(30)
            continue

        send_telegram(f"💰 NQ Price: {price}")

        if last_price is not None:

            if price > last_price:

                send_telegram(
f"""📈 LONG NQ

Entry: {price}
TP: {price+20}
SL: {price-10}
"""
)

            elif price < last_price:

                send_telegram(
f"""📉 SHORT NQ

Entry: {price}
TP: {price-20}
SL: {price+10}
"""
)

        last_price = price

        time.sleep(60)

    except Exception as e:

        print("Main loop error:", e)

        time.sleep(30)
