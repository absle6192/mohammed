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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def get_token():
    url = f"{TRADOVATE_URL}/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=15)
        # هنا سنطبع الرد كاملاً لنعرف السبب 100%
        logging.info(f"🔍 محاولة تسجيل الدخول.. كود الرد: {res.status_code}")
        if res.status_code == 200:
            return res.json().get('accessToken')
        else:
            logging.error(f"❌ خطأ من السيرفر: {res.text}")
            return None
    except Exception as e:
        logging.error(f"🔌 خطأ في الاتصال: {str(e)}")
        return None

def start_bot():
    token = get_token()
    if token:
        logging.info("✅ تم الحصول على التوكن بنجاح!")
        headers = {"Authorization": f"Bearer {token}"}
        # محاولة فحص الداتا
        res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols=ESH6", headers=headers)
        logging.info(f"📊 فحص بيانات السوق.. الرد: {res.text}")
    else:
        logging.error("⛔ توقف البوت بسبب فشل الحصول على التوكن")

if __name__ == "__main__":
    start_bot()
