import os
import time
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

# --- الإعدادات الصارمة للقنص ---
CASH_PER_TRADE = 30000     # السيولة لكل صفقة
TARGET_PROFIT = 10.0       # الهدف: 10$ ربح صافي
STOP_LOSS = -20.0          # الحماية: -20$ وقف خسارة
MAX_DAILY_TRADES = 50      # الحد اليومي: 50 صفقة
MAX_SPREAD = 0.02          # أقصى فارق سعري مسموح به للدخول
TICKERS = ["NVDA", "TSLA", "AMD", "AAPL", "MSFT"] # أسهم السيولة الضخمة

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

class PrecisionSniper:
    def __init__(self):
        self.api_key = os.getenv("APCA_API_KEY_ID")
        self.secret_key = os.getenv("APCA_API_SECRET_KEY")
        
        # التداول والبيانات
        self.trading_client = TradingClient(self.api_key, self.secret_key, paper=True) 
        self.data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
        self.trades_done = 0

    def get_precision_signal(self, sym):
        try:
            now = datetime.now(timezone.utc)
            # جلب آخر 30 دقيقة من البيانات
            bars = self.data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=30), end=now, feed="iex"
            )).df
            df = bars.xs(sym).sort_index()
            
            # 1. جلب السبريد اللحظي (مهم جداً للـ 30 ألف)
            quote = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed="iex"))
            bid = quote[sym].bid_price
            ask = quote[sym].ask_price
            spread = ask - bid
            current_price = (bid + ask) / 2

            # 2. حساب المؤشرات
            ma_20 = df["close"].rolling(window=20).mean().iloc[-1]
            rsi = calculate_rsi(df["close"]).iloc[-1]
            avg_vol = df["volume"].mean()
            last_vol = df["volume"].iloc[-1]

            # --- فلاتر الجودة ---
            if spread > MAX_SPREAD: return None # السبريد عالي (تجنب الدخول)
            if last_vol < avg_vol: return None   # السيولة ضعيفة حالياً

            # إشارة شراء (Long)
            if current_price > ma_20 and 40 < rsi < 65:
                if current_price > df["close"].iloc[-1]: # تأكيد زخم صاعد
                    return "LONG", current_price
            
            # إشارة بيع (Short)
            if current_price < ma_20 and 35 < rsi < 60:
                if current_price < df["close"].iloc[-1]: # تأكيد زخم هابط
                    return "SHORT", current_price

            return None, None
        except Exception as e:
            logging.error(f"Signal Error for {sym}: {e}")
            return None, None

    def fast_exit_monitor(self, sym, entry_price, qty, side_str):
        print(f"👀 مراقبة الربح لـ {sym}...")
        while True:
            try:
                q = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed="iex"))
                p_now = (q[sym].bid_price + q[sym].ask_price) / 2
                
                pnl = (p_now - entry_price) * qty if side_str == "LONG" else (entry_price - p_now) * qty

                if pnl >= TARGET_PROFIT or pnl <= STOP_LOSS:
                    side = OrderSide.SELL if side_str == "LONG" else OrderSide.BUY
                    self.trading_client.submit_order(MarketOrderRequest(
                        symbol=sym, qty=qty, side=side, time_in_force=TimeInForce.GTC
                    ))
                    logging.info(f"💰 خروج فوري ماركت | الربح/الخسارة: {pnl:.2f}$")
                    break
                time.sleep(0.5) # مراقبة كل نصف ثانية لسرعة الاستجابة
            except:
                continue

    def start(self):
        logging.info("🚀 تم تفعيل القناص بـ 30 ألف دولار - الهدف 50 صفقة")
        while self.trades_done < MAX_DAILY_TRADES:
            # توقيت العمل (يتوقف قبل الإغلاق بـ 30 دقيقة)
            now = datetime.now()
            if now.hour == 15 and now.minute >= 30: break

            for sym in TICKERS:
                signal, price = self.get_precision_signal(sym)
                if signal:
                    qty = int(CASH_PER_TRADE / price)
                    side = OrderSide.BUY if signal == "LONG" else OrderSide.SELL
                    
                    # دخول ماركت فوري
                    self.trading_client.submit_order(MarketOrderRequest(
                        symbol=sym, qty=qty, side=side, time_in_force=TimeInForce.GTC
                    ))
                    logging.info(f"✅ دخلنا {signal} في {sym} بـ {qty} سهم")
                    
                    # الانتقال للمراقبة والإغلاق
                    self.fast_exit_monitor(sym, price, qty, signal)
                    self.trades_done += 1
                    
                    if self.trades_done >= MAX_DAILY_TRADES: break
            
            time.sleep(1) # استراحة ثانية قبل البحث عن الفرصة التالية

if __name__ == "__main__":
    bot = PrecisionSniper()
    bot.start()
