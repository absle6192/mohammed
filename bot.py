import websocket
import json
import os
import requests
import time

# جلب الإعدادات
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
    print(f"📥 رسالة جديدة: {message}")
    # إذا كانت الرسالة نصية وليست JSON، قد نحتاج لمعالجتها بشكل مختلف
    try:
        data = json.loads(message)
        if "status" in data and data["status"] == "connection_accepted":
            send_telegram("✅ تم الاتصال بنجاح بـ Rithmic!")
    except:
        pass

def on_open(ws):
    print("🚀 جاري إرسال بيانات الدخول...")
    auth_data = {
        "user": USER,
        "password": PASS,
        "system": "Rithmic Paper Trading",
        "app_id": "DEMA",
        "version": "1.0"
    }
    ws.send(json.dumps(auth_data))

def on_error(ws, error):
    print(f"❌ خطأ: {error}")

def on_close(ws, close_status_code, close_msg):
    print("🔌 تم إغلاق الاتصال، سأحاول مجدداً بعد 5 ثواني...")
    time.sleep(5)

if __name__ == "__main__":
    uri = "wss://paper-trading.rithmic.com:443"
    
    # حلقة لا نهائية عشان السيرفر ما يقفل أبداً
    while True:
        try:
            ws = websocket.WebSocketApp(uri, 
                                      on_open=on_open, 
                                      on_message=on_message, 
                                      on_error=on_error, 
                                      on_close=on_close)
            ws.run_forever()
        except Exception as e:
            print(f"Restarting due to error: {e}")
            time.sleep(5)
