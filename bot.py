import os
import requests
import time
import math
import logging
from collections import deque

# --- بيانات الوصول (تم تعبئتها من صورك) ---
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
APP_ID = "MyBot"
API_SECRET = "29841443-34e8-4660-8488-87425f18c213"
USERNAME = "MFFUmFjXfihEG"
PASSWORD = "V+TT1?8wSnqrv" # الباسورد من صورتك الأخيرة

# --- إعدادات تليجرام (هذه الوحيدة التي تحتاج وضعها يدوياً) ---
TG_TOKEN = "ضع_توكن_تليجرام_هنا"
TG_CHAT_ID = "ضع_ID_حسابك_هنا"

# --- إعدادات الاستراتيجية ---
SYMBOLS = ["ESH6", "NQH6"] 
WINDOW_SECONDS = 45 
MIN_POINTS = 20
MIN_MOVE_PCT = 0.0006
MAX_SPREAD_PCT = 0.0025

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

class SymState:
    def __init__(self):
        self.mids = deque(maxlen=600)
        self.last_spread = 0.0

state = {s: SymState() for s in SYMBOLS}

def send_tg(text):
    if TG_TOKEN and TG_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT_ID, "text": text})
        except: pass

def get_token():
    url = f"{TRADOVATE_URL}/auth/accesstokenrequest"
    payload = {"name": USERNAME, "password": PASSWORD, "appId": APP_ID, "appVersion": "1.0", "cid": 0, "sec": API_SECRET}
    try:
        res = requests.post(url, json=payload)
        return res.json().get('accessToken')
    except: return None

def place_order(token, symbol, action):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        acc_id = requests.get(f"{TRADOVATE_URL}/account/list", headers=headers).json()[0]['id']
        payload = {
            "accountSpec": USERNAME, "accountId": acc_id, "action": action,
            "symbol": symbol, "orderStrategyTypeId": 1, "orderQty": 1,
            "orderType": "Market", "isAutomated": True
        }
        res = requests.post(f"{TRADOVATE_URL}/order/placeorder", json=payload, headers=headers)
        if res.status_code == 200:
            send_tg(f"✅ تم تنفيذ أمر {action} على {symbol}")
    except: pass

def run_strategy(token):
    headers = {"Authorization": f"Bearer {token}"}
    start_time = time.time()
    
    while time.time() - start_time < WINDOW_SECONDS:
        for s in SYMBOLS:
            res = requests.get(f"{TRADOVATE_URL}/md/getquotes?symbols={s}", headers=headers)
            if res.status_code == 200 and res.json():
                data = res.json()[0]
                bid, ask = data.get('bidPrice', 0), data.get('askPrice', 0)
                if bid and ask:
                    mid = (bid + ask) / 2.0
                    state[s].mids.append(mid)
                    state[s].last_spread = (ask - bid) / mid
        time.sleep(1)
    
    for s in SYMBOLS:
        st = state[s]
        if len(st.mids) < MIN_POINTS: continue
        move = (st.mids[-1] - st.mids[0]) / st.mids[0]
        if abs(move) >= MIN_MOVE_PCT and st.last_spread <= MAX_SPREAD_PCT:
            place_order(token, s, "Buy" if move > 0 else "Sell")

if __name__ == "__main__":
    token = get_token()
    if token:
        run_strategy(token)
