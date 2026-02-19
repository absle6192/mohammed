import os
import time
import requests
import logging
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest 
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# إعداد السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ===================== إعدادات بوت القنوع (المتوازن) =====================
TRADE_AMOUNT = 15000.0       # مبلغ معقول لكل صفقة
MAX_POSITIONS = 3            # بحد أقصى 3 صفقات لزيادة الفرص دون مخاطرة عالية
STOP_LOSS_PCT = 0.010        # وقف خسارة 1% (يحميك من التقلبات المفاجئة)
TAKE_PROFIT_PCT = 0.015      # هدف ربح 1.5% (هدف واقعي يسهل تحقيقه)

# فلاتر الدخول (الفرص الحقيقية فقط)
MIN_PRICE_DIFF = 0.001       # 0.1% اختراق سعري
MIN_VOL_RATIO = 1.5          # سيولة أعلى من المتوسط بـ 50%

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except: pass

def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    IS_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]

    trader = TradingClient(API_KEY, SECRET_KEY, paper=IS_PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    logging.info("⚖️ تشغيل بوت القنوع: تداول آلي مستمر بأهداف واقعية")
    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "⚖️ تم تشغيل (بوت القنوع): أهداف واقعية 1.5% وحماية 1% - تداول آلي مستمر")

    while True:
        try:
            clock = trader.get_clock()
            if not clock.is_open:
                time.sleep(60)
                continue

            positions = trader.get_all_positions()
            
            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=30), end=now, feed="iex"
            )).df

            if bars_df is None or bars_df.empty:
                time.sleep(15)
                continue

            for sym in TICKERS:
                if sym not in bars_df.index: continue
                df = bars_df.xs(sym).sort_index().ffill()
                if len(df) < 10: continue

                price_now = float(df["close"].iloc[-1])
                ma_price = df["close"].iloc[-10:-1].mean()
                price_diff = (price_now - ma_price) / ma_price
                
                vol_now = float(df["volume"].iloc[-1])
                vol_avg = df["volume"].iloc[-10:-1].mean()
                vol_ratio = vol_now / vol_avg

                # تنفيذ استراتيجية القنوع
                if len(positions) < MAX_POSITIONS:
                    if any(p.symbol == sym for p in positions): continue

                    # شرط الدخول: اختراق سعري مع سيولة جيدة
                    if price_diff >= MIN_PRICE_DIFF and vol_ratio >= MIN_VOL_RATIO:
                        qty = int(TRADE_AMOUNT / price_now)
                        if qty <= 0: continue

                        # حساب الأسعار بدقة
                        limit_entry = round(price_now, 2) 
                        tp_price = round(limit_entry * (1 + TAKE_PROFIT_PCT), 2)
                        sl_price = round(limit_entry * (1 - STOP_LOSS_PCT), 2)

                        # إرسال الأمر الآلي
                        trader.submit_order(LimitOrderRequest(
                            symbol=sym, qty=qty, side=OrderSide.BUY,
                            limit_price=limit_entry,
                            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                            take_profit={'limit_price': tp_price}, 
                            stop_loss={'stop_price': sl_price}
                        ))
                        
                        msg = f"✅ دخول آلي (قنوع): {sym}\nالسعر: {limit_entry}\nالهدف: {tp_price}\nالحماية: {sl_price}"
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
                        logging.info(msg)

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(30)
        time.sleep(20)

if __name__ == "__main__":
    main()
