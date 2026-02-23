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

RSI_MAX_LONG = 68
RSI_MIN_SHORT = 35
MA_WINDOW = 20

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

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "📡 *رادار السوق يعمل الآن*\nتنبيهات Long/Short (نسخة خفيفة + فلتر لون الشمعة).")

    last_alert_time = {ticker: datetime.min.replace(tzinfo=timezone.utc) for ticker in TICKERS}

    while True:
        try:
            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=TICKERS,
                    timeframe=TimeFrame.Minute,
                    start=now - timedelta(minutes=60),
                    end=now,
                    feed="iex"
                )
            ).df

            if bars_df is None or len(bars_df) == 0:
                time.sleep(60)
                continue

            available_syms = set(bars_df.index.get_level_values(0).unique().tolist())

            for sym in TICKERS:
                if sym not in available_syms:
                    continue

                df = bars_df.xs(sym).sort_index()
                if len(df) < (MA_WINDOW + 5):
                    continue

                df["rsi"] = calculate_rsi(df["close"])

                # آخر شمعة مكتملة
                last_candle = df.iloc[-2]
                price_now = float(last_candle["close"])
                current_rsi = float(df["rsi"].iloc[-2])

                ma_price = float(df["close"].iloc[-(MA_WINDOW + 2):-2].mean())

                # ✅ فلتر بسيط: لون الشمعة الأخيرة المكتملة
                last_green = float(last_candle["close"]) > float(last_candle["open"])
                last_red = float(last_candle["close"]) < float(last_candle["open"])

                alert_triggered = False
                msg = ""

                # LONG
                if price_now > ma_price and current_rsi < RSI_MAX_LONG and last_green:
                    msg = (f"🚀 *فرصة LONG (شراء): {sym}*\n"
                           f"💰 السعر (إغلاق آخر شمعة): {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f}\n"
                           f"📈 فوق المتوسط + شمعة خضراء\n"
                           f"⏳ *صلاحية: 30 ثانية*")
                    alert_triggered = True

                # SHORT
                elif price_now < ma_price and current_rsi > RSI_MIN_SHORT and last_red:
                    msg = (f"📉 *فرصة SHORT (بيع): {sym}*\n"
                           f"💰 السعر (إغلاق آخر شمعة): {price_now:.2f}\n"
                           f"📊 RSI: {current_rsi:.2f}\n"
                           f"📉 تحت المتوسط + شمعة حمراء\n"
                           f"⏳ *صلاحية: 30 ثانية*")
                    alert_triggered = True

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
