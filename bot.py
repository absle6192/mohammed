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

# --- إعدادات التنبيهات (A-Grade) للأسهم والعملات ---
RSI_MAX_LONG = 62
RSI_MIN_SHORT = 38
MA_WINDOW = 20
MIN_TREND_PCT = 0.20
MIN_RSI_BUFFER = 4.0

# --- إعدادات تداول الأسهم (كما هي) ---
OPEN_NOTIONAL_USD = 30000
OPEN_TRADE_COUNT = 3
TAKE_PROFIT_PCT = 0.30
STOP_LOSS_PCT   = 0.20
MAX_HOLD_MINUTES = 15
NY_TZ = ZoneInfo("America/New_York")

# --- إعدادات العملات الرقمية (جديد) ---
CRYPTO_TICKERS = ["BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "LTC/USD", "DOGE/USD"]
CRYPTO_ORDER_AMOUNT = 500  # مبلغ الدخول لكل صفقة كريبتو
CRYPTO_TP_PCT = 2.5        # هدف الربح للكريبتو
CRYPTO_SL_PCT = 1.5        # وقف الخسارة للكريبتو


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


def calculate_rsi(series: pd.Series, window=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def is_market_open_window(now_utc: datetime) -> bool:
    now_ny = now_utc.astimezone(NY_TZ)
    if now_ny.weekday() >= 5:
        return False
    start = now_ny.replace(hour=9, minute=30, second=5, microsecond=0)
    end   = now_ny.replace(hour=9, minute=31, second=30, microsecond=0)
    return start <= now_ny <= end


def pick_best_3_for_open(bars_df: pd.DataFrame, tickers: list[str]) -> list[dict]:
    candidates = []
    for sym in tickers:
        if sym not in bars_df.index:
            continue
        df = bars_df.xs(sym).sort_index()
        if len(df) < max(MA_WINDOW + 2, 25):
            continue
        df["rsi"] = calculate_rsi(df["close"])
        price_now = float(df["close"].iloc[-1])
        ma_price = float(df["close"].iloc[-MA_WINDOW:-1].mean())
        rsi_now = float(df["rsi"].iloc[-1])
        if ma_price <= 0:
            continue
        if price_now > ma_price and rsi_now < RSI_MAX_LONG:
            trend_pct = (price_now / ma_price - 1.0) * 100.0
            if trend_pct >= MIN_TREND_PCT and (RSI_MAX_LONG - rsi_now) >= MIN_RSI_BUFFER:
                candidates.append({"symbol": sym, "side": "LONG", "score": (trend_pct * 2.0) + ((RSI_MAX_LONG - rsi_now) * 0.5), "price": price_now})
        elif price_now < ma_price and rsi_now > RSI_MIN_SHORT:
            short_trend = (ma_price / price_now - 1.0) * 100.0
            if short_trend >= MIN_TREND_PCT and (rsi_now - RSI_MIN_SHORT) >= MIN_RSI_BUFFER:
                candidates.append({"symbol": sym, "side": "SHORT", "score": (short_trend * 2.0) + ((rsi_now - RSI_MIN_SHORT) * 0.5), "price": price_now})
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:OPEN_TRADE_COUNT]


def place_bracket_order(trading_client, symbol, notional, side, last_price):
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


def get_open_positions_symbols(trading_client: TradingClient) -> set[str]:
    try:
        positions = trading_client.get_all_positions()
        return {p.symbol for p in positions}
    except Exception:
        return set()


def close_position_market(trading_client: TradingClient, symbol: str):
    try:
        trading_client.close_position(symbol)
        logging.info(f"Force-closed position: {symbol}")
    except Exception as e:
        logging.error(f"Close position error {symbol}: {e}")

# --- دالة محرك الكريبتو (الجديدة) ---
def run_crypto_engine(crypto_client, trading_client, TG_TOKEN, TG_CHAT_ID, last_alerts):
    now = datetime.now(timezone.utc)
    try:
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
                    tp = round(price_now * (1 + CRYPTO_TP_PCT/100), 2)
                    sl = round(price_now * (1 - CRYPTO_SL_PCT/100), 2)
                    order = MarketOrderRequest(symbol=sym, notional=CRYPTO_ORDER_AMOUNT, side=OrderSide.BUY, 
                                               time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
                                               take_profit={"limit_price": tp}, stop_loss={"stop_price": sl})
                    trading_client.submit_order(order)
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🪙 *تداول كريبتو تلقائي*\n✅ شراء: {sym}\n💰 السعر: {price_now:.2f}")
                    last_alerts[sym] = datetime.now()
    except Exception as e: logging.error(f"Crypto Error: {e}")


def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv("TICKERS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN,INTC").split(",")]

    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("Missing Alpaca keys")

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    crypto_data_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY) # عميل الكريبتو
    paper = os.getenv("APCA_PAPER", "true").lower() != "false"
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=paper)

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "🚀 *رادار الهجين بدأ العمل (أسهم + كريبتو)*")

    last_alert_time = {ticker: datetime.min for ticker in TICKERS + CRYPTO_TICKERS}
    open_trades_done_for_date = None
    open_trade_items = []
    open_trade_start_utc = None
    report_sent_for_date = None

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            
            # --- أ) تشغيل الكريبتو (24/7) ---
            run_crypto_engine(crypto_data_client, trading_client, TG_TOKEN, TG_CHAT_ID, last_alert_time)

            # --- ب) منطق الأسهم (كما هو بدون تغيير) ---
            today_ny = now_utc.astimezone(NY_TZ).date()
            if is_market_open_window(now_utc) and open_trades_done_for_date != today_ny:
                bars_df = data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute, start=now_utc - timedelta(minutes=90), end=now_utc, feed="iex")).df
                picks = pick_best_3_for_open(bars_df, TICKERS)
                if len(picks) >= OPEN_TRADE_COUNT:
                    open_trade_items = [{"symbol": p["symbol"], "side": p["side"]} for p in picks]
                    open_trade_start_utc, open_trades_done_for_date = now_utc, today_ny
                    for p in picks:
                        place_bracket_order(trading_client, p["symbol"], OPEN_NOTIONAL_USD, p["side"], p["price"])
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "⚡️ *تم تنفيذ صفقات افتتاح الأسهم*")

            # تقرير الإغلاق (الأسهم)
            if open_trades_done_for_date == today_ny and open_trade_items and report_sent_for_date != today_ny:
                open_positions = get_open_positions_symbols(trading_client)
                if MAX_HOLD_MINUTES and open_trade_start_utc:
                    if (now_utc - open_trade_start_utc).total_seconds() / 60.0 >= MAX_HOLD_MINUTES:
                        for item in open_trade_items:
                            if item["symbol"] in open_positions: close_position_market(trading_client, item["symbol"])
                if len([i for i in open_trade_items if i["symbol"] in open_positions]) == 0:
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "📣 *تقرير صفقات الافتتاح تم بنجاح*")
                    report_sent_for_date = today_ny

            # رادار الأسهم اليدوي
            bars_df_manual = data_client.get_stock_bars(StockBarsRequest(symbol_or_symbols=TICKERS, timeframe=TimeFrame.Minute, start=now_utc - timedelta(minutes=60), end=now_utc, feed="iex")).df
            for sym in TICKERS:
                if sym not in bars_df_manual.index: continue
                df = bars_df_manual.xs(sym).sort_index()
                if len(df) < 25: continue
                df["rsi"] = calculate_rsi(df["close"])
                price_now, current_rsi = float(df["close"].iloc[-1]), float(df["rsi"].iloc[-1])
                ma_price = float(df["close"].iloc[-MA_WINDOW:-1].mean())
                if (price_now > ma_price and current_rsi < RSI_MAX_LONG) or (price_now < ma_price and current_rsi > RSI_MIN_SHORT):
                    if (datetime.now() - last_alert_time[sym]).total_seconds() > 900:
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, f"🚀 *إشارة A-Grade سهم*: {sym} @ {price_now}")
                        last_alert_time[sym] = datetime.now()

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(30)
        time.sleep(60)

if __name__ == "__main__":
    main()
