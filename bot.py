import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime, timedelta, timezone

from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# Environment / API client
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,AMZN").split(",") if s.strip()]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Risk / Sizing Parameters
# =========================
FIXED_DOLLARS_PER_TRADE = 1000.0   # fixed budget per trade
RISK_PCT_OF_BP          = 0.01     # cap = min( fixed, 1% of buying power )
MAX_SHARES_PER_TRADE    = 10       # never buy more than this many shares
MAX_PRICE_PER_SHARE     = 600.0    # skip very expensive tickers
DAILY_MAX_SPEND         = 5000.0   # daily spend cap
LOOP_SLEEP_SECONDS      = 30       # faster loop for day trading

# Entry Filters
MOMENTUM_THRESHOLD      = 0.003    # 0.3% last 1-min momentum
VOLUME_SPIKE_MULT       = 1.2      # last 1-min vol >= 1.2x avg prev 5
SPREAD_CENTS_LIMIT      = 0.05     # pass if spread <= 5 cents
SPREAD_PCT_LIMIT        = 0.002    # OR spread <= 0.2% of price

# Exits (autonomous)
TAKE_PROFIT_PCT         = 0.04     # +4% TP (bracket)
STOP_LOSS_PCT           = 0.02     # -2% SL (bracket)
USE_TRAILING_STOP       = True     # optional extra protection
TRAIL_PCT               = 0.015    # 1.5% trailing stop (managed manually)
MAX_HOLD_MINUTES        = 25       # time-based exit
FLATTEN_BEFORE_CLOSE_MIN= 5        # minutes before close to flatten

# =========================
# Data Container
# =========================
@dataclass
class Snapshot:
    last_price: float
    bid: float
    ask: float
    vwap: Optional[float]
    min1_change_pct: Optional[float]
    high_5m: Optional[float]
    vol_1m: Optional[float]
    vol_5m_avg: Optional[float]

# =========================
# Basic Helpers
# =========================
def market_is_open() -> bool:
    try:
        clock = api.get_clock()
        return bool(getattr(clock, "is_open", False))
    except Exception as e:
        logging.warning(f"clock check failed: {e}")
        return True  # permissive on paper

def minutes_to_close() -> Optional[int]:
    try:
        clock = api.get_clock()
        if not clock or not clock.is_open:
            return None
        now = datetime.now(timezone.utc)
        close_ts = getattr(clock, "next_close", None)
        if not close_ts:
            return None
        # `next_close` may be string or datetime depending on lib version
        if isinstance(close_ts, str):
            close_dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
        else:
            close_dt = close_ts
        delta = (close_dt - now).total_seconds() / 60.0
        return int(delta)
    except Exception as e:
        logging.warning(f"minutes_to_close failed: {e}")
        return None

def compute_vwap_from_last5(symbol: str) -> Optional[float]:
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=6)
        if not bars or len(bars) < 6:
            return None
        prev5 = bars[-6:-1]
        vol_sum = 0.0
        tpv_sum = 0.0
        for b in prev5:
            h = float(b.h); l = float(b.l); c = float(b.c); v = float(b.v)
            tp = (h + l + c) / 3.0
            tpv_sum += tp * v
            vol_sum += v
        if vol_sum <= 0:
            return None
        return tpv_sum / vol_sum
    except Exception as e:
        logging.warning(f"VWAP calc failed for {symbol}: {e}")
        return None

def compute_qty(last_price: float, today_spend: float) -> int:
    try:
        account = api.get_account()
        buying_power = float(account.buying_power)
    except Exception as e:
        logging.error(f"Failed to get account: {e}")
        return 0

    if last_price <= 0 or last_price > MAX_PRICE_PER_SHARE:
        return 0

    cap_by_fixed = FIXED_DOLLARS_PER_TRADE
    cap_by_pct   = buying_power * RISK_PCT_OF_BP
    dollars_cap  = min(cap_by_fixed, cap_by_pct)

    remaining_daily = max(DAILY_MAX_SPEND - today_spend, 0.0)
    dollars_cap = min(dollars_cap, remaining_daily)

    qty = int(dollars_cap // last_price)
    qty = max(min(qty, MAX_SHARES_PER_TRADE), 0)
    return qty

def spread_ok(bid: float, ask: float, price: float) -> bool:
    if bid <= 0 or ask <= 0 or ask < bid or price <= 0:
        return False
    spread = ask - bid
    return (spread <= SPREAD_CENTS_LIMIT) or (spread / price <= SPREAD_PCT_LIMIT)

def should_buy(s: Snapshot) -> bool:
    if s.last_price <= 0:
        return False
    if s.vwap is not None and s.last_price < s.vwap:
        return False
    if s.min1_change_pct is None or s.min1_change_pct < MOMENTUM_THRESHOLD:
        return False
    if s.high_5m is None or s.last_price < s.high_5m:
        return False
    if not spread_ok(s.bid, s.ask, s.last_price):
        return False
    if s.vol_1m is None or s.vol_5m_avg is None:
        return False
    if s.vol_1m < VOLUME_SPIKE_MULT * s.vol_5m_avg:
        return False
    return True

def fetch_snapshot(symbol: str) -> Optional[Snapshot]:
    try:
        last_trade = api.get_latest_trade(symbol)
        quote      = api.get_latest_quote(symbol)
        bars_1m    = api.get_bars(symbol, TimeFrame.Minute, limit=6)

        last_price = float(last_trade.price)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)

        min1_change_pct = None
        if bars_1m and len(bars_1m) >= 2:
            p_now  = float(bars_1m[-1].c)
            p_prev = float(bars_1m[-2].c)
            if p_prev > 0:
                min1_change_pct = (p_now - p_prev) / p_prev

        high_5m = None
        if bars_1m and len(bars_1m) >= 6:
            high_5m = max(float(b.h) for b in bars_1m[-6:-1])

        vol_1m = None
        vol_5m_avg = None
        if bars_1m and len(bars_1m) >= 6:
            vol_1m = float(bars_1m[-1].v)
            prev5  = [float(b.v) for b in bars_1m[-6:-1]]
            vol_5m_avg = (sum(prev5) / len(prev5)) if prev5 else None

        vwap_value = compute_vwap_from_last5(symbol)

        return Snapshot(
            last_price=last_price,
            bid=bid,
            ask=ask,
            vwap=vwap_value,
            min1_change_pct=min1_change_pct,
            high_5m=high_5m,
            vol_1m=vol_1m,
            vol_5m_avg=vol_5m_avg,
        )
    except Exception as e:
        logging.warning(f"fetch_snapshot failed for {symbol}: {e}")
        return None

# =========================
# Position State (for exits)
# =========================
# We mirror minimal state for manual trailing/time exits
positions_state: Dict[str, Dict] = {}  # symbol -> {entry_price, entry_time, high_water}

def update_position_state(symbol: str, entry_price: float):
    positions_state[symbol] = {
        "entry_price": entry_price,
        "entry_time": datetime.now(timezone.utc),
        "high_water": entry_price
    }

def handle_trailing_and_time_exit(symbol: str, last_price: float):
    """
    If using manual trailing/time exits in addition to bracket:
    - Update high water
    - If drawdown from high_water >= TRAIL_PCT -> close
    - If hold time > MAX_HOLD_MINUTES -> close
    Note: If the bracket TP/SL already filled, position will be gone; this is a safety layer.
    """
    st = positions_state.get(symbol)
    if not st:
        return

    # Update high water
    if last_price > st["high_water"]:
        st["high_water"] = last_price

    # Time stop
    age_min = (datetime.now(timezone.utc) - st["entry_time"]).total_seconds() / 60.0
    if age_min >= MAX_HOLD_MINUTES:
        close_position(symbol, reason=f"time_stop {age_min:.1f}m")
        return

    # Trailing stop
    if USE_TRAILING_STOP and st["high_water"] > 0:
        dd = (st["high_water"] - last_price) / st["high_water"]
        if dd >= TRAIL_PCT:
            close_position(symbol, reason=f"trailing_stop {dd:.3f}")
            return

def close_position(symbol: str, reason: str = ""):
    try:
        # Cancel open orders for this symbol first (e.g., bracket legs)
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
        # Market close the position
        api.close_position(symbol)
        logging.info(f"CLOSE {symbol} (reason={reason})")
        positions_state.pop(symbol, None)
    except Exception as e:
        logging.exception(f"close_position failed for {symbol}: {e}")

def already_in_position(symbol: str) -> bool:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty) != 0.0
    except Exception:
        return False

def place_bracket_buy(symbol: str, qty: int, last_price: float):
    return api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="day",
        order_class="bracket",
        stop_loss={"stop_price": round(last_price * (1 - STOP_LOSS_PCT), 2)},
        take_profit={"limit_price": round(last_price * (1 + TAKE_PROFIT_PCT), 2)}
    )

# =========================
# Trading Routines
# =========================
def trade_symbol(symbol: str, today_spend: float) -> float:
    # End-of-day flatten safety
    mtc = minutes_to_close()
    if mtc is not None and mtc <= FLATTEN_BEFORE_CLOSE_MIN:
        # Close everything very late
        try:
            api.close_all_positions(cancel_orders=True)
            logging.info("Flattened all positions before close.")
        except Exception as e:
            logging.warning(f"flatten before close failed: {e}")
        return 0.0

    snap = fetch_snapshot(symbol)
    if not snap:
        logging.info(f"NO_DATA {symbol}")
        return 0.0

    # If in position, manage exits (trailing/time)
    if already_in_position(symbol):
        handle_trailing_and_time_exit(symbol, snap.last_price)
        return 0.0

    # Entry logic
    if should_buy(snap):
        qty = compute_qty(snap.last_price, today_spend)
        if qty >= 1:
            try:
                order = place_bracket_buy(symbol, qty, snap.last_price)
                est_val = qty * snap.last_price
                update_position_state(symbol, snap.last_price)
                logging.info(
                    f"BUY {symbol} qty={qty} @~{snap.last_price:.2f} "
                    f"value≈${est_val:.2f} TP={snap.last_price*(1+TAKE_PROFIT_PCT):.2f} "
                    f"SL={snap.last_price*(1-STOP_LOSS_PCT):.2f} order_id={order.id}"
                )
                return est_val
            except Exception as e:
                logging.exception(f"submit_order failed for {symbol}: {e}")
                return 0.0
        else:
            logging.info(f"SKIP {symbol}: qty<1 (price={snap.last_price:.2f})")
            return 0.0
    else:
        logging.info(f"NO_ENTRY {symbol}: filters not satisfied")
        return 0.0

def run():
    logging.info("Starting day-trading bot (auto buy/sell)...")
    today_date = None
    today_spend = 0.0

    while True:
        try:
            now_date = time.strftime("%Y-%m-%d")
            if today_date != now_date:
                today_date = now_date
                today_spend = 0.0
                positions_state.clear()
                logging.info(f"New trading day: {today_date}. Reset daily state.")

            if market_is_open():
                for sym in SYMBOLS:
                    added = trade_symbol(sym, today_spend)
                    today_spend += added
            else:
                logging.info("Market closed. Waiting...")

        except Exception as e:
            logging.exception(f"Run loop error: {e}")

        time.sleep(LOOP_SLEEP_SECONDS)

if __name__ == "__main__":
    run()
