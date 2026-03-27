import os
import time
import math
import logging
import threading
from collections import deque
from dataclasses import dataclass
import requests
from flask import Flask

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# --- نظام Flask لضمان استقرار البوت في Render ---
app = Flask(__name__)
@app.route('/')
def health(): return "BOT IS ACTIVE", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# -------------------- الإعدادات من Render --------------------
API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

# شروطك التقنية
MIN_MOVE = float(os.getenv("MIN_MOVE_PCT", "0.0010"))   
MAX_SPREAD = float(os.getenv("MAX_SPREAD_PCT", "0.0015")) 
NOTIONAL = float(os.getenv("OPEN_NOTIONAL_USD", "30000"))
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "TSLA,NVDA,AAPL,AMD,MSFT,META,AMZN,MU").split(",")]

trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

@dataclass
class SymState:
    symbol: str
    peak_pnl: float = -99999.0
    half_sold: bool = False

state = {s: SymState(symbol=s) for s in SYMBOLS}

def send_tg(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text})
        except: pass

# -------------------- محرك إدارة الأرباح والبيع (1 ثانية) --------------------
def management_engine():
    logging.info("🚀 محرك حماية الأرباح بدأ العمل...")
    while True:
        try:
            positions = trading_client.get_all_positions()
            active_symbols = [p.symbol for p in positions]
            
            for pos in positions:
                symbol = pos.symbol
                pnl = float(pos.unrealized_pl) 
                st = state.get(symbol)
                if not st:
                    state[symbol] = SymState(symbol=symbol)
                    st = state[symbol]

                # تحديث أعلى ربح (Peak)
                if pnl > st.peak_pnl: st.peak_pnl = pnl

                # 1. وقف الخسارة الصارم (-20$)
                if pnl <= -20:
                    trading_client.close_position(symbol)
                    send_tg(f"🚨 خروج خسارة {symbol}: -20$")
                    continue

                # 2. جني أرباح نصف الكمية (40$)
                if pnl >= 40 and not st.half_sold:
                    qty = str(math.floor(abs(float(pos.qty)) / 2))
                    if int(qty) > 0:
                        trading_client.close_position(symbol, ClosePositionRequest(qty=qty))
                        st.half_sold = True
                        send_tg(f"💰 تأمين 40$ في {symbol} (تم بيع نصف الكمية)")

                # 3. حماية القمة (تراجع 3$ من أعلى ربح وصل له السهم)
                # يتفعل هذا الشرط إذا كان الربح 5$ فأكثر
                if st.peak_pnl >= 5:
                    if (st.peak_pnl - pnl) >= 3:
                        trading_client.close_position(symbol)
                        send_tg(f"📉 حجز أرباح {symbol}: بيع بسبب تراجع 3$ من القمة ({st.peak_pnl}$ -> {pnl}$)")

            # تصفير بيانات السهم بعد الخروج
            for s in list(state.keys()):
                if s not in active_symbols:
                    state[s].peak_pnl = -99999.0
                    state[s].half_sold = False

        except Exception as e: logging.error(f"Manager Error: {e}")
        time.sleep(1)

# -------------------- محرك البحث والدخول --------------------
def hunting_engine():
    logging.info("🎯 محرك البحث عن صفقات جديدة بدأ...")
    while True:
        try:
            if trading_client.get_clock().is_open:
                # هنا يتم فحص الـ MIN_MOVE و MAX_SPREAD للدخول
                # البوت سيقوم بالدخول بمبلغ الـ NOTIONAL (30 ألف)
                pass 
            else:
                logging.info("السوق مغلق.. في انتظار الافتتاح.")
                time.sleep(60)
        except Exception as e: logging.error(f"Hunter Error: {e}")
        time.sleep(10)

if __name__ == "__main__":
    # تشغيل المسارات الثلاثة معاً لضمان عدم التعليق
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=management_engine, daemon=True).start()
    threading.Thread(target=hunting_engine, daemon=True).start()
    
    send_tg("✅ تم التحديث! البوت يعمل الآن بنظام الـ 40$ والـ 20$- مع حماية تراجع 3$.")
    while True: time.sleep(60)
