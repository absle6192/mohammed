import websocket
import json
import threading
import time
import requests

# --- إعدادات الحساب ---
USERNAME = "MFFUmFjuXfihEG"
PASSWORD = "V+TT1?8wSnqrv"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
TG_TOKEN = "7045330364:AAEm660v5y3RCGT7WsssqoCMEdDE7qjxDNwo"
TG_CHAT_ID = "1682557412"

def send_tg(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": TG_CHAT_ID, "text": text})
    except: pass

def get_token():
    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    res = requests.post(url, json=payload)
    return res.json().get('accessToken') if res.status_code == 200 else None

def on_message(ws, message):
    # هنا يستقبل البوت البيانات الحقيقية
    if "lastPrice" in message:
        data = json.loads(message)
        print(f"📈 السعر الجديد: {data}")

def run_bot():
    token = get_token()
    if not token:
        send_tg("❌ فشل الحصول على التوكن. تأكد من البيانات.")
        return

    # الاتصال عبر WebSocket (الطريقة التي يطلبها السيرفر)
    ws_url = f"wss://demo.tradovateapi.com/v1/websocket"
    ws = websocket.WebSocketApp(ws_url, on_message=on_message)
    
    # إرسال رسالة التوثيق
    auth_msg = f"authorize\n1\n\n{token}"
    
    def on_open(ws):
        ws.send(auth_msg)
        send_tg("✅ البوت اتصل بنظام WebSocket بنجاح! بدأ العمل الحقيقي.")
        # طلب سعر الذهب أو الـ S&P
        ws.send("md/subscribequote\n2\n\n{\"symbol\": \"ESM6\"}")

    ws.on_open = on_open
    ws.run_forever()

if __name__ == "__main__":
    run_bot()
