import requests
import time
import logging

# --- بيانات الوصول ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjuXfihEG" 
PASSWORD = "V+TT1?8wSnqrv" 
# رقم الحساب الذي ظهر في صورتك
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
            return res.json().get('accessToken')
        logging.error(f"❌ فشل الدخول: {res.text}")
        return None
    except: return None

def start_bot():
    while True:
        token = get_token()
        if token:
            logging.info("✅ تم تسجيل الدخول بنجاح")
            send_tg(f"🚀 البوت متصل الآن ويراقب الحساب: {ACCOUNT_SPEC}")
            
            headers = {"Authorization": f"Bearer {token}"}
            
            while True:
                try:
                    # محاولة جلب السعر لعقد الـ S&P 500
                    # تأكد من رمز العقد في منصتك، جرب "ES" أو "ESH6"
                    res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols=ESH6", headers=headers, timeout=10)
                    
                    if res.status_code == 200 and res.json():
                        data = res.json()[0]
                        price = data.get('lastPrice') or data.get('bidPrice')
                        if price:
                            logging.info(f"📊 السعر الحالي: {price}")
                        else:
                            logging.warning("⚠️ لا توجد داتا حالياً (تأكد من فتح السوق)")
                    elif res.status_code == 401: # التوكن انتهى
                        logging.info("🔄 تجديد الاتصال...")
                        break
                    else:
                        logging.warning(f"⚠️ رد السيرفر: {res.status_code}")
                    
                    time.sleep(20) # فحص كل 20 ثانية لتجنب الحظر
                except Exception as e:
                    logging.error(f"⚠️ خطأ مؤقت: {e}")
                    time.sleep(10)
                    continue 
        else:
            logging.error("❌ فشل تسجيل الدخول.. سأحاول مجدداً بعد 30 ثانية")
            time.sleep(30)

if __name__ == "__main__":
    start_bot()
