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

# -------------------- Market Order helpers --------------------
def place_market_entry(trading_client: TradingClient, symbol: str, direction: str, notional_usd: float, last_price: float):
    if direction == "long":
        order = MarketOrderRequest(symbol=symbol, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, notional=round(notional_usd, 2))
    else:
        qty = math.floor(notional_usd / max(last_price, 0.01))
        order = MarketOrderRequest(symbol=symbol, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, qty=qty)
    return trading_client.submit_order(order)

# -------------------- WebSocket handlers --------------------
async def on_quote(q):
    s = q.symbol.upper()
    if s not in state: return
    mid = (float(q.bid_price or 0) + float(q.ask_price or 0)) / 2.0
    st = state[s]
    st.last_mid = mid
    st.mids.append(mid)

async def on_trade(t):
    s = t.symbol.upper()
    if s not in state: return
    st = state[s]
    st.last_price = float(t.price or 0)
    st.trade_sizes.append(float(t.size or 0))

# -------------------- Scoring & Buffers --------------------
def compute_score(symbol: str):
    st = state[symbol]
    if len(st.mids) < MIN_POINTS: return None
    first, last = st.mids[0], st.mids[-1]
    move = (last - first) / first
    if abs(move) < MIN_MOVE_PCT: return None
    direction = "long" if move > 0 else "short"
    return {"symbol": symbol, "score": abs(move) * 10000, "move": move, "last": last, "direction": direction, "last_price": st.last_price or last}

def reset_window_buffers():
    for s in SYMBOLS:
        state[s].mids.clear()
        state[s].trade_sizes.clear()

# -------------------- Monitoring & Selling Logic --------------------
def monitor_and_manage_positions(trading: TradingClient, active_positions):
    """دالة مراقبة الأرباح والبيع الآلي بناءً على شروطك"""
    while active_positions:
        for symbol in list(active_positions.keys()):
            try:
                pos = trading.get_open_position(symbol)
                unrealized_pl = float(pos.unrealized_pl)
                qty = float(pos.qty)
                st = state[symbol]

                # تحديث أعلى ربح
                if unrealized_pl > st.highest_profit:
                    st.highest_profit = unrealized_pl

                # 1. وقف الخسارة 100$
                if unrealized_pl <= -100:
                    trading.close_position(symbol)
                    send_tg(f"🚨 {symbol}: تم ضرب وقف الخسارة (-100$). إغلاق المركز.")
                    active_positions.pop(symbol)
                    continue

                # 2. حجز أرباح عند 40$ (بيع نصف الكمية)
                if unrealized_pl >= 40 and not st.partial_sold:
                    half_qty = abs(qty) / 2
                    side = OrderSide.SELL if float(pos.qty) > 0 else OrderSide.BUY
                    trading.submit_order(MarketOrderRequest(symbol=symbol, qty=half_qty, side=side, time_in_force=TimeInForce.DAY))
                    st.partial_sold = True
                    send_tg(f"💰 {symbol}: تم ربح 40$.. بيع نصف الكمية لضمان الربح.")

                # 3. ملاحقة الربح (نزل 3$ من القمة)
                if st.highest_profit > 0 and (st.highest_profit - unrealized_pl) >= 3:
                    trading.close_position(symbol)
                    send_tg(f"📉 {symbol}: تراجع الربح 3$ من القمة ({st.highest_profit}$). إغلاق المركز.")
                    active_positions.pop(symbol)
                    continue

            except Exception as e:
                # إذا لم يجد المركز (تم بيعه يدوياً مثلاً) يحذفه من القائمة
                active_positions.pop(symbol, None)
        
        time.sleep(1) # فحص كل ثانية

# -------------------- Main --------------------
def main():
    trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    stream = StockDataStream(API_KEY, API_SECRET, feed=FEED)
    for s in SYMBOLS:
        stream.subscribe_quotes(on_quote, s)
        stream.subscribe_trades(on_trade, s)
    
    t = threading.Thread(target=stream.run, daemon=True)
    t.start()

    send_tg("🚀 بوت الاقتناص والبيع الآلي بدأ العمل...")

    # انتظار افتتاح السوق
    while not trading.get_clock().is_open:
        time.sleep(5)

    reset_window_buffers()
    start = time.time()
    while time.time() - start < WINDOW_SECONDS:
        time.sleep(0.2)

    scored = []
    for s in SYMBOLS:
        r = compute_score(s)
        if r: scored.append(r)
    scored.sort(key=lambda x: x["score"], reverse=True)

    filled_positions = {}
    for r in scored:
        if len(filled_positions) >= 3: break
        try:
            order = place_market_entry(trading, r["symbol"], r["direction"], NOTIONAL_PER_TRADE, r["last_price"])
            filled_positions[r["symbol"]] = order.id
            send_tg(f"✅ تم الدخول في {r['symbol']} ({r['direction']}). بدأت المراقبة الآلية.")
        except Exception as e:
            continue

    if filled_positions:
        # البدء بمراقبة المراكز المفتوحة بدلاً من إغلاق البوت
        monitor_and_manage_positions(trading, filled_positions)
    
    send_tg("🏁 تم الانتهاء من جميع العمليات وإغلاق البوت.")
    stream.stop()

if __name__ == "__main__":
    main()
