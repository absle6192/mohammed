import requests
import time
import logging

# --- بيانات الوصول ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjuXfihEG" 
PASSWORD = "V+TT1?8wSnqrv" 
# إجبار البوت على استخدام حسابك المحدد في الصورة
ACCOUNT_ID_NUMBER = 553939001 
ACCOUNT_SPEC = "MFFUEVRPD553939001"

# --- تليجرام ---
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
    while True:
        token = get_token()
        if token:
            logging.info("✅ متصل بنجاح")
            send_tg(f"🚀 تم الربط بنجاح مع محفظة: {ACCOUNT_SPEC}")
            headers = {"Authorization": f"Bearer {token}"}
            
            while True:
                try:
                    # طلب السعر المباشر
                    res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols=ESH6", headers=headers, timeout=10)
                    if res.status_code == 200 and res.json():
                        price = res.json()[0].get('lastPrice')
                        logging.info(f"📊 السعر الآن: {price}")
                    elif res.status_code == 401: break # تجديد التوكن
                    
                    time.sleep(20) # راحة للسيرفر
                except:
                    time.sleep(10)
                    break 
        else:
            time.sleep(30)

if __name__ == "__main__":
    start_bot()
