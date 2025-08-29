import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone
from statistics import mean
from alpaca_trade_api.rest import REST, TimeFrame, APIError

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

# =========================
# API / ENV
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
if not API_KEY or not API_SECRET:
    raise SystemExit("Missing API keys")

api = REST(API_KEY, API_SECRET, base_url=BASE_URL)

# =========================
# Universe (liquid large caps)
# =========================
SYMBOLS: List[str] = [
    "AAPL","MSFT","AMZN","GOOGL","GOOG","NVDA","META","TSLA","BRK.B","JPM",
    "JNJ","V","UNH","PG","XOM","MA","AVGO","HD","MRK","PEP","COST","KO",
    "ABBV","ADBE","NFLX","CRM","CSCO","WMT","TMO","ORCL","BAC","MCD",
    "ASML","AMD","ACN","LIN","CMCSA","ABT","DHR","QCOM","TXN","NKE",
    "AMAT","IBM","INTC","CAT","GE","LLY","MS","PM"
]

# =========================
# Risk / Sizing
# =========================
FIXED_DOLLARS_PER_TRADE = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "1000"))
RISK_PCT_OF_BP          = float(os.getenv("RISK_PCT_OF_BP", "0.01"))
MAX_SHARES_PER_TRADE    = int(os.getenv("MAX_SHARES_PER_TRADE", "10"))
MAX_PRICE_PER_SHARE     = float(os.getenv("MAX_PRICE_PER_SHARE", "600"))
DAILY_MAX_SPEND         = float(os.getenv("DAILY_MAX_SPEND", "5000"))
LOOP_SLEEP_SECONDS      = int(os.getenv("LOOP_SLEEP_SECONDS", "30"))

# =========================
# Lite entry filters
# =========================
DAILY_CHANGE_MIN_PCT    = float(os.getenv("DAILY_CHANGE_MIN_PCT", "0.02"))  # +2%
SPREAD_CENTS_LIMIT      = float(os.getenv("SPREAD_CENTS_LIMIT", "0.05"))
SPREAD_PCT_LIMIT        = float(os.getenv("SPREAD_PCT_LIMIT", "0.002"))

# Optional (off by default)
ENABLE_VWAP         = os.getenv("ENABLE_VWAP", "false").lower() == "true"
ENABLE_MOMENTUM     = os.getenv("ENABLE_MOMENTUM", "false").lower() == "true"
ENABLE_VOLUME_SPIKE = os.getenv("ENABLE_VOLUME_SPIKE", "false").lower() == "true"
ENABLE_5M_BREAK     = os.getenv("ENABLE_5M_BREAK", "false").lower() == "true"

MOMENTUM_THRESHOLD  = float(os.getenv("MOMENTUM_THRESHOLD", "0.001"))  # +0.1% if enabled
VOLUME_SPIKE_MULT   = float(os.getenv("VOLUME_SPIKE_MULT", "1.0"))     # 1.0x if enabled

# =========================
# Exits (Trailing + safety)
# =========================
TRAILING_STOP_PCT        = float(os.getenv("TRAILING_STOP_PCT", "0.02"))  # 2% trailing from high-water
INITIAL_STOP_PCT         = float(os.getenv("INITIAL_STOP_PCT", "0.02"))   # initial protective stop vs entry
MAX_HOLD_MINUTES         = int(os.getenv("MAX_HOLD_MINUTES", "25"))
FLATTEN_BEFORE_CLOSE_MIN = int(os.getenv("FLATTEN_BEFORE_CLOSE_MIN", "5"))

# =========================
# Data types
# =========================
@dataclass
class Snapshot:
    symbol: str
    last_price: float
    bid: float
    ask: float
    vwap: Optional[float]
    min1_change_pct: Optional[float]
    high_5m: Optional[float]
    vol_1m: Optional[float]
    vol_5m_avg: Optional[float]
    prev_close: Optional[float]
    daily_change_pct: Optional[float]

# =========================
# Helpers
# =========================
def market_is_open() -> bool:
    try:
        return bool(getattr(api.get_clock(), "is_open", False))
    except Exception:
        return True

def minutes_to_close() -> Optional[int]:
    try:
        clock = api.get_clock()
        if not clock or not clock.is_open:
            return None
        now = datetime.now(timezone.utc)
        close_ts = getattr(clock, "next_close", None)
        close_dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00")) if isinstance(close_ts, str) else close_ts
        return int((close_dt - now).total_seconds() / 60)
    except Exception:
        return None

def compute_qty(last_price: float, today_spend: float) -> int:
    try:
        bp = float(api.get_account().buying_power)
    except Exception:
        return 0
    if last_price <= 0 or last_price > MAX_PRICE_PER_SHARE:
        return 0
    dollars_cap = min(FIXED_DOLLARS_PER_TRADE, bp * RISK_PCT_OF_BP, max(DAILY_MAX_SPEND - today_spend, 0.0))
    qty = int(dollars_cap // last_price)
    return max(min(qty, MAX_SHARES_PER_TRADE), 0)

def spread_ok(bid: float, ask: float, price: float) -> bool:
    if bid <= 0 or ask <= 0 or ask < bid or price <= 0:
        return False
    spread = ask - bid
    return (spread <= SPREAD_CENTS_LIMIT) or (spread / price <= SPREAD_PCT_LIMIT)

def compute_vwap_from_last5(symbol: str) -> Optional[float]:
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=6)
        if not bars or len(bars) < 6:
            return None
        prev5 = bars[-6:-1]
        tpv = 0.0
        vol = 0.0
        for b in prev5:
            tp = (float(b.h) + float(b.l) + float(b.c)) / 3.0
            v = float(b.v)
            tpv += tp * v
            vol += v
        return tpv / vol if vol > 0 else None
    except Exception:
        return None

def fetch_snapshot(symbol: str) -> Optional[Snapshot]:
    try:
        last_trade = api.get_latest_trade(symbol)
        quote      = api.get_latest_quote(symbol)
        bars_1m    = api.get_bars(symbol, TimeFrame.Minute, limit=6)

        last_price = float(last_trade.price)
        bid = float(getattr(quote, "bid_price", 0) or 0)
        ask = float(getattr(quote, "ask_price", 0) or 0)

        min1_change_pct = None
        high_5m = None
        vol_1m = None
        vol_5m_avg = None

        if bars_1m and len(bars_1m) >= 2:
            p_now  = float(bars_1m[-1].c)
            p_prev = float(bars_1m[-2].c)
            if p_prev > 0:
                min1_change_pct = (p_now - p_prev) / p_prev

        if bars_1m and len(bars_1m) >= 6:
            high_5m = max(float(b.h) for b in bars_1m[-6:-1])
            vol_1m = float(bars_1m[-1].v)
            vol_5m_avg = sum(float(b.v) for b in bars_1m[-6:-1]) / 5.0

        vwap_value = compute_vwap_from_last5(symbol)

        prev_close = None
        daily_change_pct = None
        try:
            bars_day = api.get_bars(symbol, TimeFrame.Day, limit=2)
            if bars_day and len(bars_day) >= 2:
                prev_close = float(bars_day[-2].c)
                if prev_close > 0:
                    daily_change_pct = (last_price - prev_close) / prev_close
        except Exception:
            pass

        return Snapshot(symbol, last_price, bid, ask, vwap_value, min1_change_pct,
                        high_5m, vol_1m, vol_5m_avg, prev_close, daily_change_pct)
    except Exception as e:
        logging.warning(f"fetch_snapshot failed for {symbol}: {e}")
        return None

def fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:.2f}%" if x is not None else "None"

def fmt_price(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "None"

def fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}x" if x is not None else "None"

# =========================
# Entry (Lite)
# =========================
def should_buy(s: Snapshot) -> Tuple[bool, str]:
    vwap_ok = (s.vwap is None) or (s.last_price >= s.vwap)
    vol_ratio = None
    if s.vol_1m is not None and s.vol_5m_avg not in (None, 0):
        vol_ratio = s.vol_1m / s.vol_5m_avg
    spread_flag = spread_ok(s.bid, s.ask, s.last_price)

    logging.info(f"CHK {s.symbol} | day={fmt_pct(s.daily_change_pct)} | 1m={fmt_pct(s.min1_change_pct)} "
                 f"| vwap_ok={vwap_ok} | 5m_high={fmt_price(s.high_5m)}<=curr={fmt_price(s.last_price)} "
                 f"| vol_ratio={fmt_ratio(vol_ratio)} | spread_ok={spread_flag}")

    if s.last_price <= 0:
        return False, "bad_price"

    # Do NOT reject if daily is None; only reject when present and below threshold
    if (s.daily_change_pct is not None) and (s.daily_change_pct < DAILY_CHANGE_MIN_PCT):
        return False, "daily_change_below_threshold"

    if not spread_flag:
        return False, "bad_spread"

    # Optional filters (default OFF)
    if ENABLE_VWAP and not vwap_ok:
        return False, "below_VWAP"
    if ENABLE_5M_BREAK and (s.high_5m is not None) and (s.last_price < s.high_5m):
        return False, "not_breaking_5min_high"
    if ENABLE_MOMENTUM and (s.min1_change_pct is None or s.min1_change_pct < MOMENTUM_THRESHOLD):
        return False, "weak_1min_momentum"
    if ENABLE_VOLUME_SPIKE:
        if s.vol_1m is None or s.vol_5m_avg in (None, 0):
            return False, "volume_data_missing"
        if vol_ratio is None or vol_ratio < VOLUME_SPIKE_MULT:
            return False, "weak_volume_spike"

    return True, "ok"

# =========================
# Trailing stop management
# =========================
positions_state: Dict[str, Dict] = {}  # symbol -> {entry_price, entry_time, high_water, stop_order_id}

def create_or_update_stop(symbol: str, qty: int, desired_stop: float, last_price: float):
    """
    Place or update a server-side SELL stop order at desired_stop.
    Will only move stop UP (never down).
    """
    # Alpaca rule: stop for a long must be below current price
    desired_stop = round(min(desired_stop, last_price - 0.01), 2)

    st = positions_state.get(symbol, {})
    existing_id = st.get("stop_order_id")
    current_stop = st.get("current_stop")

    # Only raise the stop (or create if none)
    if current_stop is None or desired_stop > current_stop:
        # Cancel old stop if any
        if existing_id:
            try:
                api.cancel_order(existing_id)
                logging.info(f"{symbol}: canceled old stop {existing_id} @ {current_stop}")
            except Exception as e:
                logging.warning(f"{symbol}: cancel old stop failed: {e}")

        # Submit new stop
        try:
            o = api.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                type="stop",
                time_in_force="day",
                stop_price=desired_stop
            )
            st["stop_order_id"] = o.id
            st["current_stop"]  = desired_stop
            positions_state[symbol] = st
            logging.info(f"{symbol}: placed/raised stop to {desired_stop:.2f}")
        except APIError as e:
            logging.exception(f"{symbol}: submit stop failed: {e}")
        except Exception as e:
            logging.exception(f"{symbol}: submit stop failed: {e}")

def handle_trailing_and_time_exit(symbol: str, last_price: float, qty: int):
    st = positions_state.get(symbol)
    if not st:
        return

    # Update high-water
    if last_price > st["high_water"]:
        st["high_water"] = last_price

    # Time exit
    age_min = (datetime.now(timezone.utc) - st["entry_time"]).total_seconds() / 60.0
    if age_min >= MAX_HOLD_MINUTES:
        close_position(symbol, f"time_stop {age_min:.1f}m")
        return

    # Trailing: stop follows high_water down by TRAILING_STOP_PCT
    desired_stop = round(st["high_water"] * (1 - TRAILING_STOP_PCT), 2)
    create_or_update_stop(symbol, qty, desired_stop, last_price)

def close_position(symbol: str, reason: str = ""):
    try:
        # Cancel open orders first (stop orders etc.)
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
        # Close market
        api.close_position(symbol)
        logging.info(f"CLOSE {symbol} (reason={reason})")
        positions_state.pop(symbol, None)
    except Exception as e:
        logging.exception(f"close_position failed for {symbol}: {e}")

def already_in_position(symbol: str) -> Tuple[bool, int]:
    try:
        pos = api.get_position(symbol)
        qty = int(float(pos.qty))
        in_pos = qty != 0
        if not in_pos:
            positions_state.pop(symbol, None)
        return in_pos, qty
    except Exception:
        positions_state.pop(symbol, None)
        return False, 0

# =========================
# Order placement (market buy + server-side stop)
# =========================
def place_market_buy_with_initial_stop(symbol: str, qty: int, base_price: float):
    # Market buy
    order = api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="day"
    )
    logging.info(f"BUY {symbol} qty={qty} @~{base_price:.2f}")

    # Initial protective stop (relative to entry)
    initial_stop = round(base_price * (1 - INITIAL_STOP_PCT), 2)
    # Alpaca rule: must be below current price by at least $0.01
    initial_stop = round(min(initial_stop, base_price - 0.01), 2)

    # Create the initial stop server-side
    create_or_update_stop(symbol, qty, initial_stop, base_price)
    return order

# =========================
# Trade routine
# =========================
def trade_symbol(symbol: str, today_spend: float) -> float:
    mtc = minutes_to_close()
    if mtc is not None and mtc <= FLATTEN_BEFORE_CLOSE_MIN:
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

    in_pos, qty = already_in_position(symbol)
    if in_pos:
        # Manage trailing/time using last price
        handle_trailing_and_time_exit(symbol, snap.last_price, qty)
        return 0.0

    ok, reason = should_buy(snap)
    if not ok:
        logging.info(
            f"NO_ENTRY {symbol}: {reason} | day={fmt_pct(snap.daily_change_pct)} "
            f"| 1m={fmt_pct(snap.min1_change_pct)} | vwap={fmt_price(snap.vwap)} "
            f"| 5m_high={fmt_price(snap.high_5m)} | spread_ok={spread_ok(snap.bid, snap.ask, snap.last_price)}"
        )
        return 0.0

    qty_new = compute_qty(snap.last_price, today_spend)
    if qty_new < 1:
        logging.info(f"SKIP {symbol}: qty<1 (price={snap.last_price:.2f})")
        return 0.0

    try:
        place_market_buy_with_initial_stop(symbol, qty_new, snap.last_price)
        positions_state[symbol] = {
            "entry_price": snap.last_price,
            "entry_time": datetime.now(timezone.utc),
            "high_water": snap.last_price,
            "stop_order_id": positions_state.get(symbol, {}).get("stop_order_id"),
            "current_stop": positions_state.get(symbol, {}).get("current_stop"),
        }
        est_val = qty_new * snap.last_price
        return est_val
    except Exception as e:
        logging.exception(f"submit_order failed for {symbol}: {e}")
        return 0.0

def run():
    logging.info("Starting LITE bot (Trailing Stop, no fixed TP)...")
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
                    today_spend += trade_symbol(sym, today_spend)
            else:
                logging.info("Market closed. Waiting...")
        except Exception as e:
            logging.exception(f"Run loop error: {e}")
        time.sleep(LOOP_SLEEP_SECONDS)

if __name__ == "__main__":
    run()
