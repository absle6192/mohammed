import requests
import os
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    requests.post(url, json=data)

# رسالة تشغيل
send_telegram("🚀 Bot Running")

while True:
    try:
        send_telegram("✅ Loop Working")
        time.sleep(10)

    except Exception as e:
        print(e)
        time.sleep(10)
