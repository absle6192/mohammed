import requests
import time
import logging
from collections import deque

# --- بيانات الوصول (Tradovate) ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
# تم تصحيح اليوزر بناءً على صورة البريد (إضافة حرف u)
USERNAME = "MFFUmFjuXfihEG" 
PASSWORD = "V+TT1?8wSnqrv" 

# --- إعدادات تليجرام ---
TG_TOKEN = "0v5y3RCGT7WsssqoCMEdDE7qjxDNwo" 
TG_CHAT_ID = "1682557412"

# --- إعدادات الاستراتيجية ---
SYMBOLS = ["ESH6", "NQH6"] 
MIN_MOVE_PCT = 0.0001 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

def send_tg(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
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
            logging.info("✅ تم تسجيل الدخول بنجاح")
            return res.json().get('accessToken')
        else:
            logging.error(f"❌ فشل تسجيل الدخول: {res.text}")
            return None
    except Exception as e:
        logging.error(f"❌ خطأ اتصال: {e}")
        return None

def place_order(token, symbol, action):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        # جلب رقم الحساب تلقائياً
        acc_res = requests.get(f"{TRADOVATE_URL}/account/list", headers=headers, timeout=10)
        accounts = acc_res.json()
        if not accounts: return
        acc_id = accounts[0]['id']
        
        payload = {
            "accountSpec": USERNAME,
            "accountId": acc_id,
            "action": action,
            "symbol": symbol,
            "orderStrategyTypeId": 1,
            "orderQty": 1,
            "orderType": "Market",
            "isAutomated": True
        }
        res = requests.post(f"{TRADOVATE_URL}/order/placeorder", json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            msg = f"🔔 أمر {action} نفذ على {symbol}!"
            logging.info(msg)
            send_tg(msg)
    except Exception as e:
        logging.error(f"❌ خطأ في تنفيذ الأمر: {e}")

def start_bot():
    while True:
        token = get_token()
        if token:
            send_tg("🚀 البوت متصل الآن ويراقب السوق...")
            prices = {s: deque(maxlen=20) for s in SYMBOLS}
            
            while True:
                try:
                    for s in SYMBOLS:
                        headers = {"Authorization": f"Bearer {token}"}
                        res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols={s}", headers=headers, timeout=5)
                        if res.status_code == 200 and res.json():
                            data = res.json()[0]
                            mid = (data['bidPrice'] + data['askPrice']) / 2
                            prices[s].append(mid)
                            logging.info(f"مراقبة {s}: {mid}")
                            
                            if len(prices[s]) >= 15:
                                move = (prices[s][-1] - prices[s][0]) / prices[s][0]
                                if abs(move) >= MIN_MOVE_PCT:
                                    action = "Buy" if move > 0 else "Sell"
                                    place_order(token, s, action)
                                    prices[s].clear() # تصفير الذاكرة بعد الصفقة
                    time.sleep(10)
                except Exception as e:
                    logging.error(f"⚠️ تنبيه: {e}")
                    break 
        else:
            logging.error("❌ فشل الدخول.. محاولة جديدة بعد 30 ثانية")
            time.sleep(30)

if __name__ == "__main__":
    start_bot()
