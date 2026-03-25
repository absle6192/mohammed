import os
import time
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# --- الإعدادات (تأكد من وجودها في Render) ---
API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- قيم التحكم (الاستراتيجية التراكمية) ---
MIN_ACTIVATION = 5.0      # يبدأ البوت يراقب ويحمي من ربح 5$
STOP_LOSS_VAL = -25.0     # هروب طوارئ لو انعكس السعر بقوة

# مخزن لأعلى ربح وصل له كل سهم
max_profits = {}

def send_tg(text):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                          json={"chat_id": TG_CHAT_ID, "text": text})
        except: pass

def get_dynamic_trail(high_pnl):
    """تحديد المسافة (طول الحبل) بناءً على قوة الربح"""
    if high_pnl >= 100.0:
        return 2.5   # ربح ضخم؟ اقفل عليه بحبل قصير جداً
    elif high_pnl >= 50.0:
        return 5.0   # ربح جيد؟ أعطه مساحة يتنفس (ذبذبة عمالقة السوق)
    elif high_pnl >= 15.0:
        return 4.0   # بداية صعود؟ مسافة متوسطة
    else:
        return 3.0   # ربح بسيط (مثل الـ 9$ اللي سألت عنها)؟ خلك قريب بمسافة 3$

def main():
    client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    print("🚀 Multi-Stage Sniper is Live! Monitoring 30k positions...")
    
    while True:
        try:
            positions = client.get_all_positions()
            active_symbols = [p.symbol for p in positions]
            
            # تنظيف الذاكرة
            for s in list(max_profits.keys()):
                if s not in active_symbols: del max_profits[s]

            for p in positions:
                symbol = p.symbol
                current_pnl = float(p.unrealized_pl)
                
                # تحديث القمة
                if symbol not in max_profits:
                    max_profits[symbol] = current_pnl
                else:
                    max_profits[symbol] = max(max_profits[symbol], current_pnl)

                high_pnl = max_profits[symbol]
                
                # 1. نظام المطاردة التراكمي
                if high_pnl >= MIN_ACTIVATION:
                    trail = get_dynamic_trail(high_pnl)
                    if (high_pnl - current_pnl) >= trail:
                        side = OrderSide.SELL if p.side == 'long' else OrderSide.BUY
                        client.submit_order(MarketOrderRequest(
                            symbol=symbol, qty=abs(float(p.qty)), side=side, time_in_force=TimeInForce.DAY
                        ))
                        send_tg(f"💰 صيد تراكمي ذكي: {symbol}\nالقمة وصل: ${high_pnl:.2f}\nالبيع عند: ${current_pnl:.2f}")
                        continue

                # 2. تأمين التعادل (لو الربح فات 10$ ونزل لـ 1$)
                if high_pnl >= 10.0 and current_pnl <= 1.0:
                    side = OrderSide.SELL if p.side == 'long' else OrderSide.BUY
                    client.submit_order(MarketOrderRequest(symbol=symbol, qty=abs(float(p.qty)), side=side, time_in_force=TimeInForce.DAY))
                    send_tg(f"🛡️ تأمين (منع خسارة): {symbol} أُغلق عند ${current_pnl:.2f}")
                    continue

                # 3. وقف الخسارة الإجباري
                if current_pnl <= STOP_LOSS_VAL:
                    side = OrderSide.SELL if p.side == 'long' else OrderSide.BUY
                    client.submit_order(MarketOrderRequest(symbol=symbol, qty=abs(float(p.qty)), side=side, time_in_force=TimeInForce.DAY))
                    send_tg(f"🛑 خروج طوارئ: {symbol} خسارة ${current_pnl:.2f}")

            time.sleep(0.1) # سرعة فحص فائقة (10 مرات بالثانية)
        except:
            time.sleep(2)

if __name__ == "__main__":
    main()
