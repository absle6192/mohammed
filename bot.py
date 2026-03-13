import requests
import time

# بياناتك الصحيحة من السجلات
USERNAME = "MFFUmFjuXfihEG"
PASSWORD = "V+TT1?8wSnqrv"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
TG_TOKEN = "7045330364:AAEm660v5y3RCGT7WsssqoCMEdDE7qjxDNwo"
TG_CHAT_ID = "1682557412"

def send_tg(text):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except: pass

def run_bot():
    send_tg("🚀 بدأت محاولة الاتصال من السيرفر الجديد...")
    while True:
        try:
            url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
            payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
            res = requests.post(url, json=payload, timeout=15)
            
            if res.status_code == 200:
                token = res.json().get('accessToken')
                send_tg(f"✅ تم الدخول بنجاح! التوكن شغال.")
                # هنا نضع كود جلب السعر لاحقاً، المهم الآن استقرار الاتصال
            else:
                print(f"❌ خطأ من السيرفر: {res.status_code}")
                
        except Exception as e:
            print(f"⚠️ خطأ تقني: {e}")
        
        # الانتظار لمدة 5 دقائق بين كل محاولة لعدم التعرض للحظر مرة أخرى
        time.sleep(300) 

if __name__ == "__main__":
    run_bot()
