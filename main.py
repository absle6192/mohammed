import websocket
import json
import os
import requests
import time

# جلب الإعدادات من Koyeb (تأكد من وجودها في Environment Variables)
USER = os.getenv('RITHMIC_USER')
PASS = os.getenv('RITHMIC_PASS')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": message})
    except Exception as e:
        print(f"Telegram Error: {e}")

def on_message(ws, message):
    print(f"📥 رسالة من ريثميك: {message}")
    try:
        data = json.loads(message)
        # التحقق من نجاح الاتصال
        if "status" in data and data["status"] == "connection_accepted":
            send_telegram("✅ تم الاتصال بنجاح بسيرفر ريثميك المباشر!")
            print("✅ تم الاتصال وتسجيل الدخول!")
    except:
        pass

def on_open(ws):
    print("🚀 جاري محاولة تسجيل الدخول عبر IP المباشر...")
    auth_data = {
        "user": USER,
        "password": PASS,
        "system": "NinjaTrader Continuum",
        "app_id": "DEMA",
        "version": "1.0"
    }
    ws.send(json.dumps(auth_data))

def on_error(ws, error):
    print(f"❌ خطأ في الاتصال: {error}")

def on_close(ws, close_status_code, close_msg):
    print("🔌 انقطع الاتصال، سأحاول مجدداً بعد 5 ثواني...")
    time.sleep(5)

if __name__ == "__main__":
    # استخدام الـ IP المباشر لتخطي مشكلة "Name or service not known"
    uri = "wss://104.129.182.25:443"
    
    while True:
        try:
            ws = websocket.WebSocketApp(uri, 
                                      on_open=on_open, 
                                      on_message=on_message, 
                                      on_error=on_error, 
                                      on_close=on_close)
            ws.run_forever()
        except Exception as e:
            print(f"Restarting... {e}")
            time.sleep(5)
