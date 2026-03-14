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
    response = requests.get(url)
    data = response.json()

    result = data["quoteResponse"]["result"]

    if len(result) == 0:
        return None

    return result[0]["regularMarketPrice"]


while True:
    try:

        price = get_price()

        if price is None:
            print("No price received")
            time.sleep(60)
            continue

        print("Current price:", price)

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
