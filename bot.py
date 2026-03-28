import os
import time
import math
import logging
import threading
import requests
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# -------------------- ENV helpers --------------------
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

def env_int(name: str, default: str) -> int:
    return int(env(name, default))

def env_float(name: str, default: str) -> float:
    return float(env(name, default))

def env_bool(name: str, default: str = "false") -> bool:
    return env(name, default).lower() in ("1", "true", "yes", "y", "on")


# -------------------- Telegram --------------------
def send_tg(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logging.warning(f"Telegram send failed: {e}")


# -------------------- Config --------------------
SYMBOLS = [s.strip().upper() for s in env("SYMBOLS", "TSLA,AAPL,NVDA,AMD,GOOGL,MSFT,META,AMZN,MU").split(",") if s.strip()]
NOTIONAL_PER_TRADE = env_float("OPEN_NOTIONAL_USD", "30000")

WINDOW_SECONDS = env_int("OPEN_WINDOW_SECONDS", "45")          # تجمع بيانات كم ثانية بعد الافتتاح
MIN_POINTS = env_int("MIN_POINTS", "20")                       # أقل عدد تيكات/نقاط سعر
MIN_MOVE_PCT = env_float("MIN_MOVE_PCT", "0.0006")             # 0.06% (مناسب للافتتاح)
MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.0025")         # 0.25%
COOLDOWN_AFTER = env_int("COOLDOWN_AFTER_OPEN_TRADES", "9999") # نخليه كبير عشان ما يعيد يدخل

ALLOW_SHORT = env_bool("ALLOW_SHORT", "true")

FEED_NAME = env("DATA_FEED", "iex").lower()
FEED = DataFeed.IEX if FEED_NAME == "iex" else DataFeed.SIP

API_KEY = env("APCA_API_KEY_ID")
API_SECRET = env("APCA_API_SECRET_KEY")
PAPER = env_bool("ALPACA_PAPER", "true")  # خله مثل ما هو (واضح عندك موجود)

@dataclass
class SymState:
    mids: deque   # mid prices
    spreads: deque  # spread pct
    trade_sizes: deque  # trade sizes
    last_mid: float = 0.0
    last_spread: float = 0.0
    last_price: float = 0.0  # last traded price
    highest_profit: float = 0.0 # لتتبع أعلى ربح للملاحقة
    partial_sold: bool = False  # هل تم بيع النصف؟


state = {s: SymState(deque(maxlen=600), deque(maxlen=600), deque(maxlen=600)) for s in SYMBOLS}


# -------------------- Market Order helpers --------------------
def place_market_entry(trading_client: TradingClient, symbol: str, direction: str, notional_usd: float, last_price: float):
    if direction == "long":
        order = MarketOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            notional=round(notional_usd, 2),
        )
        return trading_client.submit_order(order)

    if not ALLOW_SHORT:
        raise RuntimeError("Short is disabled by ALLOW_SHORT=false")

    qty = math.floor(notional_usd / max(last_price, 0.01))
    if qty <= 0:
        raise ValueError(f"qty computed 0 for {symbol} (notional={notional_usd}, last={last_price})")

    order = MarketOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        qty=qty,
    )
    return trading_client.submit_order(order)

# -------------------- New Monitoring Logic (Added) --------------------
def monitor_and_sell(trading_client, symbol):
    """وظيفة المراقبة التي ستعمل في الخلفية لكل سهم يتم شراؤه"""
    st = state[symbol]
    while True:
        try:
            pos = trading_client.get_open_position(symbol)
            profit = float(pos.unrealized_pl)
            qty = abs(float(pos.qty))
            
            # تحديث أعلى ربح وصل له السهم للملاحقة
            if profit > st.highest_profit:
                st.highest_profit = profit

            # 1. وقف الخسارة الصارم (100 دولار)
            if profit <= -100:
                trading_client.close_position(symbol)
                send_tg(f"🚨 {symbol}: ضرب وقف الخسارة (-100$). تم إغلاق المركز.")
                break

            # 2. بيع نصف الكمية لضمان الربح (عند وصول الربح 40 دولار)
            if profit >= 40 and not st.partial_sold:
                side = OrderSide.SELL if float(pos.qty) > 0 else OrderSide.BUY
                trading_client.submit_order(MarketOrderRequest(
                    symbol=symbol, qty=qty/2, side=side, time_in_force=TimeInForce.DAY
                ))
                st.partial_sold = True
                send_tg(f"💰 {symbol}: وصل الربح 40$. تم بيع نصف الكمية.")

            # 3. ملاحقة الربح (إذا نزل 3$ من أعلى قمة ربح وصل لها)
            if st.highest_profit > 0 and (st.highest_profit - profit) >= 3:
                trading_client.close_position(symbol)
                send_tg(f"📉 {symbol}: تراجع الربح 3$ من القمة ({st.highest_profit}$). تم إغلاق المتبقي.")
                break
                
            time.sleep(1) # فحص الأرباح كل ثانية
        except Exception:
            # إذا لم يجد المركز (يعني تم إغلاقه) يخرج من المراقبة
            break

# -------------------- WebSocket handlers --------------------
async def on_quote(q):
    s = q.symbol.upper()
    if s not in state:
        return
    bid = float(q.bid_price or 0)
    ask = float(q.ask_price or 0)
    if bid <= 0 or ask <= 0:
        return
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid if mid > 0 else 0.0

    st = state[s]
    st.last_mid = mid
    st.last_spread = spread_pct
    st.mids.append(mid)
    st.spreads.append(spread_pct)

async def on_trade(t):
    s = t.symbol.upper()
    if s not in state:
        return
    price = float(t.price or 0)
    size = float(t.size or 0)
    if price <= 0:
        return
    st = state[s]
    st.last_price = price
    st.trade_sizes.append(size)


# -------------------- Scoring --------------------
def compute_score(symbol: str):
    st = state[symbol]
    if len(st.mids) < MIN_POINTS:
        return None
    if st.last_spread <= 0 or st.last_spread > MAX_SPREAD_PCT:
        return None
    first = st.mids[0]
    last = st.mids[-1]
    if first <= 0:
        return None
    move = (last - first) / first
    if abs(move) < MIN_MOVE_PCT:
        return None
    ma = sum(st.mids) / len(st.mids)
    trend_ok_long = (last > ma)
    trend_ok_short = (last < ma)
    if move > 0 and trend_ok_long:
        direction = "long"
    elif move < 0 and trend_ok_short:
        direction = "short"
    else:
        direction = "long" if move > 0 else "short"
    vol = sum(st.trade_sizes) if len(st.trade_sizes) else 0.0
    vol_score = math.log1p(vol)
    spread_pen = st.last_spread * 100.0
    score = (abs(move) * 10000.0) + (vol_score * 10.0) - (spread_pen * 2.0)
    return {
        "symbol": symbol, "score": score, "move": move, "ma": ma, "last": last,
        "spread": st.last_spread, "vol": vol, "direction": direction, "last_price": st.last_price or last,
    }

def reset_window_buffers():
    for s in SYMBOLS:
        state[s].mids.clear()
        state[s].spreads.clear()
        state[s].trade_sizes.clear()


# -------------------- Main --------------------
def main():
    trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    stream = StockDataStream(API_KEY, API_SECRET, feed=FEED)

    for s in SYMBOLS:
        stream.subscribe_quotes(on_quote, s)
        stream.subscribe_trades(on_trade, s)

    def run_stream():
        stream.run()

    t = threading.Thread(target=run_stream, daemon=True)
    t.start()

    send_tg(f"🚀 Open-3 Bot started | Monitor Enabled")

    while True:
        try:
            clock = trading.get_clock()
            if clock.is_open: break
            time.sleep(5)
        except Exception: time.sleep(5)

    reset_window_buffers()
    start = time.time()
    send_tg(f"⏱️ Collecting {WINDOW_SECONDS}s data...")

    while time.time() - start < WINDOW_SECONDS:
        time.sleep(0.2)

    scored = []
    for s in SYMBOLS:
        r = compute_score(s)
        if r:
            if r["direction"] == "short" and not ALLOW_SHORT: continue
            scored.append(r)

    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored:
        send_tg("⚠️ No candidates passed filters.")
        return

    filled = []
    for r in scored:
        if len(filled) >= 3: break
        symbol = r["symbol"]
        direction = r["direction"]
        last_price = float(r["last_price"] or r["last"])
        
        if r["spread"] > MAX_SPREAD_PCT: continue

        try:
            order = place_market_entry(trading, symbol, direction, NOTIONAL_PER_TRADE, last_price)
            filled.append(symbol)
            send_tg(f"✅ ENTRY: {symbol} | {direction.upper()} | المراقبة الآلية بدأت")
            
            # --- الإضافة الوحيدة هنا: تشغيل المراقبة لكل سهم يتم شراؤه ---
            threading.Thread(target=monitor_and_sell, args=(trading, symbol), daemon=True).start()
            
        except Exception as e:
            logging.warning(f"Order rejected for {symbol}: {e}")
            continue

    if filled:
        send_tg(f"🎯 Positions: {', '.join(filled)}. البوت سيغلق المراكز آلياً حسب الشروط.")
    
    # البوت يبقى حياً طالما هناك مراكز مفتوحة للمراقبة
    while True:
        try:
            positions = trading.get_all_positions()
            if not positions: break
            time.sleep(10)
        except: break

    send_tg("🏁 All positions closed. Bot stopping.")
    try: stream.stop()
    except: pass

if __name__ == "__main__":
    main()
