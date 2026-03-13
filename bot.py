import websocket
import json
import requests
import time
import threading

# --- البيانات الأساسية ---
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

def get_token():
    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=15)
        return res.json().get('accessToken') if res.status_code == 200 else None
    except: return None

def on_message(ws, message):
    # استقبال البيانات وعرضها في السجلات
    print(f"📥 بيانات مستلمة: {message}")
    if "lastPrice" in message:
        send_tg(f"🎯 تحديث سعر الماركت: {message}")

def on_error(ws, error):
    print(f"❌ خطأ في الاتصال: {error}")

def on_close(ws, close_status_code, close_msg):
    print("🔌 تم إغلاق الاتصال.. إعادة محاولة بعد 30 ثانية")
    time.sleep(30)
    start_connection()

def on_open(ws):
    print("✅ تم فتح الاتصال بنجاح!")
    token = get_token()
    if token:
        # التوثيق والاشتراك في سعر الذهب/الأسهم
        auth_msg = f"authorize\n1\n\n{token}"
        ws.send(auth_msg)
        # الاشتراك في رمز ESM6 (تأكد من الرمز من حسابك)
        ws.send("md/subscribequote\n2\n\n{\"symbol\": \"ESM6\"}")
        send_tg("🚀 البوت الآن متصل رسمياً بنظام الأسعار المباشر!")

def start_connection():
    ws_url = "wss://demo.tradovateapi.com/v1/websocket"
    ws = websocket.WebSocketApp(ws_url,
                              on_open=on_open,
                              on_message=on_message,
                              on_error=on_error,
                              on_close=on_close)
    ws.run_forever()

if __name__ == "__main__":
    # تشغيل البوت في خيط منفصل لضمان عدم توقفه
    print("🎬 بدء تشغيل محرك البوت...")
    while True:
        try:
            start_connection()
        except Exception as e:
            print(f"⚠️ خلل مفاجئ: {e}")
            time.sleep(60)
