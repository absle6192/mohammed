import requests
import time
import logging

# --- بيانات الوصول ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjuXfihEG" 
PASSWORD = "V+TT1?8wSnqrv" 
ACCOUNT_SPEC = "MFFUEVRPD553939001"

# --- تليجرام ---
TG_TOKEN = "7045330364:AAEm660v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def send_tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": TG_CHAT_ID, "text": text}, timeout=5)
    except: pass

def get_token():
    url = f"{TRADOVATE_URL}/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            return res.json().get('accessToken')
        logging.error(f"❌ فشل تسجيل الدخول: {res.text}")
        return None
    except: return None

def start_bot():
    while True:
        token = get_token()
        if token:
            logging.info("✅ تم الاتصال بنجاح!")
            send_tg("🚀 البوت يعمل الآن ويراقب السوق...")
            headers = {"Authorization": f"Bearer {token}"}
            
            while True:
                try:
                    # محاولة جلب السعر لرمز الـ S&P 500
                    # ملاحظة: تأكد من رمز العقد الحالي (مثل ESH6)
                    res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols=ESH6", headers=headers, timeout=10)
                    
                    if res.status_code == 200 and res.json():
                        data = res.json()[0]
                        price = data.get('lastPrice') or data.get('bidPrice')
                        logging.info(f"📊 السعر الحالي لـ ESH6 هو: {price}")
                    elif res.status_code == 401: # التوكن انتهى
                        break
                    else:
                        logging.warning("⚠️ في انتظار بيانات السوق...")
                    
                    time.sleep(15) # فحص كل 15 ثانية
                except Exception as e:
                    logging.error(f"⚠️ خطأ أثناء المراقبة: {e}")
                    break
        else:
            time.sleep(30)

if __name__ == "__main__":
    start_bot()
