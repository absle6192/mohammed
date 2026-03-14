import os
import requests
import time

# =========================
# TELEGRAM
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": msg
    }

    try:
        requests.post(url, data=data)
    except:
        print("Telegram error")


# =========================
# TRADOVATE SETTINGS
# =========================

USERNAME = os.getenv("TRADOVATE_USERNAME")
PASSWORD = os.getenv("TRADOVATE_PASSWORD")
CID = os.getenv("TRADOVATE_CID")
SECRET = os.getenv("TRADOVATE_SECRET")

BASE_URL = "https://demo.tradovateapi.com"


# =========================
# LOGIN
# =========================

def login_tradovate():

    url = f"{BASE_URL}/v1/auth/accesstokenrequest"

    data = {
        "name": USERNAME,
        "password": PASSWORD,
        "cid": CID,
        "sec": SECRET,
        "deviceId": "bot123"
    }

    try:
        r = requests.post(url, json=data)
        j = r.json()

        if "accessToken" not in j:
            print("LOGIN RESPONSE:", j)
            send_telegram("❌ Tradovate login failed")
            return None

        token = j["accessToken"]

        send_telegram("✅ Connected to Tradovate")
        print("Login success")

        return token

    except Exception as e:

        print("LOGIN FAILED:", e)
        send_telegram("❌ Could not login to Tradovate")

        return None


# =========================
# MAIN
# =========================

def main():

    send_telegram("🚀 Bot started")

    token = login_tradovate()

    if token is None:
        return

    while True:

        print("Bot running...")
        time.sleep(60)


if __name__ == "__main__":
    main()
