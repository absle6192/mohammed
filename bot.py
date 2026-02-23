import os
import time
import requests
import logging
import pandas as pd
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ================== إعدادات سكالب ثابتة ==================
LOOKBACK_MIN = 40
LOOP_SEC = 8
ALERT_COOLDOWN_SEC = 150   # 2.5 دقيقة

MAX_SPREAD_PCT = 0.0022    # صارم
MIN_VOL_RATIO = 1.25       # نشاط واضح
MOMENTUM_BPS = 7           # زخم حقيقي
MOMENTUM_LOOKBACK = 3

RSI_MAX_LONG = 66
RSI_MIN_SHORT = 38
MA_WINDOW = 20

# فلتر ذيل الشمعة (يمنع الانعكاس السريع)
MAX_WICK_RATIO = 0.45


# ================== Telegram ==================
def send_tg(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logging.error(f"Telegram error: {e}")


# ================== RSI ==================
def rsi(close, window=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


# ================== MAIN ==================
def main():
    API_KEY = os.getenv("APCA_API_KEY_ID")
    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    TICKERS = ["TSLA","AAPL","NVDA","AMD","GOOGL","MSFT","META"]

    client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    send_tg(TG_TOKEN, TG_CHAT_ID,
            "🚀 سكالب رادار شغال\n"
            "هدف: 7$–12$ خروج سريع\n"
            "⚠️ صلاحية الإشارة 30 ثانية")

    last_alert = {t: datetime.min.replace(tzinfo=timezone.utc) for t in TICKERS}

    while True:
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=LOOKBACK_MIN)

            bars = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=TICKERS,
                    timeframe=TimeFrame.Minute,
                    start=start,
                    end=now,
                    feed="iex"
                )
            ).df

            quotes = client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=TICKERS, feed="iex")
            )

            available = set(bars.index.get_level_values(0).unique())

            for sym in TICKERS:
                if sym not in available:
                    continue

                df = bars.xs(sym).sort_index()
                if len(df) < 25:
                    continue

                last = df.iloc[-1]
                prev = df.iloc[-(MOMENTUM_LOOKBACK+1)]

                price = float(last["close"])
                prev_price = float(prev["close"])

                # سبريد
                q = quotes.get(sym)
                if not q or not q.bid_price or not q.ask_price:
                    continue

                bid = float(q.bid_price)
                ask = float(q.ask_price)
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid

                if spread_pct > MAX_SPREAD_PCT:
                    continue

                # حجم
                vol_now = float(last["volume"])
                vol_avg = float(df["volume"].iloc[-20:-1].mean())
                if vol_avg == 0:
                    continue

                vol_ratio = vol_now / vol_avg
                if vol_ratio < MIN_VOL_RATIO:
                    continue

                # زخم
                mom_bps = ((price - prev_price) / prev_price) * 10000
                if abs(mom_bps) < MOMENTUM_BPS:
                    continue

                # RSI + MA
                df["rsi"] = rsi(df["close"])
                rsi_now = df["rsi"].iloc[-1]
                ma = df["close"].iloc[-MA_WINDOW:-1].mean()

                # فلتر ذيل الشمعة
                candle_body = abs(last["close"] - last["open"])
                candle_range = last["high"] - last["low"]
                if candle_range == 0:
                    continue

                wick_ratio = (candle_range - candle_body) / candle_range
                if wick_ratio > MAX_WICK_RATIO:
                    continue

                direction = None

                if price > ma and mom_bps > 0 and rsi_now < RSI_MAX_LONG:
                    direction = "LONG"
                elif price < ma and mom_bps < 0 and rsi_now > RSI_MIN_SHORT:
                    direction = "SHORT"

                if not direction:
                    continue

                if (now - last_alert[sym]).total_seconds() < ALERT_COOLDOWN_SEC:
                    continue

                strength = "⭐ قوية" if abs(mom_bps) > 10 else "⚡ متوسطة"

                msg = (
                    f"{'🚀' if direction=='LONG' else '📉'} *{direction} سكالب: {sym}*\n"
                    f"💰 السعر: {price:.2f}\n"
                    f"⚡ زخم: {mom_bps:.1f} bps\n"
                    f"📦 حجم: x{vol_ratio:.2f}\n"
                    f"↔️ سبريد: {spread_pct*100:.2f}%\n"
                    f"{strength}\n\n"
                    f"🎯 هدفك: +7$ إلى +12$\n"
                    f"⏳ صلاحية: 30 ثانية"
                )

                send_tg(TG_TOKEN, TG_CHAT_ID, msg)
                last_alert[sym] = now
                logging.info(f"Sent {direction} {sym}")

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(15)

        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
