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
RSI_MAX_LONG = 68   # للدخول شراء (تجنب التضخم)
RSI_MIN_SHORT = 35  # للدخول شورت (تجنب القاع السحيق)
MA_WINDOW = 20      # متوسط 20 دقيقة

# ✅ فلتر جديد بسيط: لازم السعر يبتعد عن المتوسط بنسبة بسيطة (يقلل الانعكاس)
MIN_DISTANCE_FROM_MA_PCT = 0.0005   # 0.05% (جرّبها، وإذا قلّت الإشارات كثير نخليها 0.0003)

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram Error: {e}")

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN").split(",")]

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "📡 *رادار السوق يعمل الآن*\nسأرسل تنبيهات لفرص الـ Long والـ Short.")

    # سجل التنبيهات لمنع التكرار المزعج (15 دقيقة لكل سهم)
    last_alert_time = {ticker: datetime.min.replace(tzinfo=timezone.utc) for ticker in TICKERS}

    while True:
        try:
            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                    start=now - timedelta(minutes=60), end=now, feed="iex"
                )
            ).df

            if bars_df is None or len(bars_df) == 0:
                time.sleep(60)
                continue

            # ✅ إصلاح فحص وجود السهم لأن df MultiIndex
            available_syms = set(bars_df.index.get_level_values(0).unique().tolist())

            for sym in TICKERS:
                if sym not in available_syms:
                    continue

                df = bars_df.xs(sym).sort_index()
                if len(df) < (MA_WINDOW + 5):
                    continue

                df['rsi'] = calculate_rsi(df['close'])

                # ✅ نعتمد على آخر شمعة مكتملة بدل الشمعة الحالية
                price_now = float(df["close"].iloc[-2])
                current_rsi = float(df['rsi'].iloc[-2])

                # ✅ نحسب MA بدون الشمعة الحالية
                ma_price = float(df["close"].iloc[-(MA_WINDOW + 2):-2].mean())

                # ✅ فلتر: ابتعاد السعر عن المتوسط (يقلل إشارات التذبذب والانعكاس)
                distance_pct = abs(price_now - ma_price) / ma_price if ma_price > 0 else 0.0

                alert_triggered = False
                msg = ""

                # 1. شرط الصعود (Long)
                if price_now > ma_price and current_rsi < RSI_MAX_LONG and distance_pct > MIN_DISTANCE_FROM_MA_PCT:
                    msg = (f"🚀 *فرصة LONG (شراء): {sym}*\n"
                           f"💰 السعر (إغلاق آخر شمعة): {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f}\n"
                           f"📈 الاتجاه: فوق المتوسط (صاعد)\n"
                           f"📏 بُعد عن المتوسط: {distance_pct*100:.3f}%")
                    alert_triggered = True

                # 2. شرط الهبوط (Short)
                elif price_now < ma_price and current_rsi > RSI_MIN_SHORT and distance_pct > MIN_DISTANCE_FROM_MA_PCT:
                    msg = (f"📉 *فرصة SHORT (بيع): {sym}*\n"
                           f"💰 السعر (إغلاق آخر شمعة): {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f}\n"
                           f"📉 الاتجاه: تحت المتوسط (هابط)\n"
                           f"📏 بُعد عن المتوسط: {distance_pct*100:.3f}%")
                    alert_triggered = True

                # إرسال التنبيه إذا تحقق الشرط ولم يتم الإرسال مؤخراً
                if alert_triggered:
                    if (now - last_alert_time[sym]).total_seconds() > 900:
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
                        last_alert_time[sym] = now
                        logging.info(f"Alert sent for {sym}")

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(30)

        time.sleep(60)

if __name__ == "__main__":
    main()
