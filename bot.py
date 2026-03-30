import os
import time
import math
import logging
import threading
import requests
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import pytz

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
WINDOW_SECONDS = env_int("OPEN_WINDOW_SECONDS", "45")
MIN_POINTS = env_int("MIN_POINTS", "20")
MIN_MOVE_PCT = env_float("MIN_MOVE_PCT", "0.0006")
MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.0025")
ALLOW_SHORT = env_bool("ALLOW_SHORT", "true")

FEED_NAME = env("DATA_FEED", "iex").lower()
FEED = DataFeed.IEX if FEED_NAME == "iex" else DataFeed.SIP

API_KEY = env("APCA_API_KEY_ID")
API_SECRET = env("APCA_API_SECRET_KEY")
PAPER = env_bool("ALPACA_PAPER", "true")

@dataclass
class SymState:
    mids: deque
    spreads: deque
    trade_sizes: deque
    last_mid: float = 0.0
    last_spread: float = 0.0
    last_price: float = 0.0
    highest_profit: float = 0.0
    partial_sold: bool = False

state = {s: SymState(deque(maxlen=600), deque(maxlen=600), deque(maxlen=600)) for s in SYMBOLS}

def is_market_still_open():
    """يفحص إذا كان الوقت الحالي قبل إغلاق السوق (11:00 م بتوقيت السعودية)"""
    tz_sa = pytz.timezone('Asia/Riyadh')
    now = datetime.now(tz_sa)
    # السوق يغلق 11:00 م (الساعة 23)
    if now.hour >= 23:
        return False
    return True

# -------------------- Market Order helpers --------------------
def place_market_entry(trading_client: TradingClient, symbol: str, direction: str, notional_usd: float, last_price: float):
    if direction == "long":
        order = MarketOrderRequest(
            symbol=symbol, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, notional=round(notional_usd, 2)
        )
        return trading_client.submit_order(order)
    if not ALLOW_SHORT:
        raise RuntimeError("Short is disabled")
    qty = math.floor(notional_usd / max(last_price, 0.01))
    order = MarketOrderRequest(symbol=symbol, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, qty=qty)
    return trading_client.submit_order(order)

# -------------------- Monitoring Logic --------------------
def monitor_and_sell(trading_client, symbol):
    st = state[symbol]
    st.highest_profit = 0.0
    st.partial_sold = False
    while True:
        try:
            pos = trading_client.get_open_position(symbol)
            profit = float(pos.unrealized_pl)
            qty = abs(float(pos.qty))
            if profit > st.highest_profit: st.highest_profit = profit
            
            if profit <= -100:
                trading_client.close_position(symbol)
                send_tg(f"🚨 {symbol}: ضرب وقف الخسارة (-100$).")
                break
            if profit >= 70 and not st.partial_sold:
                side = OrderSide.SELL if float(pos.qty) > 0 else OrderSide.BUY
                trading_client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty/2, side=side, time_in_force=TimeInForce.DAY))
                st.partial_sold = True
                send_tg(f"💰 {symbol}: بيع نصف الكمية (ربح +70$).")
            if st.highest_profit > 0 and (st.highest_profit - profit) >= 10:
                trading_client.close_position(symbol)
                send_tg(f"📉 {symbol}: تراجع الربح 10$ من القمة ({st.highest_profit}$). تم الإغلاق.")
                break
            time.sleep(1)
        except: break

# -------------------- WebSocket handlers --------------------
def on_quote(q):
    s = q.symbol.upper()
    if s not in state: return
    mid = (float(q.bid_price) + float(q.ask_price)) / 2.0
    st = state[s]
    st.last_mid = mid
    st.mids.append(mid)
    st.last_spread = (float(q.ask_price) - float(q.bid_price)) / mid

def on_trade(t):
    s = t.symbol.upper()
    if s not in state: return
    state[s].last_price = float(t.price)
    state[s].trade_sizes.append(float(t.size))

def reset_window_buffers():
    for s in SYMBOLS:
        state[s].mids.clear()
        state[s].spreads.clear()
        state[s].trade_sizes.clear()

# -------------------- Scoring --------------------
def compute_score(symbol: str):
    st = state[symbol]
    if len(st.mids) < MIN_POINTS: return None
    first, last = st.mids[0], st.mids[-1]
    move = (last - first) / first
    if abs(move) < MIN_MOVE_PCT: return None
    direction = "long" if move > 0 else "short"
    vol = sum(st.trade_sizes)
    score = (abs(move) * 10000.0) + (math.log1p(vol) * 10.0) - (st.last_spread * 200.0)
    return {"symbol": symbol, "score": score, "direction": direction, "last_price": st.last_price or last, "spread": st.last_spread}

# -------------------- Main --------------------
def main():
    trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    stream = StockDataStream(API_KEY, API_SECRET, feed=FEED)
    for s in SYMBOLS:
        stream.subscribe_quotes(on_quote, s)
        stream.subscribe_trades(on_trade, s)
    threading.Thread(target=stream.run, daemon=True).start()
    
    send_tg("🚀 Open-3 Bot started | Continuous Mode Enabled")

    while is_market_still_open():
        try:
            # انتظار فتح السوق الأمريكي
            clock = trading.get_clock()
            if not clock.is_open:
                time.sleep(30)
                continue

            reset_window_buffers()
            start_scan = time.time()
            while time.time() - start_scan < WINDOW_SECONDS: time.sleep(1)

            scored = []
            for s in SYMBOLS:
                r = compute_score(s)
                if r and (r["direction"] != "short" or ALLOW_SHORT): scored.append(r)
            
            scored.sort(key=lambda x: x["score"], reverse=True)
            if not scored:
                time.sleep(10)
                continue

            filled = []
            for r in scored:
                if len(filled) >= 2: break # الحد الأقصى مركزين حسب طلبك السابق
                if r["spread"] > MAX_SPREAD_PCT: continue
                try:
                    place_market_entry(trading, r["symbol"], r["direction"], NOTIONAL_PER_TRADE, r["last_price"])
                    filled.append(r["symbol"])
                    threading.Thread(target=monitor_and_sell, args=(trading, r["symbol"]), daemon=True).start()
                    send_tg(f"✅ ENTRY: {r['symbol']} | {r['direction'].upper()}")
                except: continue

            # البوت ينتظر هنا طالما هناك صفقات مفتوحة
            while True:
                pos = trading.get_all_positions()
                if not pos: break
                time.sleep(10)
            
            send_tg("♻️ All positions closed. Searching for new opportunities...")
            
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(10)

    send_tg("🏁 Market closed (Saudi Time). Bot stopping for the day.")

if __name__ == "__main__":
    main()
