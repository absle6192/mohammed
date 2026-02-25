‏import os
‏import time
‏import requests
‏import logging
‏import pandas as pd
‏from datetime import datetime, timezone, timedelta
‏from zoneinfo import ZoneInfo

‏from alpaca.data.historical import StockHistoricalDataClient
‏from alpaca.data.requests import StockBarsRequest
‏from alpaca.data.timeframe import TimeFrame

‏from alpaca.trading.client import TradingClient
‏from alpaca.trading.requests import MarketOrderRequest
‏from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

‏logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- إعدادات التنبيهات (A-Grade) ---
‏RSI_MAX_LONG = 62
‏RSI_MIN_SHORT = 38
‏MA_WINDOW = 20

‏MIN_TREND_PCT = 0.20
‏MIN_RSI_BUFFER = 4.0

# --- إعدادات تداول الافتتاح ---
‏OPEN_NOTIONAL_USD = 30000
‏OPEN_TRADE_COUNT = 3

‏# Bracket defaults
‏TAKE_PROFIT_PCT = 0.30
‏STOP_LOSS_PCT   = 0.20

‏MAX_HOLD_MINUTES = 15
‏NY_TZ = ZoneInfo("America/New_York")


‏def send_tg_msg(token, chat_id, text):
‏    if not token or not chat_id:
‏        return
‏    try:
‏        requests.post(
‏            f"https://api.telegram.org/bot{token}/sendMessage",
‏            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
‏            timeout=10
        )
‏    except Exception as e:
‏        logging.error(f"Telegram Error: {e}")


‏def calculate_rsi(series: pd.Series, window=14):
‏    delta = series.diff()
‏    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
‏    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
‏    rs = gain / loss
‏    return 100 - (100 / (1 + rs))


‏def is_market_open_window(now_utc: datetime) -> bool:
    """نافذة تنفيذ أوامر الافتتاح: من 09:30:05 إلى 09:31:30 بتوقيت نيويورك"""
‏    now_ny = now_utc.astimezone(NY_TZ)
‏    if now_ny.weekday() >= 5:
‏        return False
‏    start = now_ny.replace(hour=9, minute=30, second=5, microsecond=0)
‏    end   = now_ny.replace(hour=9, minute=31, second=30, microsecond=0)
‏    return start <= now_ny <= end


‏def pick_best_3_for_open(bars_df: pd.DataFrame, tickers: list[str]) -> list[dict]:
    """
    اختيار أفضل 3 إشارات A-Grade من قائمة 9 (Long/Short)
    يرجع قائمة عناصر: {"symbol":..., "side":"LONG|SHORT", "score":..., "price":..., "rsi":..., "trend":...}
    """
‏    candidates = []

‏    for sym in tickers:
‏        if sym not in bars_df.index:
‏            continue

‏        df = bars_df.xs(sym).sort_index()
‏        if len(df) < max(MA_WINDOW + 2, 25):
‏            continue

‏        df["rsi"] = calculate_rsi(df["close"])
‏        price_now = float(df["close"].iloc[-1])
‏        ma_price = float(df["close"].iloc[-MA_WINDOW:-1].mean())
‏        rsi_now = float(df["rsi"].iloc[-1])

‏        if ma_price <= 0:
‏            continue

‏        # ========== LONG Candidate ==========
‏        if price_now > ma_price and rsi_now < RSI_MAX_LONG:
‏            trend_pct = (price_now / ma_price - 1.0) * 100.0
‏            rsi_buffer = (RSI_MAX_LONG - rsi_now)

‏            if trend_pct >= MIN_TREND_PCT and rsi_buffer >= MIN_RSI_BUFFER:
‏                score = (trend_pct * 2.0) + (rsi_buffer * 0.5)
‏                candidates.append({
‏                    "symbol": sym,
‏                    "side": "LONG",
‏                    "score": score,
‏                    "price": price_now,
‏                    "rsi": rsi_now,
‏                    "trend": trend_pct
                })

‏        # ========== SHORT Candidate ==========
‏        elif price_now < ma_price and rsi_now > RSI_MIN_SHORT:
            # مقدار الهبوط تحت المتوسط
‏            short_trend = (ma_price / price_now - 1.0) * 100.0
‏            rsi_buffer = (rsi_now - RSI_MIN_SHORT)

‏            if short_trend >= MIN_TREND_PCT and rsi_buffer >= MIN_RSI_BUFFER:
‏                score = (short_trend * 2.0) + (rsi_buffer * 0.5)
‏                candidates.append({
‏                    "symbol": sym,
‏                    "side": "SHORT",
‏                    "score": score,
‏                    "price": price_now,
‏                    "rsi": rsi_now,
‏                    "trend": short_trend
                })

‏    candidates.sort(key=lambda x: x["score"], reverse=True)
‏    return candidates[:OPEN_TRADE_COUNT]


‏def place_bracket_order(trading_client: TradingClient, symbol: str, notional: float, side: str, last_price: float):
    """
‏    Market notional + Bracket exits (TP/SL).
‏    - LONG: BUY ثم TP أعلى / SL أقل
‏    - SHORT: SELL ثم TP أقل / SL أعلى
    """
‏    if side == "LONG":
‏        order_side = OrderSide.BUY
‏        tp_price = round(last_price * (1.0 + TAKE_PROFIT_PCT / 100.0), 2)
‏        sl_price = round(last_price * (1.0 - STOP_LOSS_PCT / 100.0), 2)
‏    else:
‏        order_side = OrderSide.SELL
‏        tp_price = round(last_price * (1.0 - TAKE_PROFIT_PCT / 100.0), 2)  # take profit lower
‏        sl_price = round(last_price * (1.0 + STOP_LOSS_PCT / 100.0), 2)    # stop loss higher

‏    order = MarketOrderRequest(
‏        symbol=symbol,
‏        notional=notional,
‏        side=order_side,
‏        time_in_force=TimeInForce.DAY,
‏        order_class=OrderClass.BRACKET,
‏        take_profit={"limit_price": tp_price},
‏        stop_loss={"stop_price": sl_price}
    )
‏    return trading_client.submit_order(order)


‏def get_open_positions_symbols(trading_client: TradingClient) -> set[str]:
‏    try:
‏        positions = trading_client.get_all_positions()
‏        return {p.symbol for p in positions}
‏    except Exception:
‏        return set()


‏def close_position_market(trading_client: TradingClient, symbol: str):
‏    try:
‏        trading_client.close_position(symbol)
‏        logging.info(f"Force-closed position: {symbol}")
‏    except Exception as e:
‏        logging.error(f"Close position error {symbol}: {e}")


‏def main():
‏    API_KEY = os.getenv("APCA_API_KEY_ID")
‏    SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
‏    TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
‏    TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

‏    TICKERS = [t.strip().upper() for t in os.getenv(
‏        "TICKERS",
‏        "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN,INTC"
‏    ).split(",")]

‏    if not API_KEY or not SECRET_KEY:
‏        raise RuntimeError("Missing Alpaca keys: APCA_API_KEY_ID / APCA_API_SECRET_KEY")

‏    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

‏    paper = os.getenv("APCA_PAPER", "true").lower() != "false"
‏    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=paper)

‏    send_tg_msg(
‏        TG_TOKEN, TG_CHAT_ID,
        "📡 *رادار السوق يعمل الآن*\n"
        "سأرسل *A-Grade فقط*.\n"
        "وعند الافتتاح سأفتح 3 صفقات (30,000$ لكل صفقة) من أفضل 3 إشارات Long/Short من قائمتك."
    )

‏    last_alert_time = {ticker: datetime.min for ticker in TICKERS}

‏    open_trades_done_for_date = None
‏    open_trade_items = []         # list of dicts: symbol, side
‏    open_trade_start_utc = None
‏    report_sent_for_date = None

‏    while True:
‏        try:
‏            now_utc = datetime.now(timezone.utc)

            # ========== 1) صفقات الافتتاح Long/Short ==========
‏            today_ny = now_utc.astimezone(NY_TZ).date()
‏            if is_market_open_window(now_utc) and open_trades_done_for_date != today_ny:
‏                bars_df = data_client.get_stock_bars(
‏                    StockBarsRequest(
‏                        symbol_or_symbols=TICKERS,
‏                        timeframe=TimeFrame.Minute,
‏                        start=now_utc - timedelta(minutes=90),
‏                        end=now_utc,
‏                        feed="iex"
                    )
‏                ).df

‏                picks = pick_best_3_for_open(bars_df, TICKERS)

‏                if len(picks) < OPEN_TRADE_COUNT:
‏                    send_tg_msg(
‏                        TG_TOKEN, TG_CHAT_ID,
‏                        f"⚠️ *الافتتاح*: ما قدرت ألقى 3 إشارات A-Grade من قائمتك.\n"
‏                        f"المتاح الآن: {[p['symbol']+'('+p['side']+')' for p in picks]}"
                    )
‏                    open_trades_done_for_date = today_ny
‏                else:
‏                    open_trade_items = [{"symbol": p["symbol"], "side": p["side"]} for p in picks]
‏                    open_trade_start_utc = now_utc
‏                    open_trades_done_for_date = today_ny

‏                    lines = []
‏                    for p in picks:
‏                        sym = p["symbol"]
‏                        side = p["side"]
‏                        last_price = float(p["price"])

‏                        try:
‏                            place_bracket_order(trading_client, sym, OPEN_NOTIONAL_USD, side, last_price)
‏                            tp = (TAKE_PROFIT_PCT if side == "LONG" else TAKE_PROFIT_PCT)
‏                            sl = (STOP_LOSS_PCT if side == "LONG" else STOP_LOSS_PCT)
‏                            emoji = "🟢" if side == "LONG" else "🔴"
‏                            lines.append(
‏                                f"{emoji} ✅ {sym} *{side}* @ ~{last_price:.2f} (TP {tp}% / SL {sl}%)"
                            )
‏                        except Exception as e:
‏                            lines.append(f"❌ {sym} *{side}* فشل فتح الصفقة: {e}")

‏                    send_tg_msg(
‏                        TG_TOKEN, TG_CHAT_ID,
                        "⚡️ *تم تنفيذ صفقات الافتتاح (Top 3 Long/Short)*\n" + "\n".join(lines)
                    )
‏                    logging.info(f"Open trades submitted: {open_trade_items}")

            # ========== 2) متابعة إغلاق صفقات الافتتاح + تقرير ==========
‏            if open_trades_done_for_date == today_ny and open_trade_items and report_sent_for_date != today_ny:
‏                open_positions = get_open_positions_symbols(trading_client)

                # إغلاق زمني احتياطي
‏                if MAX_HOLD_MINUTES and open_trade_start_utc:
‏                    age_min = (now_utc - open_trade_start_utc).total_seconds() / 60.0
‏                    if age_min >= MAX_HOLD_MINUTES:
‏                        for item in open_trade_items:
‏                            sym = item["symbol"]
‏                            if sym in open_positions:
‏                                close_position_market(trading_client, sym)

‏                still_open = [i for i in open_trade_items if i["symbol"] in open_positions]
‏                if len(still_open) == 0:
‏                    msg_lines = [f"📌 {i['symbol']} ({i['side']}): تم الإغلاق ✅" for i in open_trade_items]
‏                    send_tg_msg(
‏                        TG_TOKEN, TG_CHAT_ID,
                        "📣 *تقرير صفقات الافتتاح*\n"
‏                        + "\n".join(msg_lines)
‏                        + "\n\n✅ انتهت 3 صفقات الافتتاح. تقدر تدخل يدوي الآن بناءً على إشارات A-Grade."
                    )

‏                    report_sent_for_date = today_ny
‏                    open_trade_items = []
‏                    open_trade_start_utc = None

            # ========== 3) رادار إشارات اليدوي (A-Grade فقط) ==========
‏            bars_df = data_client.get_stock_bars(
‏                StockBarsRequest(
‏                    symbol_or_symbols=TICKERS,
‏                    timeframe=TimeFrame.Minute,
‏                    start=now_utc - timedelta(minutes=60),
‏                    end=now_utc,
‏                    feed="iex"
                )
‏            ).df

‏            for sym in TICKERS:
‏                if sym not in bars_df.index:
‏                    continue

‏                df = bars_df.xs(sym).sort_index()
‏                if len(df) < max(MA_WINDOW + 2, 25):
‏                    continue

‏                df["rsi"] = calculate_rsi(df["close"])
‏                current_rsi = float(df["rsi"].iloc[-1])
‏                price_now = float(df["close"].iloc[-1])
‏                ma_price = float(df["close"].iloc[-MA_WINDOW:-1].mean())
‏                if ma_price <= 0:
‏                    continue

‏                alert_triggered = False
‏                msg = ""

‏                # Long A-Grade
‏                if price_now > ma_price and current_rsi < RSI_MAX_LONG:
‏                    trend_pct = (price_now / ma_price - 1.0) * 100.0
‏                    rsi_buffer = RSI_MAX_LONG - current_rsi
‏                    if trend_pct >= MIN_TREND_PCT and rsi_buffer >= MIN_RSI_BUFFER:
‏                        msg = (f"🚀 *A-Grade LONG (شراء): {sym}*\n"
‏                               f"💰 السعر: {price_now:.2f}\n"
‏                               f"📊 RSI: {current_rsi:.2f}\n"
‏                               f"📈 فوق المتوسط بـ: {trend_pct:.2f}%")
‏                        alert_triggered = True

‏                # Short A-Grade
‏                elif price_now < ma_price and current_rsi > RSI_MIN_SHORT:
‏                    short_trend = (ma_price / price_now - 1.0) * 100.0
‏                    rsi_buffer = current_rsi - RSI_MIN_SHORT
‏                    if short_trend >= MIN_TREND_PCT and rsi_buffer >= MIN_RSI_BUFFER:
‏                        msg = (f"📉 *A-Grade SHORT (بيع): {sym}*\n"
‏                               f"💰 السعر: {price_now:.2f}\n"
‏                               f"📊 RSI: {current_rsi:.2f}\n"
‏                               f"📉 تحت المتوسط بـ: {short_trend:.2f}%")
‏                        alert_triggered = True

‏                if alert_triggered:
‏                    if (datetime.now() - last_alert_time[sym]).total_seconds() > 900:
‏                        send_tg_msg(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), msg)
‏                        last_alert_time[sym] = datetime.now()
‏                        logging.info(f"A-Grade alert sent for {sym}")

‏        except Exception as e:
‏            logging.error(f"Error: {e}")
‏            time.sleep(30)

‏        time.sleep(60)


‏if __name__ == "__main__":
‏    main()
