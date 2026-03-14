import os
import requests
import time

# ==============================
# Telegram settings
# ==============================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    data = {
        "chat_id": CHAT_ID,
        "text": message
    }

    try:
        requests.post(url, data=data)
    except:
        print("Telegram error")


# ==============================
# Tradovate login
# ==============================

USERNAME = os.getenv("TRADOVATE_USERNAME")
PASSWORD = os.getenv("TRADOVATE_PASSWORD")
CID = os.getenv("TRADOVATE_CID")
SECRET = os.getenv("TRADOVATE_SECRET")

def login_tradovate():

    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"

    data = {
        "name": USERNAME,
        "password": PASSWORD,
        "cid": CID,
        "sec": SECRET,
        "deviceId": "bot"
    }

    r = requests.post(url, json=data)

    try:
        token = r.json()["accessToken"]
        print("Login success")
        send_telegram("✅ Bot connected to Tradovate")
        return token

    except:
        print("LOGIN FAILED")
        send_telegram("❌ Could not login to Tradovate")
        return None


# ==============================
# Main
# ==============================

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
