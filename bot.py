import os
import requests
import time
import logging
from collections import deque

# --- بيانات الوصول (Tradovate) ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjXfihEG"
PASSWORD = "V+TT1?8wSnqrv" 

# --- إعدادات تليجرام ---
TG_TOKEN = "0v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

# --- إعدادات الاستراتيجية ---
SYMBOLS = ["ESH6", "NQH6"] 
WINDOW_SECONDS = 30  # تحليل كل 30 ثانية
MIN_MOVE_PCT = 0.0002 # نسبة حركة بسيطة للتجربة

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def send_tg(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except: pass

def get_token():
    url = f"{TRADOVATE_URL}/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=15)
        return res.json().get('accessToken') if res.status_code == 200 else None
    except: return None

def place_order(token, symbol, action):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        acc_res = requests.get(f"{TRADOVATE_URL}/account/list", headers=headers, timeout=10)
        acc_id = acc_res.json()[0]['id']
        payload = {"accountSpec": USERNAME, "accountId": acc_id, "action": action, "symbol": symbol, "orderStrategyTypeId": 1, "orderQty": 1, "orderType": "Market", "isAutomated": True}
        requests.post(f"{TRADOVATE_URL}/order/placeorder", json=payload, headers=headers, timeout=10)
        send_tg(f"✅ تم تنفيذ أمر {action} على {symbol}")
    except: pass

def start_bot():
    token = get_token()
    if not token:
        logging.error("❌ فشل تسجيل الدخول")
        return

    send_tg("🚀 البوت بدأ العمل الآن وسيراقب السوق بشكل دائم...")
    logging.info("✅ متصل وجاري المراقبة...")

    prices = {s: deque(maxlen=60) for s in SYMBOLS}
    
    while True: # حلقة لا نهائية لضمان عدم توقف البوت
        for s in SYMBOLS:
            try:
                headers = {"Authorization": f"Bearer {token}"}
                res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols={s}", headers=headers, timeout=5)
                if res.status_code == 200 and res.json():
                    mid = (res.json()[0]['bidPrice'] + res.json()[0]['askPrice']) / 2
                    prices[s].append(mid)
                    
                    if len(prices[s]) > 10:
                        move = (prices[s][-1] - prices[s][0]) / prices[s][0]
                        if abs(move) >= MIN_MOVE_PCT:
                            place_order(token, s, "Buy" if move > 0 else "Sell")
                            prices[s].clear() # إعادة التصفير بعد الصفقة
            except: 
                token = get_token() # إعادة تجديد التوكن في حال الخطأ
        time.sleep(1)

if __name__ == "__main__":
    start_bot()
