import requests
import time

# بياناتك كما هي
TG_TOKEN = "7045330364:AAEm660v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

def send_test():
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID, 
        "text": "🚀 يا إياد، إذا وصلت هذه الرسالة فالاتصال سليم 100% والسيرفر الأمريكي شغال!"
    }
    try:
        res = requests.post(url, json=payload)
        print(f"Response: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    send_test()
    # حلقة بسيطة ليبقى السيرفر Healthy
    while True:
        time.sleep(60)
