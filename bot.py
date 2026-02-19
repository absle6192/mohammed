import os
import time
import requests
import logging
import pandas_ta as ta  # تأكد من تثبيت مكتبة pandas_ta
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# إعداد السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ===================== إعدادات بوت القنوع المطور (RSI + سهمين) =====================
TRADE_AMOUNT = 15000.0       
MAX_POSITIONS = 2            # حد أقصى سهمين فقط
STOP_LOSS_PCT = 0.012        # وقف خسارة 1.2% (مساحة أمان جيدة)
TAKE_PROFIT_PCT = 0.018      # هدف ربح 1.8% (واقعي وقنوع)

# فلاتر الدخول الذكية
RSI_PERIOD = 14
RSI_MAX = 68                 # لا يشتري إذا كان السهم متضخماً (أعلى من 68)
MIN_VOL_RATIO = 1.5          # سيولة قوية

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

    logging.info("🛡️ تشغيل البوت المطور (RSI + Limit + 2 Positions)")
    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "🛡️ تم التحديث: إضافة فلتر RSI لمنع الشراء عند القمم + الالتزام بسهمين فقط.")

    while True:
        try:
            clock = trader.get_clock()
            if not clock.is_open:
                time.sleep(60)
                continue

            # فحص الصفقات والأوامر المعلقة
            positions = trader.get_all_positions()
            orders_request = GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY)
            pending_buy_orders = trader.get_orders(filter=orders_request)

            if len(positions) + len(pending_buy_orders) >= MAX_POSITIONS:
                time.sleep(30)
                continue

            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=60), end=now, feed="iex"
            )).df

            if bars_df is None or bars_df.empty:
                time.sleep(15)
                continue

            for sym in TICKERS:
                if any(p.symbol == sym for p in positions): continue
                if sym not in bars_df.index: continue
                
                df = bars_df.xs(sym).sort_index().ffill()
                if len(df) < 20: continue

                # --- حساب المؤشرات الفنية ---
                # حساب RSI باستخدام pandas_ta
                df['RSI'] = ta.rsi(df['close'], length=RSI_PERIOD)
                current_rsi = df['RSI'].iloc[-1]
                
                price_now = float(df["close"].iloc[-1])
                ma_price = df["close"].iloc[-15:-1].mean()
                vol_now = float(df["volume"].iloc[-1])
                vol_avg = df["volume"].iloc[-15:-1].mean()

                # شرط الدخول المطور:
                # 1. السعر فوق المتوسط (اتجاه صاعد)
                # 2. RSI تحت الـ 68 (ليس متضخماً)
                # 3. وجود سيولة قوية
                if price_now > ma_price and current_rsi < RSI_MAX and (vol_now / vol_avg) >= MIN_VOL_RATIO:
                    qty = int(TRADE_AMOUNT / price_now)
                    if qty <= 0: continue

                    limit_entry = round(price_now, 2)
                    tp_price = round(limit_entry * (1 + TAKE_PROFIT_PCT), 2)
                    sl_price = round(limit_entry * (1 - STOP_LOSS_PCT), 2)

                    trader.submit_order(LimitOrderRequest(
                        symbol=sym, qty=qty, side=OrderSide.BUY,
                        limit_price=limit_entry,
                        time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                        take_profit={'limit_price': tp_price}, 
                        stop_loss={'stop_price': sl_price}
                    ))
                    
                    msg = f"🎯 قنص ذكي (RSI): {sym}\nRSI: {current_rsi:.2f}\nالسعر: {limit_entry}\nالهدف: {tp_price}"
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)
                    break 

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(20)
        time.sleep(20)

if __name__ == "__main__":
    main()
