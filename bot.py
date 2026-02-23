import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- إعدادات السجلات ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات الاستراتيجية ---
RSI_MAX_LONG = 68   # الحد الأقصى للـ RSI للشراء (تجنب القمم)
RSI_MIN_SHORT = 35  # الحد الأدنى للـ RSI للبيع (تجنب القيعان)
MA_WINDOW = 20      # متوسط الحركة لـ 20 دقيقة
VOL_MULTIPLIER = 1.3 # تنبيه إذا كان الفوليوم أعلى بـ 30% من المتوسط

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    try: 
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, 
                      timeout=5)
    except Exception as e:
        logging.error(f"Telegram Error: {e}")

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def main():
    # جلب الإعدادات من البيئة
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    # قائمة الأسهم الافتراضية
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "📡 *رادار الأسهم المطور (V2) متصل*\n⏱️ مزامنة الثواني: مفعلة\n💎 فلتر السيولة: مفعل")

    last_alert_time = {ticker: datetime.min for ticker in TICKERS}

    while True:
        try:
            # --- 1. المزامنة الزمنية الدقيقة (دخول أول 5 ثوانٍ) ---
            now_check = datetime.now()
            # الانتظار حتى بداية الدقيقة القادمة (ثانية 00)
            wait_seconds = 60 - now_check.second
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            now = datetime.now(timezone.utc)
            # جلب البيانات لآخر 45 دقيقة (كافية للحسابات)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=45), end=now, feed="iex"
            )).df

            for sym in TICKERS:
                if sym not in bars_df.index: continue
                
                df = bars_df.xs(sym).sort_index()
                if len(df) < 21: continue 

                # حساب RSI والحجم (Volume)
                df['rsi'] = calculate_rsi(df['close'])
                current_vol = df['volume'].iloc[-1]
                avg_vol = df['volume'].iloc[-11:-1].mean() # متوسط الـ 10 دقائق السابقة
                
                price_now = float(df["close"].iloc[-1])
                current_rsi = df['rsi'].iloc[-1]
                prev_rsi = df['rsi'].iloc[-2]
                ma_price = df["close"].iloc[-MA_WINDOW:-1].mean()

                alert_triggered = False
                msg = ""

                # فلتر السيولة: هل الفوليوم الحالي قوي؟
                high_volume = current_vol > (avg_vol * VOL_MULTIPLIER)
                vol_status = "✅ سيولة قوية" if high_volume else "⚠️ سيولة عادية"

                # 🚀 حالة الشراء (LONG)
                if price_now > ma_price and current_rsi < RSI_MAX_LONG and current_rsi > prev_rsi:
                    msg = (f"🚀 *فرصة شراء (LONG): {sym}*\n"
                           f"💰 السعر: {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f} ↗️\n"
                           f"💎 التقييم: {vol_status}\n"
                           f"⏱️ الوقت: {now.strftime('%H:%M:%S')} UTC")
                    alert_triggered = True

                # 📉 حالة البيع (SHORT)
                elif price_now < ma_price and current_rsi > RSI_MIN_SHORT and current_rsi < prev_rsi:
                    msg = (f"📉 *فرصة بيع (SHORT): {sym}*\n"
                           f"💰 السعر: {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f} ↘️\n"
                           f"💎 التقييم: {vol_status}\n"
                           f"⏱️ الوقت: {now.strftime('%H:%M:%S')} UTC")
                    alert_triggered = True

                # إرسال التنبيه (منع التكرار خلال 5 دقائق)
                if alert_triggered:
                    if (datetime.now() - last_alert_time[sym]).total_seconds() > 300: 
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
                        last_alert_time[sym] = datetime.now()
                        logging.info(f"Alert sent for {sym} | RSI: {current_rsi:.2f}")

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(5) # إعادة المحاولة سريعاً

if __name__ == "__main__":
    main()
