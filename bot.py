import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# مكتبات الأسهم
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات الحساسية (A-Grade) ---
RSI_MAX_LONG = 62
MA_WINDOW = 20
MIN_TREND_PCT = 0.20  # نسبة % (مثال: 0.20 يعني 0.20%)

# --- إعدادات تداول الأسهم ---
OPEN_NOTIONAL_USD = 30000
OPEN_TRADE_COUNT = 3
TAKE_PROFIT_PCT = 0.30
STOP_LOSS_PCT   = 0.20
MAX_HOLD_MINUTES = 15
NY_TZ = ZoneInfo("America/New_York")

# --- إعدادات إشعارات الفرص بعد الافتتاح ---
POST_OPEN_LOOKBACK_MIN = 45      # نجلب بيانات آخر 45 دقيقة لحساب المؤشرات
POST_OPEN_TREND_LOOKBACK_MIN = 20  # نحسب Trend على آخر 20 دقيقة
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "300"))  # افتراضي 5 دقائق لكل سهم
SLEEP_SECONDS = 5  # بدل 60

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


def is_regular_market_hours(now_utc: datetime) -> bool:
    """لإشعارات ما بعد الافتتاح: فقط وقت السوق العادي"""
    now_ny = now_utc.astimezone(NY_TZ)
    if now_ny.weekday() >= 5:
        return False
    start = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    end   = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
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
        symbol=symbol,
        notional=notional,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit={"limit_price": tp_price},
        stop_loss={"stop_price": sl_price},
    )
    return trading_client.submit_order(order)


def get_bars_df(data_client: StockHistoricalDataClient, tickers: list[str], start_utc: datetime, end_utc: datetime) -> pd.DataFrame:
    """يرجع DF لشموع الدقيقة لكل الأسهم المطلوبة"""
    req = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Minute,
        start=start_utc,
        end=end_utc,
        feed="iex"
    )
    df = data_client.get_stock_bars(req).df
    # df عادة MultiIndex: (symbol, timestamp)
    return df


def build_post_open_signal(df_symbol: pd.DataFrame) -> dict | None:
    """
    إشارة شراء (LONG) بعد الافتتاح:
    - Trend% آخر POST_OPEN_TREND_LOOKBACK_MIN دقيقة >= MIN_TREND_PCT
    - Close فوق MA20
    - RSI < RSI_MAX_LONG
    """
    if df_symbol is None or len(df_symbol) < max(MA_WINDOW + 5, POST_OPEN_TREND_LOOKBACK_MIN + 2):
        return None

    closes = df_symbol["close"].astype(float)

    ma = closes.rolling(MA_WINDOW).mean()
    rsi = calculate_rsi(closes, window=14)

    last_close = float(closes.iloc[-1])
    last_ma = float(ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else None
    last_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None

    if last_ma is None or last_rsi is None:
        return None

    # Trend% على آخر N دقيقة
    n = POST_OPEN_TREND_LOOKBACK_MIN
    prev_close = float(closes.iloc[-(n + 1)])
    if prev_close <= 0:
        return None

    trend_pct = ((last_close - prev_close) / prev_close) * 100.0

    # شروط الإشارة
    if trend_pct >= MIN_TREND_PCT and last_close > last_ma and last_rsi <= RSI_MAX_LONG:
        return {
            "price": last_close,
            "ma": last_ma,
            "rsi": last_rsi,
            "trend_pct": trend_pct
        }

    return None


def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TICKERS = [t.strip().upper() for t in os.getenv(
        "TICKERS",
        "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN,INTC"
    ).split(",")]

    if not API_KEY or not SECRET_KEY:
        raise RuntimeError("Missing Alpaca keys")

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    paper = os.getenv("APCA_PAPER", "true").lower() != "false"
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=paper)

    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "🚀 *بوت الأسهم بدأ العمل*\nبعد الافتتاح: سيتم إرسال فرص شراء (تنبيه فقط).")

    last_alert_time = {ticker: datetime.min.replace(tzinfo=timezone.utc) for ticker in TICKERS}
    open_trades_done_for_date = None

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_ny = now_utc.astimezone(NY_TZ).date()

            # 1) صفقات الافتتاح (كما هي)
            if is_market_open_window(now_utc) and open_trades_done_for_date != today_ny:
                bars_df = get_bars_df(
                    data_client,
                    TICKERS,
                    start_utc=now_utc - timedelta(minutes=90),
                    end_utc=now_utc
                )

                from pick_best import pick_best_3_for_open
                picks = pick_best_3_for_open(bars_df, TICKERS)

                if len(picks) >= OPEN_TRADE_COUNT:
                    open_trades_done_for_date = today_ny
                    for p in picks[:OPEN_TRADE_COUNT]:
                        place_bracket_order(
                            trading_client,
                            p["symbol"],
                            OPEN_NOTIONAL_USD,
                            p["side"],
                            p["price"]
                        )
                    send_tg_msg(TG_TOKEN, TG_CHAT_ID, "⚡️ *تم تنفيذ صفقات افتتاح الأسهم*")

            # 2) بعد الافتتاح: تنبيهات فرص (بدون تداول)
            if open_trades_done_for_date == today_ny and is_regular_market_hours(now_utc) and (not is_market_open_window(now_utc)):
                bars_df = get_bars_df(
                    data_client,
                    TICKERS,
                    start_utc=now_utc - timedelta(minutes=POST_OPEN_LOOKBACK_MIN),
                    end_utc=now_utc
                )

                # تنظيم الداتا لكل سهم (MultiIndex)
                for sym in TICKERS:
                    try:
                        # قد تختلف طريقة الفهرسة حسب df، لذا نتعامل بحذر
                        if isinstance(bars_df.index, pd.MultiIndex):
                            if sym not in bars_df.index.get_level_values(0):
                                continue
                            df_sym = bars_df.xs(sym, level=0).copy()
                        else:
                            # في حالة غير متوقعة
                            df_sym = bars_df[bars_df["symbol"] == sym].copy() if "symbol" in bars_df.columns else None

                        if df_sym is None or df_sym.empty:
                            continue

                        sig = build_post_open_signal(df_sym)
                        if not sig:
                            continue

                        # تهدئة الإشعارات (Cooldown)
                        if (now_utc - last_alert_time[sym]).total_seconds() < ALERT_COOLDOWN_SEC:
                            continue

                        last_alert_time[sym] = now_utc

                        msg = (
                            f"📌 *فرصة شراء بعد الافتتاح*\n"
                            f"• السهم: *{sym}*\n"
                            f"• السعر: `{sig['price']:.2f}`\n"
                            f"• Trend آخر {POST_OPEN_TREND_LOOKBACK_MIN}د: `{sig['trend_pct']:.2f}%`\n"
                            f"• MA{MA_WINDOW}: `{sig['ma']:.2f}`\n"
                            f"• RSI: `{sig['rsi']:.1f}`\n"
                            f"\n✅ *تنبيه فقط* — قرار الدخول يدوي."
                        )
                        send_tg_msg(TG_TOKEN, TG_CHAT_ID, msg)

                    except Exception as inner_e:
                        logging.error(f"Signal Error {sym}: {inner_e}")

            time.sleep(SLEEP_SECONDS)  # بدل 60

        except Exception as e:
            logging.error(f"Main Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
