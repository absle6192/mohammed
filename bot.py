# bot.py
import os
import time
import logging
from datetime import datetime, timezone, timedelta
from alpaca_trade_api.rest import REST, TimeFrame

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# API & ENV
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "AAPL,MSFT,AMZN,NVDA,META,TSLA,GOOGL,AMD"
).split(",") if s.strip()]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Trading Parameters (ENV)
# =========================
ENTRY_PCT        = float(os.getenv("ENTRY_PCT", "0.003"))             # 0.3% (1-min candle move)
FIXED_DPT        = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "500")) # dollars per trade
STOP_LOSS_DOLLAR = float(os.getenv("STOP_LOSS_DOLLARS", "27"))        # $ stop per position
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT", "0.006"))       # 0.6% take profit
DAILY_TARGET     = float(os.getenv("DAILY_TARGET", "133.33"))         # printed only
HEARTBEAT_SECS   = int(os.getenv("HEARTBEAT_SECS", "60"))             # 0 disables

# =========================
# Helpers
# =========================
def tick_round(price: float) -> float:
    """
    Force price to valid tick:
      - >= $1.00 : 0.01 ticks
      -  < $1.00 : 0.0001 ticks
    """
    if price <= 0:
        return 0.01
    tick = 0.01 if price >= 1.0 else 0.0001
    # round to nearest valid tick, then keep 4 dp for readability
    steps = round(price / tick)
    return round(steps * tick, 4)

def compute_qty(last_price: float) -> int:
    """Position size as fixed dollars per trade."""
    if last_price <= 0:
        return 0
    return max(1, int(FIXED_DPT // last_price))

def has_open_position(symbol: str) -> bool:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty) > 0
    except Exception:
        return False

def has_open_orders(symbol: str) -> bool:
    try:
        orders = api.list_orders(status="open", symbols=[symbol])
        return len(orders) > 0
    except Exception:
        return False

def get_last_minute_bar(symbol: str):
    """
    Get the latest 1-min bar robustly across library versions.
    Returns object with .o (open) and .c (close), or None.
    """
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=1, feed="sip")
        # some versions return list-like, some return iterator
        if not bars:
            return None
        try:
            bar = bars[0]
        except Exception:
            bar = next(iter(bars), None)
        return bar
    except Exception as e:
        logging.error(f"[BARS] Fetch error for {symbol}: {e}")
        return None

# =========================
# Main
# =========================
logging.info(
    "Starting bot | feed=sip | daily_target=$%.2f | stop_loss=$%.2f | entry_pct=%.4f"
    % (DAILY_TARGET, STOP_LOSS_DOLLAR, ENTRY_PCT)
)

last_heartbeat = time.time()

while True:
    try:
        clock = api.get_clock()

        if clock.is_open:
            # ===== Regular Market Logic (RTH) =====
            for sym in SYMBOLS:
                try:
                    # منع التكرار على نفس السهم
                    if has_open_position(sym) or has_open_orders(sym):
                        logging.info(f"[SKIP] {sym} has open position/order. Skip.")
                        continue

                    bar = get_last_minute_bar(sym)
                    if bar is None:
                        continue

                    o = getattr(bar, "o", None)
                    c = getattr(bar, "c", None)
                    if not o or not c:
                        continue

                    move_pct = (c - o) / o if o else 0.0
                    if move_pct >= ENTRY_PCT:
                        qty = compute_qty(c)
                        if qty <= 0:
                            logging.info(f"[SKIP] {sym} qty<=0 at price {c}")
                            continue

                        logging.info(f"[BUY] {sym} qty={qty} @ {c:.4f} (move={move_pct:.4%})")

                        # نُرسل أمر شراء مع Bracket (OCO) لربط TP و SL بدون تعارض
                        # نحتاج سعر الدخول المرجعي لحساب TP/SL بشكل تقريبي
                        # سنستخدم سعر الإغلاق الأخير كمرجع، والوسيط يربط الأوامر تلقائياً
                        tp_price = tick_round(c * (1.0 + TAKE_PROFIT_PCT))
                        sl_price = tick_round(max(0.01, c - STOP_LOSS_DOLLAR))

                        try:
                            api.submit_order(
                                symbol=sym,
                                qty=qty,
                                side="buy",
                                type="market",
                                time_in_force="day",
                                order_class="bracket",
                                take_profit={"limit_price": tp_price},
                                stop_loss={"stop_price": sl_price}
                            )
                            logging.info(f"[BRACKET] {sym} TP={tp_price} SL={sl_price}")
                        except Exception as e:
                            logging.error(f"[BUY/BRACKET] Submit error for {sym}: {e}")

                except Exception as e:
                    logging.error(f"[RTH] Scan error for {sym}: {e}")

            time.sleep(5)

        else:
            # Market closed — Heartbeat
            if HEARTBEAT_SECS > 0 and (time.time() - last_heartbeat) >= HEARTBEAT_SECS:
                try:
                    next_open = api.get_clock().next_open
                except Exception:
                    next_open = None
                logging.info(f"[HB] Market closed. Next open: {next_open}. Bot alive.")
                last_heartbeat = time.time()
            time.sleep(5)

    except Exception as e:
        logging.error(f"[MAIN] Loop error: {e}")
        time.sleep(10)
