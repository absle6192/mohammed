import os
import time
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta

# استيراد مكتبات Alpaca (تأكد من تثبيت alpaca-trade-api و alpaca-py)
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

# --- إعدادات القناص الصارمة ---
CASH_PER_TRADE = 30000     # السيولة لكل صفقة
TARGET_PROFIT = 10.0       # الهدف: 10$ ربح صافي
STOP_LOSS = -20.0          # الحماية: -20$ وقف خسارة
MAX_DAILY_TRADES = 50      # الحد اليومي: 50 صفقة
MAX_SPREAD = 0.02          # أقصى فارق سعري مسموح به (سنتين)
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
        # جلب المفاتيح من متغيرات البيئة (Render)
        self.api_key = os.getenv("APCA_API_KEY_ID")
        self.secret_key = os.getenv("APCA_API_SECRET_KEY")
        
        # التداول والبيانات (اجعل paper=False للحساب الحقيقي)
        self.trading_client = TradingClient(self.api_key, self.secret_key, paper=True) 
        self.data_client = StockHistoricalDataClient(self.api_key, self.secret_key)
        self.trades_done = 0

    def get_precision_signal(self, sym):
        try:
            now = datetime.now(timezone.utc)
            bars = self.data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=30), end=now, feed="iex"
            )).df
            
            if bars.empty or sym not in bars.index:
                return None, None
                
            df = bars.xs(sym).sort_index()
            
            # جلب السعر والـ Spread
            quote = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed="iex"))
            bid = quote[sym].bid_price
            ask = quote[sym].ask_price
            
            if bid is None or ask is None:
                return None, None
                
            spread = ask - bid
            current_price = (bid + ask) / 2

            # حساب المؤشرات
            ma_20 = df["close"].rolling(window=20).mean().iloc[-1]
            rsi = calculate_rsi(df["close"]).iloc[-1]
            avg_vol = df["volume"].mean()
            last_vol = df["volume"].iloc[-1]

            # فلاتر الجودة والدقة
            if spread > MAX_SPREAD: return None, None
            if last_vol < avg_vol: return None, None

            # منطق الدخول (شراء/بيع)
            if current_price > ma_20 and 40 < rsi < 65:
                if current_price > df["close"].iloc[-1]: # تأكيد الاتجاه
                    return "LONG", current_price
            
            if current_price < ma_20 and 35 < rsi < 60:
                if current_price < df["close"].iloc[-1]: # تأكيد الاتجاه
                    return "SHORT", current_price

            return None, None
        except Exception:
            return None, None

    def fast_exit_monitor(self, sym, entry_price, qty, side_str):
        logging.info(f"👀 بدأت مراقبة الربح لـ {sym}")
        while True:
            try:
                q = self.data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym, feed="iex"))
                p_now = (q[sym].bid_price + q[sym].ask_price) / 2
                
                # حساب الربح/الخسارة لحظياً
                if side_str == "LONG":
                    pnl = (p_now - entry_price) * qty
                else:
                    pnl = (entry_price - p_now) * qty

                # تنفيذ الخروج الفوري
                if pnl >= TARGET_PROFIT or pnl <= STOP_LOSS:
                    side = OrderSide.SELL if side_str == "LONG" else OrderSide.BUY
                    self.trading_client.submit_order(MarketOrderRequest(
                        symbol=sym, qty=qty, side=side, time_in_force=TimeInForce.GTC
                    ))
                    logging.info(f"💰 تم إغلاق الصفقة بنجاح | الربح/الخسارة: {pnl:.2f}$")
                    break
                time.sleep(0.5) 
            except Exception as e:
                logging.error(f"خطأ في مراقبة الخروج: {e}")
                break

    def start(self):
        logging.info(f"🚀 القناص يعمل بـ 30 ألف دولار | الهدف اليومي: {MAX_DAILY_TRADES}")
        
        while self.trades_done < MAX_DAILY_TRADES:
            # توقيت إغلاق السوق (3:30 مساءً بتوقيت نيويورك)
            now_utc = datetime.now(timezone.utc)
            # حسب توقيت Render، تأكد من ضبط منطق الوقت ليناسب السوق
            
            for sym in TICKERS:
                signal, price = self.get_precision_signal(sym)
                
                # صمام الأمان الذي حل مشكلة Render
                if signal is None or price is None:
                    continue

                if signal:
                    qty = int(CASH_PER_TRADE / price)
                    side = OrderSide.BUY if signal == "LONG" else OrderSide.SELL
                    
                    try:
                        self.trading_client.submit_order(MarketOrderRequest(
                            symbol=sym, qty=qty, side=side, time_in_force=TimeInForce.GTC
                        ))
                        logging.info(f"✅ تم فتح صفقة {signal} في {sym}")
                        
                        # الدخول في وضع المراقبة حتى الإغلاق
                        self.fast_exit_monitor(sym, price, qty, signal)
                        self.trades_done += 1
                        logging.info(f"📊 صفقات اليوم: {self.trades_done}/{MAX_DAILY_TRADES}")
                        
                    except Exception as e:
                        logging.error(f"❌ تعذر فتح صفقة {sym}: {e}")
                        continue
                    
                    if self.trades_done >= MAX_DAILY_TRADES: break
            
            time.sleep(1)

if __name__ == "__main__":
    bot = PrecisionSniper()
    bot.start()
