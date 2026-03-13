import requests
import time
import logging

# --- إعدادات الحساب ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjuXfihEG" 
PASSWORD = "V+TT1?8wSnqrv" 

# --- إعدادات تليجرام ---
TG_TOKEN = "7045330364:AAEm660v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def send_tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except: pass

def get_token():
    url = f"{TRADOVATE_URL}/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            return res.json().get('accessToken')
        return None
    except: return None

def start_bot():
    send_tg("🤖 نبض البوت: أنا شغال الآن وبدأت أول محاولة اتصال...")
    
    while True:
        token = get_token()
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            # جربنا الرموز الأكثر شيوعاً لعقد الـ S&P 500
            for sym in ["ESM6", "ESH6", "ESZ5"]: 
                try:
                    res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols={sym}", headers=headers, timeout=10)
                    if res.status_code == 200 and res.json():
                        price = res.json()[0].get('lastPrice')
                        if price:
                            send_tg(f"📈 سعر {sym} الحالي هو: {price}")
                            break
                except: continue
        
        # رسالة طمأنينة كل 5 دقائق في حال عدم وجود سعر
        time.sleep(300) 
        send_tg("🔄 البوت لا يزال يعمل في الخلفية ويحاول جلب البيانات...")

if __name__ == "__main__":
    start_bot()
