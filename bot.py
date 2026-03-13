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
        logging.error(f"❌ فشل الدخول: {res.text}")
        return None
    except Exception as e:
        logging.error(f"🔌 خطأ اتصال: {e}")
        return None

def start_bot():
    logging.info("🚀 بدء تشغيل البوت...")
    send_tg("✅ البوت يحاول الاتصال الآن بعد معالجة خطأ WebSocket.")
    
    while True:
        token = get_token()
        if token:
            logging.info("🔑 تم تجديد التوكن بنجاح")
            headers = {"Authorization": f"Bearer {token}"}
            
            # محاولة جلب السعر بهدوء
            for _ in range(20): # محاولة لـ 20 مرة قبل تجديد التوكن
                try:
                    # نستخدم رمز ESM6 أو ESH6 حسب المتاح في حسابك
                    res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols=ESM6", headers=headers, timeout=10)
                    if res.status_code == 200 and res.json():
                        price = res.json()[0].get('lastPrice')
                        if price:
                            logging.info(f"📈 السعر الحالي: {price}")
                    
                    time.sleep(45) # انتظار 45 ثانية لضمان عدم الحظر (مهم جداً)
                except Exception as e:
                    logging.warning(f"⚠️ محاولة فاشلة: {e}")
                    time.sleep(30)
        else:
            logging.info("⏳ انتظار دقيقتين للمحاولة مرة أخرى...")
            time.sleep(120)

if __name__ == "__main__":
    start_bot()
