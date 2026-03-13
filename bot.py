import requests
import time
import logging
from collections import deque

# --- بيانات الوصول (Tradovate) ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjXfihEG"
PASSWORD = "V+TT1?8wSnqrv" 

# --- إعدادات تليجرام ---
TG_TOKEN = "0v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

# --- إعدادات الاستراتيجية ---
SYMBOLS = ["ESH6", "NQH6"] 
MIN_MOVE_PCT = 0.0001 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def send_tg(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except: pass

def get_token():
    url = f"{TRADOVATE_URL}/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            logging.info("✅ تم تسجيل الدخول بنجاح")
            return res.json().get('accessToken')
        return None
    except: return None

def start_bot():
    token = get_token()
    if not token:
        logging.error("❌ فشل تسجيل الدخول عند البداية")
        return

    send_tg("🚀 البوت بدأ العمل الآن بنظام المراقبة الدائمة...")
    prices = {s: deque(maxlen=20) for s in SYMBOLS}
    
    # حلقة لانهائية لضمان عدم إغلاق السيرفر
    while True:
        try:
            for s in SYMBOLS:
                headers = {"Authorization": f"Bearer {token}"}
                res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols={s}", headers=headers, timeout=5)
                if res.status_code == 200 and res.json():
                    mid = (res.json()[0]['bidPrice'] + res.json()[0]['askPrice']) / 2
                    prices[s].append(mid)
                    logging.info(f"مراقبة {s}: السعر الحالي {mid}")
            
            time.sleep(10) # فحص كل 10 ثواني
        except Exception as e:
            logging.error(f"خطأ في الحلقة: {e}")
            token = get_token() # تجديد التوكن عند الخطأ
            time.sleep(5)

if __name__ == "__main__":
    start_bot()
