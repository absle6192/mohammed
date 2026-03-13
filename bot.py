import requests
import time

# --- البيانات الأساسية ---
USERNAME = "MFFUmFjuXfihEG" 
PASSWORD = "V+TT1?8wSnqrv" 
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
TG_TOKEN = "7045330364:AAEm660v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

def send_tg(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text})

def check_connection():
    # محاولة الحصول على التوكن فقط
    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
    payload = {
        "name": USERNAME, "password": PASSWORD, 
        "appId": APP_ID, "appVersion": "1.0", 
        "cid": 0, "sec": API_SECRET
    }
    
    try:
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            send_tg("✅ تم كسر الدائرة! البوت متصل الآن بنجاح.")
            print("Success!")
        else:
            send_tg(f"❌ فشل الاتصال: {res.status_code}")
    except Exception as e:
        send_tg(f"⚠️ خطأ تقني: {e}")

if __name__ == "__main__":
    while True:
        check_connection()
        time.sleep(600) # محاولة كل 10 دقائق فقط لتهدئة السيرفر
