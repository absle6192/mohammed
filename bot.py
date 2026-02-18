import os
import time
import requests
import logging
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

# إعداد السجلات
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ===================== إعدادات القناص الجديدة =====================
TRADE_AMOUNT = 20000.0
MAX_POSITIONS = 2
FLEXIBLE_TARGET = 250.0      # يبدأ بالتحول لمنبه عند هذا الربح
STOP_LOSS_USD = 40.0         # وقف خسارة ثابت بالدولار لكل صفقة
TAKE_PROFIT_USD = 80.0       # هدف ربح ثابت بالدولار لكل صفقة

# فلاتر الدخول القاسية (الصياد المحترف)
MIN_PRICE_DIFF = 0.001       # 0.1% اختراق سعري
MIN_VOL_RATIO = 2.0          # ضعف متوسط السيولة السابقة

# ===================== وظائف المساعدة =====================
def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except: pass

# ===================== المحرك الرئيسي =====================
def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    IS_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META").split(",")]

    trader = TradingClient(API_KEY, SECRET_KEY, paper=IS_PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    auto_mode = True
    logging.info("🎯 Sniper Bot Started | Quality over Quantity Mode")
    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "🎯 تم تشغيل (بوت القناص): شروط قاسية + أهداف (80$ ربح / 40$ خسارة)")

    while True:
        try:
            # 1. فحص حالة السوق والربح اليومي
            clock = trader.get_clock()
            if not clock.is_open:
                time.sleep(60)
                continue

            account = trader.get_account()
            current_pnl = float(account.equity) - float(account.last_equity)

            if auto_mode and current_pnl >= FLEXIBLE_TARGET:
                auto_mode = False
                send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"💰 تم تحقيق الهدف المرن (${current_pnl:.2f}). الوضع: منبه فقط 🔔")

            # 2. جلب البيانات وتحليلها
            now = datetime.now(timezone.utc)
            bars_df = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=30), end=now, feed="iex"
            )).df

            if bars_df is None or bars_df.empty:
                time.sleep(15)
                continue

            for sym in TICKERS:
                if sym not in bars_df.index: continue
                df = bars_df.xs(sym).sort_index().ffill()
                if len(df) < 10: continue

                # حساب المؤشرات
                price_now = float(df["close"].iloc[-2])
                ma_price = df["close"].iloc[-7:-2].mean()
                price_diff = (price_now - ma_price) / ma_price
                
                vol_now = float(df["volume"].iloc[-2])
                vol_avg = df["volume"].iloc[-7:-2].mean()
                vol_ratio = vol_now / vol_avg if vol_avg > 0 else 0

                # تطبيق فلاتر القناص
                if abs(price_diff) >= MIN_PRICE_DIFF and vol_ratio >= MIN_VOL_RATIO:
                    side = "LONG" if price_diff > 0 else "SHORT"
                    
                    positions = trader.get_all_positions()
                    if auto_mode and len(positions) < MAX_POSITIONS:
                        if any(p.symbol == sym for p in positions): continue
                        
                        qty = int(TRADE_AMOUNT / price_now)
                        if qty <= 0: continue

                        # حساب الـ TP و الـ SL بالدولار بناءً على عدد الأسهم
                        # الهدف 80 دولار والوقوف 40 دولار
                        move_for_tp = TAKE_PROFIT_USD / qty
                        move_for_sl = STOP_LOSS_USD / qty

                        if side == "LONG":
                            tp_price = round(price_now + move_for_tp, 2)
                            sl_price = round(price_now - move_for_sl, 2)
                            order_side = OrderSide.BUY
                        else:
                            tp_price = round(price_now - move_for_tp, 2)
                            sl_price = round(price_now + move_for_sl, 2)
                            order_side = OrderSide.SELL

                        trader.submit_order(MarketOrderRequest(
                            symbol=sym, qty=qty, side=order_side,
                            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                            take_profit={'limit_price': tp_price}, stop_loss={'stop_price': sl_price}
                        ))
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🎯 دخول قناص (آلي): {sym}\nالهدف: +${TAKE_PROFIT_USD}\nالحماية: -${STOP_LOSS_USD}")
                    else:
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🔔 تنبيه قناص (يدوي): {sym} محقق شروط الانفجار السعري!")

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(30)

        time.sleep(20)

if __name__ == "__main__":
    main()
