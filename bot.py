import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات القنوع (سهمين كحد أقصى) ---
TRADE_AMOUNT = 15000.0       
MAX_POSITIONS = 2            
STOP_LOSS_PCT = 0.012        
TAKE_PROFIT_PCT = 0.018      
RSI_MAX = 68                 

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
    except: pass

# دالة حساب RSI يدوياً لتجنب مشاكل المكتبات
def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    IS_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]

    trader = TradingClient(API_KEY, SECRET_KEY, paper=IS_PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "🚀 تم تشغيل البوت بنظام RSI اليدوي (سهمين بحد أقصى).")

    while True:
        try:
            clock = trader.get_clock()
            if not clock.is_open:
                time.sleep(60)
                continue

            positions = trader.get_all_positions()
            orders_request = GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY)
            pending_orders = trader.get_orders(filter=orders_request)

            if len(positions) + len(pending_orders) >= MAX_POSITIONS:
                time.sleep(30)
                continue

            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=60), end=now, feed="iex"
            )).df

            for sym in TICKERS:
                if any(p.symbol == sym for p in positions): continue
                if sym not in bars_df.index: continue
                
                df = bars_df.xs(sym).sort_index()
                if len(df) < 20: continue

                # حساب RSI والسعر
                df['rsi'] = calculate_rsi(df['close'])
                current_rsi = df['rsi'].iloc[-1]
                price_now = float(df["close"].iloc[-1])
                ma_price = df["close"].iloc[-20:-1].mean()

                # شرط القناص: اتجاه صاعد + RSI غير متضخم
                if price_now > ma_price and current_rsi < RSI_MAX:
                    qty = int(TRADE_AMOUNT / price_now)
                    limit_entry = round(price_now, 2)
                    tp = round(limit_entry * (1 + TAKE_PROFIT_PCT), 2)
                    sl = round(limit_entry * (1 - STOP_LOSS_PCT), 2)

                    trader.submit_order(LimitOrderRequest(
                        symbol=sym, qty=qty, side=OrderSide.BUY, limit_price=limit_entry,
                        time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                        take_profit={'limit_price': tp}, stop_loss={'stop_price': sl}
                    ))
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🎯 دخول ذكي: {sym}\nالسعر: {limit_entry}\nRSI: {current_rsi:.2f}")
                    break 

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(30)
        time.sleep(20)

if __name__ == "__main__":
    main()
