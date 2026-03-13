import requests
import time
import logging
from collections import deque

# --- بيانات الوصول (Tradovate) ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
# تم تصحيح اليوزر بزيادة حرف u بناءً على صورة البريد
USERNAME = "MFFUmFjuXfihEG" 
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
    payload = {
        "name": USERNAME, 
        "password": PASSWORD, 
        "appId": APP_ID, 
        "appVersion": "1.0", 
        "cid": 0, 
        "sec": API_SECRET
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            logging.info("✅ تم تسجيل الدخول بنجاح")
            return res.json().get('accessToken')
        else:
            logging.error(f"❌ فشل تسجيل الدخول: {res.text}")
            return None
    except Exception as e:
        logging.error(f"❌ خطأ اتصال: {e}")
        return None

def start_bot():
    # حلقة خارجية لضمان بقاء السيرفر يعمل حتى لو حدث خطأ
    while True:
        token = get_token()
        if token:
            send_tg("🚀 تم الاتصال بنجاح! البوت بدأ بمراقبة السوق الآن...")
            prices = {s: deque(maxlen=20) for s in SYMBOLS}
            
            while True: # حلقة المراقبة
                try:
                    for s in SYMBOLS:
                        headers = {"Authorization": f"Bearer {token}"}
                        res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols={s}", headers=headers, timeout=5)
                        if res.status_code == 200 and res.json():
                            mid = (res.json()[0]['bidPrice'] + res.json()[0]['askPrice']) / 2
                            prices[s].append(mid)
                            logging.info(f"مراقبة {s}: السعر الحالي {mid}")
                    time.sleep(10)
                except Exception as e:
                    logging.error(f"حدث خطأ أثناء المراقبة: {e}")
                    break # ارجع للحلقة الخارجية لتجديد التوكن
        else:
            logging.error("❌ فشل تسجيل الدخول.. سأحاول مرة أخرى بعد 30 ثانية")
            time.sleep(30)

if __name__ == "__main__":
    start_bot()
