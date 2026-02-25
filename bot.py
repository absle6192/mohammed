import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# مكتبات الأسهم والعملات
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات الحساسية (A-Grade) ---
RSI_MAX_LONG = 62
MA_WINDOW = 20
MIN_TREND_PCT = 0.20

# --- أهداف الكريبتو للبيع التلقائي (محرك الإغلاق الجديد) ---
CRYPTO_TP_PCT = 1.5  # يبيع عند ربح 1.5%
CRYPTO_SL_PCT = 1.0  # يبيع عند خسارة 1.0%

# --- إعدادات تداول الأسهم ---
OPEN_NOTIONAL_USD = 30000
OPEN_TRADE_COUNT = 3
TAKE_PROFIT_PCT = 0.30
STOP_LOSS_PCT   = 0.20
MAX_HOLD_MINUTES = 15
NY_TZ = ZoneInfo("America/New_York")

# --- العملات الرقمية ---
CRYPTO_TICKERS = ["BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "LTC/USD", "DOGE/USD"]
CRYPTO_ORDER_AMOUNT = 5000


def send_tg_msg(token, chat_id, text):
    if not token or not chat_id: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: logging.error(f"Telegram Error: {e}")


def calculate_rsi(series: pd.Series, window=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def is_market_open_window(now_utc: datetime) -> bool:
    now_ny = now_utc.astimezone(NY_TZ)
    if now_ny.weekday() >= 5: return False
    start = now_ny.replace(hour=9, minute=30, second=5, microsecond=0)
    end   = now_ny.replace(hour=9, minute=31, second=30, microsecond=0)
    return start <= now_ny <= end


def place_bracket_order(trading_client, symbol, notional, side, last_price):
    """خاص بالأسهم فقط"""
    if side == "LONG":
        order_side = OrderSide.BUY
        tp_price = round(last_price * (1.0 + TAKE_PROFIT_PCT / 100.0), 2)
        sl_price = round(last_price * (1.0 - STOP_LOSS_PCT / 100.0), 2)
    else:
        order_side = OrderSide.SELL
        tp_price = round(last_price * (1.0 - TAKE_PROFIT_PCT / 100.0), 2)
        sl_price = round(last_price * (1.0 + STOP_LOSS_PCT / 100.0), 2)
    order = MarketOrderRequest(
        symbol=symbol, notional=notional, side=order_side,
        time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
        take_profit={"limit_price": tp_price}, stop_loss={"stop_price": sl_price}
    )
    return trading_client.submit_order(order)


# --- محرك مراقبة وإغلاق صفقات الكريبتو (لحل مشكلة ثبات الشاشة وبيع الصفقات) ---
def monitor_and_close_crypto(trading_client, TG_TOKEN, TG_CHAT_ID):
    try:
        positions = trading_client.get_all_positions()
        for p in positions:
            # التحقق إذا كانت الصفقة عملة رقمية
            if p.asset_class == 'crypto':
                # الربح/الخسارة الحالية بالنسبة المئوية
                unrealized_pl_pct = float(p.unrealized_intraday_plpc) * 100
                
                if unrealized_pl_pct >= CRYPTO_TP_PCT or unrealized_pl_pct <= -CRYPTO_SL_PCT:
                    trading_client.close_position(p.symbol)
                    status = "✅ ربح" if unrealized_pl_pct > 0 else "🛑 خسارة"
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"💰 *بيع تلقائي للكريبتو*\nالعملة: {p.symbol}\nالحالة: {status}\nالنسبة: {unrealized_pl_pct:.2f}%")
    except Exception as e:
        logging.error(f"Monitoring Error: {e}")


def run_crypto_engine(crypto_client, trading_client, TG_TOKEN, TG_CHAT_ID, last_alerts):
    now = datetime.now(timezone.utc)
    try:
        # أولاً: تشغيل محرك البيع التلقائي
        monitor_and_close_crypto(trading_client, TG_TOKEN, TG_CHAT_ID)
        
        # ثانياً: البحث عن فرص شراء
        request_params = CryptoBarsRequest(symbol_or_symbols=CRYPTO_TICKERS, timeframe=TimeFrame.Minute, start=now - timedelta(minutes=60))
        bars = crypto_client.get_crypto_bars(request_params).df
        for sym in CRYPTO_TICKERS:
            if sym not in bars.index: continue
            df = bars.xs(sym).sort_index()
            df["rsi"] = calculate_rsi(df["close"])
            price_now = float(df["close"].iloc[-1])
            rsi_now = float(df["rsi"].iloc[-1])
            ma_price = float(df["close"].iloc[-MA_WINDOW:-1].mean())
            
            if price_now > ma_price and rsi_now < RSI_MAX_LONG:
                if (datetime.now() - last_alerts.get(sym, datetime.min)).total_seconds() > 1800:
                    # التأكد من عدم تكرار الشراء لنفس العملة
                    existing_pos = [pos.symbol for pos in trading_client.get_all_positions()]
                    if sym.replace("/", "") not in existing_pos:
                        # شراء أمر بسيط (بدون Bracket) لتجاوز خطأ المنصة
                        order = MarketOrderRequest(
                            symbol=sym, notional=CRYPTO_ORDER_AMOUNT, 
                            side=OrderSide.BUY, time_in_force=TimeInForce.GTC
                        )
                        trading_client.submit_order(order)
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🪙 *شراء كريبتو*: {sym}\n💰 السعر: {price_now:.2f}")
                        last_alerts[sym] = datetime.now()
    except Exception as e: logging.error(f"Crypto Error: {e}")


def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN,INTC").split(",")]

    if not API_KEY or not SECRET_KEY: raise RuntimeError("Missing Alpaca keys")

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    crypto_data_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)
    paper = os.getenv("APCA_PAPER", "true").lower() != "false"
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=paper)

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "🚀 *بوت الهجين المطور بدأ العمل*\nنظام البيع الآلي للكريبتو مفعّل الآن.")

    last_alert_time = {ticker: datetime.min for ticker in TICKERS + CRYPTO_TICKERS}
    open_trades_done_for_date = None
    open_trade_items = []
    open_trade_start_utc = None
    report_sent_for_date = None

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            
            # تشغيل محرك الكريبتو (بيع وشراء)
            run_crypto_engine(crypto_data_client, trading_client, TG_TOKEN, TG_CHAT_ID, last_alert_time)

            # منطق الأسهم
            today_ny = now_utc.astimezone(NY_TZ).date()
            if is_market_open_window(now_utc) and open_trades_done_for_date != today_ny:
                bars_df = data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute, start=now_utc - timedelta(minutes=90), end=now_utc, feed="iex")).df
                from pick_best import pick_best_3_for_open # فرضاً أنها بنفس الملف أو مدمجة
                picks = pick_best_3_for_open(bars_df, TICKERS) 
                if len(picks) >= OPEN_TRADE_COUNT:
                    open_trade_items = [{"symbol": p["symbol"], "side": p["side"]} for p in picks]
                    open_trade_start_utc, open_trades_done_for_date = now_utc, today_ny
                    for p in picks:
                        place_bracket_order(trading_client, p["symbol"], OPEN_NOTIONAL_USD, p["side"], p["price"])
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "⚡️ *تم تنفيذ صفقات افتتاح الأسهم*")

            time.sleep(60) # فحص كل دقيقة
        except Exception as e:
            logging.error(f"Main Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
