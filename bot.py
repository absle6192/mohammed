import os
import time
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta

# مكتبات Alpaca الجديدة للداول
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

# --- إعدادات القناص (تعديلك المباشر) ---
CASH_PER_TRADE = 30000     # دخول بـ 30 ألف دولار
TARGET_PROFIT = 10.0       # الخروج عند ربح 10 دولار
STOP_LOSS = -20.0          # وقف خسارة عند 20 دولار (لحماية السيولة)
MAX_DAILY_TRADES = 50      # هدفك: 50 صفقة يومياً
TICKERS = ["NVDA", "TSLA", "AAPL", "AMD"] # الأسهم المقترحة لسيولة عالية

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class SniperBot:
    def __init__(self):
        self.api_key = os.getenv("APCA_API_KEY_ID")
        self.secret_key = os.getenv("APCA_API_SECRET_KEY")
        
        # عملاء Alpaca (البيانات والتداول)
        self.trading_client = TradingClient(self.api_key, self.secret_key, paper=True) # اجعله False للحقيقي
        self.data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
        
        self.trades_count = 0

    def get_signal(self, sym):
        # نفس منطق كودك "الممتاز" للتحليل
        now = datetime.now(timezone.utc)
        bars = self.data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
            start=now - timedelta(minutes=60), end=now, feed="iex"
        )).df
        
        df = bars.xs(sym).sort_index()
        if len(df) < 20: return None
        
        # حساب المؤشرات
        ma_price = df["close"].iloc[-20:-1].mean()
        price_now = float(df["close"].iloc[-1])
        
        if price_now > ma_price: return "LONG"
        if price_now < ma_price: return "SHORT"
        return None

    def execute_and_monitor(self, sym, side_str):
        # 1. حساب الكمية بناءً على 30 ألف دولار
        quote = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed="iex"))
        current_price = (quote[sym].bid_price + quote[sym].ask_price) / 2
        qty = int(CASH_PER_TRADE / current_price)

        # 2. دخول ماركت فوري
        side = OrderSide.BUY if side_str == "LONG" else OrderSide.SELL
        print(f"🚀 تنفيذ صفقة {side_str} لـ {sym} | الكمية: {qty} سهم")
        
        order_data = MarketOrderRequest(symbol=sym, qty=qty, side=side, time_in_force=TimeInForce.GTC)
        self.trading_client.submit_order(order_data)
        
        entry_price = current_price
        
        # 3. حلقة مراقبة الربح (EXIT STRATEGY)
        while True:
            time.sleep(0.5) # فحص فائق السرعة
            q_now = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed="iex"))
            price_now = (q_now[sym].bid_price + q_now[sym].ask_price) / 2
            
            # حساب الربح/الخسارة بالدولار
            if side_str == "LONG":
                pnl = (price_now - entry_price) * qty
            else:
                pnl = (entry_price - price_now) * qty

            # شروط الخروج الفوري
            if pnl >= TARGET_PROFIT or pnl <= STOP_LOSS:
                exit_side = OrderSide.SELL if side_str == "LONG" else OrderSide.BUY
                exit_order = MarketOrderRequest(symbol=sym, qty=qty, side=exit_side, time_in_force=TimeInForce.GTC)
                self.trading_client.submit_order(exit_order)
                print(f"💰 تم الخروج! الربح/الخسارة: {pnl:.2f}$")
                break

    def run(self):
        print("🎯 البوت بدأ العمل لتحقيق 50 صفقة...")
        while self.trades_count < MAX_DAILY_TRADES:
            # شرط الوقت (قبل الإغلاق بـ 30 دقيقة)
            now_est = datetime.now(timezone(timedelta(hours=-5))) # توقيت نيويورك تقريبي
            if now_est.hour == 15 and now_est.minute >= 30:
                print("🛑 اقترب إغلاق السوق، توقف القناص.")
                break

            for sym in TICKERS:
                signal = self.get_signal(sym)
                if signal:
                    self.execute_and_monitor(sym, signal)
                    self.trades_count += 1
                    print(f"📊 إجمالي الصفقات اليوم: {self.trades_count}/50")
                    
                    if self.trades_count >= MAX_DAILY_TRADES: break
                
            time.sleep(10) # انتظار فرصة جديدة

if __name__ == "__main__":
    bot = SniperBot()
    bot.run()
