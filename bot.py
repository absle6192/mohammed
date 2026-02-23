import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات التنبيهات الفنية ---
RSI_MAX_LONG = 68   
RSI_MIN_SHORT = 35  
MA_WINDOW = 20      

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    try: 
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, 
                      timeout=5) # تقليل المهلة لسرعة التنفيذ
    except Exception as e:
        logging.error(f"Telegram Error: {e}")

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "📡 *الرادار المطور يعمل*\nتم ضبط توقيت الدخول اللحظي ⏱️")

    last_alert_time = {ticker: datetime.min for ticker in TICKERS}

    while True:
        try:
            # --- التطوير 1: مزامنة الوقت للوصول في أول 5 ثوانٍ ---
            now_local = datetime.now()
            wait_time = 60 - now_local.second
            if wait_time > 0:
                time.sleep(wait_time) # الانتظار حتى بداية الدقيقة القادمة بالضبط

            now_utc = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now_utc - timedelta(minutes=45), end=now_utc, feed="iex"
            )).df

            for sym in TICKERS:
                if sym not in bars_df.index: continue
                df = bars_df.xs(sym).sort_index()
                if len(df) < 21: continue

                df['rsi'] = calculate_rsi(df['close'])
                current_rsi = df['rsi'].iloc[-1]
                prev_rsi = df['rsi'].iloc[-2] # قيمة RSI للدقيقة السابقة
                price_now = float(df["close"].iloc[-1])
                ma_price = df["close"].iloc[-MA_WINDOW:-1].mean()

                alert_triggered = False
                msg = ""

                # --- التطوير 2: إضافة شرط اتجاه الـ RSI (Slope) ---
                # شراء: السعر فوق المتوسط + RSI مقبول + RSI بدأ يصعد
                if price_now > ma_price and current_rsi < RSI_MAX_LONG and current_rsi > prev_rsi:
                    msg = (f"🚀 *LONG (شراء): {sym}*\n"
                           f"💰 السعر: {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f} ↗️")
                    alert_triggered = True

                # بيع: السعر تحت المتوسط + RSI فوق القاع + RSI بدأ يهبط
                elif price_now < ma_price and current_rsi > RSI_MIN_SHORT and current_rsi < prev_rsi:
                    msg = (f"📉 *SHORT (بيع): {sym}*\n"
                           f"💰 السعر: {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f} ↘️")
                    alert_triggered = True

                if alert_triggered:
                    # تقليل وقت منع التكرار لـ 5 دقائق (أفضل للمضاربة السريعة)
                    if (datetime.now() - last_alert_time[sym]).total_seconds() > 300: 
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
                        last_alert_time[sym] = datetime.now()

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
