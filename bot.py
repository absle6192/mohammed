import os
import requests
import time

BASE_URL = os.getenv("TRADOVATE_BASE_URL")
USERNAME = os.getenv("TRADOVATE_USERNAME")
PASSWORD = os.getenv("TRADOVATE_PASSWORD")
ACCOUNT_SPEC = os.getenv("TRADOVATE_ACCOUNT_SPEC")
DEVICE_ID = os.getenv("TRADOVATE_DEVICE_ID")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "NQ"
CONTRACTS = 2


def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, json=payload)
    except:
        pass


def login():
    url = f"{BASE_URL}/v1/auth/accesstokenrequest"

    payload = {
        "name": USERNAME,
        "password": PASSWORD,
        "cid": 0,
        "sec": 0,
        "deviceId": DEVICE_ID
    }

    r = requests.post(url, json=payload)
    token = r.json()["accessToken"]

    send_telegram("✅ Connected to Tradovate")

    return token


def get_account_id(token):

    url = f"{BASE_URL}/v1/account/list"

    headers = {"Authorization": f"Bearer {token}"}

    r = requests.get(url, headers=headers)

    for acc in r.json():
        if acc["name"] == ACCOUNT_SPEC:
            return acc["id"]

    raise Exception("Account not found")


def place_order(token, account_id, action):

    url = f"{BASE_URL}/v1/order/placeorder"

    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "accountId": account_id,
        "action": action,
        "symbol": SYMBOL,
        "orderQty": CONTRACTS,
        "orderType": "Market"
    }

    r = requests.post(url, json=payload, headers=headers)

    send_telegram(f"📈 Order sent: {action} {CONTRACTS} {SYMBOL}")


def main():

    send_telegram("🚀 Trading bot started")

    token = login()

    account_id = get_account_id(token)

    send_telegram("📊 Account connected")

    while True:

        try:

            # مثال: يفتح صفقة شراء للتجربة
            place_order(token, account_id, "Buy")

            time.sleep(60)

        except Exception as e:

            send_telegram(f"⚠️ Error: {str(e)}")

            time.sleep(30)


if __name__ == "__main__":
    main()
