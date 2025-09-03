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
# Regular session (RTH) entry threshold based on 1-min candle move
ENTRY_PCT        = float(os.getenv("ENTRY_PCT", "0.003"))        # 0.3%
FIXED_DPT        = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "500"))   # dollars per trade
STOP_LOSS_DOLLAR = float(os.getenv("STOP_LOSS_DOLLARS", "27"))          # dollar stop per position
TAKE_PROFIT_PCT  = float(os.getenv("TAKE_PROFIT_PCT", "0.006"))         # 0.6% take profit
DAILY_TARGET     = float(os.getenv("DAILY_TARGET", "133.33"))           # printed only

# Optional heartbeat (0 disables)
HEARTBEAT_SECS   = int(os.getenv("HEARTBEAT_SECS", "0"))

# =========================
# Helpers
# =========================
def compute_qty(last_price: float) -> int:
    """Position size as fixed dollars per trade."""
    if last_price <= 0:
        return 0
    return int(max(1, FIXED_DPT // last_price))

def safe_sell(symbol: str, *, limit_price: float | None = None, stop_price: float | None = None):
    """
    Sell only if a position with qty > 0 exists. Works for market, limit, or stop.
    Returns the order object or None.
    """
    try:
        pos = api.get_position(symbol)
        qty = int(float(pos.qty))
    except Exception:
        logging.info(f"[SELL] No open position for {symbol}")
        return None

    if qty <= 0:
        logging.info(f"[SELL] Qty <= 0 for {symbol}, skip")
        return None

    order_args = dict(
        symbol=symbol,
        qty=qty,
        side="sell",
        time_in_force="day",
    )
    if limit_price is not None:
        order_args.update(type="limit", limit_price=round(float(limit_price), 4))
    elif stop_price is not None:
        order_args.update(type="stop", stop_price=round(float(stop_price), 4))
    else:
        order_args.update(type="market")

    logging.info(f"[SELL] {symbol} qty={qty} type={order_args['type']} "
                 f"limit={order_args.get('limit_price')} stop={order_args.get('stop_price')}")
    try:
        return api.submit_order(**order_args)
    except Exception as e:
        logging.error(f"[SELL] Submit error for {symbol}: {e}")
        return None

def place_exit_orders(symbol: str, avg_fill_price: float):
    """
    Place separate TP (limit) and SL (stop) orders. Not a true OCO, but simple and robust.
    """
    tp_price = round(avg_fill_price * (1 + TAKE_PROFIT_PCT), 4)
    sl_price = round(max(0.01, avg_fill_price - STOP_LOSS_DOLLAR), 4)
    safe_sell(symbol, limit_price=tp_price)
    safe_sell(symbol, stop_price=sl_price)
    logging.info(f"[EXIT] TP={tp_price} SL={sl_price} for {symbol}")

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
                    bars = api.get_bars(sym, TimeFrame.Minute, limit=1, feed="sip")
                    if not bars:
                        continue
                    bar = bars[0]
                    if not getattr(bar, "o", None) or not getattr(bar, "c", None):
                        continue

                    move_pct = (bar.c - bar.o) / bar.o if bar.o else 0.0

                    if move_pct >= ENTRY_PCT:
                        qty = compute_qty(bar.c)
                        if qty <= 0:
                            continue
                        logging.info(f"[BUY] {sym} qty={qty} @ {bar.c:.4f} (move={move_pct:.4%})")
                        try:
                            api.submit_order(
                                symbol=sym,
                                qty=qty,
                                side="buy",
                                type="market",
                                time_in_force="day",
                            )
                            # small pause then fetch avg entry and place exits
                            time.sleep(1)
                            try:
                                pos = api.get_position(sym)
                                avg = float(pos.avg_entry_price)
                                place_exit_orders(sym, avg)
                            except Exception as e:
                                logging.error(f"[EXIT] Could not place exits for {sym}: {e}")
                        except Exception as e:
                            logging.error(f"[BUY] Submit error for {sym}: {e}")
                except Exception as e:
                    logging.error(f"[RTH] Scan error for {sym}: {e}")

            time.sleep(5)

        else:
            # Market closed
            if HEARTBEAT_SECS > 0 and (time.time() - last_heartbeat) >= HEARTBEAT_SECS:
                # Print a lightweight "alive" message while waiting
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
