import os
import time
import math
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
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
COOLDOWN_AFTER = env_int("COOLDOWN_AFTER_OPEN_TRADES", "9999")

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


state = {s: SymState(deque(maxlen=600), deque(maxlen=600), deque(maxlen=600)) for s in SYMBOLS}


# ===================== SELL LOGIC =====================
sell_state = {}

def manage_positions(trading):
    while True:
        try:
            positions = trading.get_all_positions()

            for pos in positions:
                symbol = pos.symbol
                qty = int(float(pos.qty))
                profit = float(pos.unrealized_pl)

                if symbol not in sell_state:
                    sell_state[symbol] = {
                        "highest": profit,
                        "partial_sold": False
                    }

                s = sell_state[symbol]

                if profit > s["highest"]:
                    s["highest"] = profit

                # Stop Loss
                if profit <= -12:
                    trading.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side="sell",
                        type="market",
                        time_in_force="day"
                    )
                    send_tg(f"🔴 SOLD {symbol} | Loss: {round(profit,2)}$")
                    sell_state.pop(symbol, None)
                    continue

                # Partial Sell
                if profit >= 40 and not s["partial_sold"]:
                    half = qty // 2
                    if half > 0:
                        trading.submit_order(
                            symbol=symbol,
                            qty=half,
                            side="sell",
                            type="market",
                            time_in_force="day"
                        )
                        send_tg(f"💰 PARTIAL SELL {symbol} | Profit: {round(profit/2,2)}$")
                        s["partial_sold"] = True

                # Trailing
                if profit >= 7:
                    gap = 2
                    if profit >= 15:
                        gap = 3
                    if profit >= 25:
                        gap = 5

                    if profit <= s["highest"] - gap:
                        trading.submit_order(
                            symbol=symbol,
                            qty=qty,
                            side="sell",
                            type="market",
                            time_in_force="day"
                        )
                        send_tg(f"🚀 TRAILING SELL {symbol} | Profit: {round(profit,2)}$")
                        sell_state.pop(symbol, None)
                        continue

            time.sleep(2)

        except Exception as e:
            print("Manage error:", e)
            time.sleep(5)
# =====================================================


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
        raise ValueError(f"qty computed 0 for {symbol}")

    order = MarketOrderRequest(
        symbol=symbol,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        qty=qty,
    )
    return trading_client.submit_order(order)


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
        "symbol": symbol,
        "score": score,
        "move": move,
        "ma": ma,
        "last": last,
        "spread": st.last_spread,
        "vol": vol,
        "direction": direction,
        "last_price": st.last_price or last,
    }


def reset_window_buffers():
    for s in SYMBOLS:
        state[s].mids.clear()
        state[s].spreads.clear()
        state[s].trade_sizes.clear()


# -------------------- Main --------------------
def main():
    trading = TradingClient(API_KEY, API_SECRET, paper=PAPER)

    # تشغيل البيع في الخلفية
    import threading
    threading.Thread(target=manage_positions, args=(trading,), daemon=True).start()

    # Start websocket
    stream = StockDataStream(API_KEY, API_SECRET, feed=FEED)

    for s in SYMBOLS:
        stream.subscribe_quotes(on_quote, s)
        stream.subscribe_trades(on_trade, s)

    def run_stream():
        stream.run()

    t = threading.Thread(target=run_stream, daemon=True)
    t.start()

    send_tg(
        "🚀 Open-3 Bot started\n"
        f"Symbols: {','.join(SYMBOLS)}\n"
        f"Feed: {FEED_NAME}\n"
        f"Window: {WINDOW_SECONDS}s | MinMove: {MIN_MOVE_PCT*100:.3f}% | MaxSpread: {MAX_SPREAD_PCT*100:.2f}%\n"
        f"Notional/Trade: ${NOTIONAL_PER_TRADE:,.0f} | Short: {ALLOW_SHORT}\n"
    )

    ny = ZoneInfo("America/New_York")
    while True:
        try:
            clock = trading.get_clock()
            if clock.is_open:
                break
            time.sleep(5)
        except Exception as e:
            logging.warning(f"Clock error: {e}")
            time.sleep(5)

    reset_window_buffers()
    start = time.time()
    logging.info("Market open detected. Collecting window...")
    send_tg(f"⏱️ Market OPEN detected. Collecting {WINDOW_SECONDS}s data to pick best 3...")

    while time.time() - start < WINDOW_SECONDS:
        time.sleep(0.2)

    scored = []
    for s in SYMBOLS:
        r = compute_score(s)
        if r:
            if r["direction"] == "short" and not ALLOW_SHORT:
                continue
            scored.append(r)

    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored:
        msg = "⚠️ No candidates passed filters.\n" \
              "Try lowering MIN_MOVE_PCT or MIN_POINTS, or increase MAX_SPREAD_PCT slightly."
        send_tg(msg)
        logging.warning(msg)
        return

    picks = []
    for r in scored:
        if len(picks) >= 3:
            break
        picks.append(r)

    filled = []
    rejected = []

    for r in scored:
        if len(filled) >= 3:
            break

        symbol = r["symbol"]
        direction = r["direction"]
        last_price = float(r["last_price"] or r["last"])
        spread_pct = r["spread"]

        if spread_pct > MAX_SPREAD_PCT:
            continue

        try:
            order = place_market_entry(trading, symbol, direction, NOTIONAL_PER_TRADE, last_price)
            filled.append((symbol, direction, order.id))
            send_tg(
                f"✅ ENTRY (Market)\n"
                f"{symbol} | {direction.upper()}\n"
                f"Last≈ {last_price:.2f}\n"
                f"Move({WINDOW_SECONDS}s): {r['move']*100:.3f}% | Spread: {spread_pct*100:.3f}%\n"
                f"Score: {r['score']:.1f}\n"
                f"⚠️ البيع الآن تلقائي"
            )
            logging.info(f"Submitted {symbol} {direction} order_id={order.id}")
        except Exception as e:
            rejected.append((symbol, str(e)))
            logging.warning(f"Order rejected for {symbol}: {e}")
            continue

    if filled:
        send_tg(
            "🎯 Done. Open-3 entries placed:\n" +
            "\n".join([f"- {s} {d.upper()}" for s, d, _ in filled]) +
            ("\n\n⚠️ Some were rejected:\n" + "\n".join([f"- {s}: {err[:80]}..." for s, err in rejected]) if rejected else "")
        )
    else:
        send_tg("❌ All entries were rejected. غالبًا بسبب الشورت/الهامش/عدم توفر أسهم للإقتراض أو قيود الحساب.")

    try:
        stream.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
