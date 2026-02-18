import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

# إعداد السجلات بشكل احترافي
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ===================== الإعدادات الثابتة =====================
TRADE_AMOUNT = 20000.0
MAX_POSITIONS = 2
FLEXIBLE_TARGET = 250.0
DAILY_PROFIT_TARGET = 300.0
MAX_SPREAD_PCT = 0.002 

# ===================== وظائف المساعدة =====================
def get_spread(data_client, symbol):
    try:
        resp = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
        q = resp[symbol]
        if q.ask_price <= 0 or q.bid_price <= 0: return 1.0
        return (q.ask_price - q.bid_price) / q.ask_price
    except Exception as e:
        logging.error(f"Spread Check Error for {symbol}: {e}")
        return 1.0

def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except:
        pass

# ===================== المحرك الرئيسي =====================
def main():
    # التحقق من وجود المفاتيح
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    if not API_KEY or not SECRET_KEY:
        logging.error("❌ مفاتيح Alpaca مفقودة في إعدادات Render!")
        return

    IS_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA").split(",")]

    # إنشاء العملاء
    trader = TradingClient(API_KEY, SECRET_KEY, paper=IS_PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    auto_mode = True
    logging.info(f"🚀 Bot Started | Amount: ${TRADE_AMOUNT} | Auto: {auto_mode}")
    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "✅ تم تشغيل البوت بنجاح (نسخة الحماية القصوى)")

    while True:
        try:
            # 1. فحص وقت السوق
            clock = trader.get_clock()
            if not clock.is_open:
                logging.info("😴 السوق مغلق الآن...")
                time.sleep(300)
                continue

            # 2. فحص الربح/الخسارة اليومي
            account = trader.get_account()
            current_pnl = float(account.equity) - float(account.last_equity)

            if auto_mode and current_pnl >= FLEXIBLE_TARGET:
                auto_mode = False
                send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"💰 تم تحقيق ربح ممتاز (${current_pnl:.2f}). وضع المنبه مفعل الآن 🔔")

            # 3. جلب بيانات الشموع
            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=30), end=now, feed="iex"
            )).df

            if bars_df is None or bars_df.empty:
                time.sleep(15)
                continue

            for sym in TICKERS:
                try:
                    if sym not in bars_df.index: continue
                    df = bars_df.xs(sym).sort_index()
                    if len(df) < 10: continue

                    # تنظيف البيانات من القيم الفارغة
                    df = df.ffill() 

                    price_now = float(df["close"].iloc[-2])
                    vol_now = float(df["volume"].iloc[-2])
                    vol_avg = df["volume"].iloc[-7:-2].mean()
                    
                    # حساب الفرق عن المتوسط (SMA 5)
                    ma_price = df["close"].iloc[-7:-2].mean()
                    price_diff = (price_now - ma_price) / ma_price
                    vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0

                    # الشروط الصارمة (0.06% + سيولة x1.2)
                    if abs(price_diff) >= 0.0006 and vol_ratio >= 1.2:
                        side = "LONG" if price_diff > 0 else "SHORT"
                        
                        # فلتر السبريد
                        if get_spread(data_client, sym) > MAX_SPREAD_PCT:
                            logging.warning(f"⚠️ {sym} سبريد عالي جداً - تم التخطى")
                            continue

                        # فحص المراكز المفتوحة
                        positions = trader.get_all_positions()
                        
                        if auto_mode and len(positions) < MAX_POSITIONS:
                            if any(p.symbol == sym for p in positions): continue
                            
                            qty = int(TRADE_AMOUNT / price_now)
                            if qty <= 0: continue

                            # تحديد مستويات الربح والخسارة
                            tp = round(price_now * 1.008, 2) if side == "LONG" else round(price_now * 0.992, 2)
                            sl = round(price_now * 0.996, 2) if side == "LONG" else round(price_now * 1.004, 2)

                            # تنفيذ الأمر
                            trader.submit_order(MarketOrderRequest(
                                symbol=sym, qty=qty, 
                                side=OrderSide.BUY if side == "LONG" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                                take_profit={'limit_price': tp}, stop_loss={'stop_price': sl}
                            ))
                            send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🚀 تم دخول صفقة آلياً: {sym} | الجانب: {side}")
                        else:
                            # وضع المنبه فقط
                            msg = f"🔔 فرصة دخول {side} على {sym}\nالسعر الحالي: {price_now}"
                            send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)

                except Exception as e:
                    logging.error(f"Error processing {sym}: {e}")

        except Exception as e:
            logging.error(f"General Loop Error: {e}")
            time.sleep(30)

        time.sleep(20)

if __name__ == "__main__":
    main()
