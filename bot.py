import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone
from statistics import mean

from alpaca_trade_api.rest import REST, TimeFrame

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

# Universe: large/liquid names
SYMBOLS: List[str] = [
    "AAPL","MSFT","AMZN","GOOGL","GOOG","NVDA","META","TSLA","BRK.B","JPM",
    "JNJ","V","UNH","PG","XOM","MA","AVGO","HD","MRK","PEP",
    "COST","KO","ABBV","ADBE","NFLX","CRM","CSCO","WMT","TMO","ORCL",
    "BAC","MCD","ASML","AMD","ACN","LIN","CMCSA","ABT","DHR","QCOM",
    "TXN","NKE","AMAT","IBM","INTC","CAT","GE","LLY","MS","PM"
]

if not API_KEY or not API_SECRET:
    logging.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, base_url=BASE_URL)

# =========================
# RISK / SIZING
# =========================
FIXED_DOLLARS_PER_TRADE = float(os.getenv("FIXED_DOLLARS_PER_TRADE", "1000"))
RISK_PCT_OF_BP          = float(os.getenv("RISK_PCT_OF_BP", "0.01"))
MAX_SHARES_PER_TRADE    = int(os.getenv("MAX_SHARES_PER_TRADE", "10"))
MAX_PRICE_PER_SHARE     = float(os.getenv("MAX_PRICE_PER_SHARE", "600"))
DAILY_MAX_SPEND         = float(os.getenv("DAILY_MAX_SPEND", "5000"))
LOOP_SLEEP_SECONDS      = int(os.getenv("LOOP_SLEEP_SECONDS", "30"))

# =========================
# LITE FILTERS (flags)
# =========================
# Core lite filters (always on):
DAILY_CHANGE_MIN_PCT    = float(os.getenv("DAILY_CHANGE_MIN_PCT", "0.02"))  # +2%
SPREAD_CENTS_LIMIT      = float(os.getenv("SPREAD_CENTS_LIMIT", "0.05"))
SPREAD_PCT_LIMIT        = float(os.getenv("SPREAD_PCT_LIMIT", "0.002"))

# Optional filters (OFF by default for lite mode)
ENABLE_VWAP             = os.getenv("ENABLE_VWAP", "false").lower() == "true"
ENABLE_MOMENTUM         = os.getenv("ENABLE_MOMENTUM", "false").lower() == "true"
ENABLE_VOLUME_SPIKE     = os.getenv("ENABLE_VOLUME_SPIKE", "false").lower() == "true"
ENABLE_5M_BREAK         = os.getenv("ENABLE_5M_BREAK", "false").lower() == "true"

MOMENTUM_THRESHOLD      = float(os.getenv("MOMENTUM_THRESHOLD", "0.001"))  # +0.1% if enabled
VOLUME_SPIKE_MULT       = float(os.getenv("VOLUME_SPIKE_MULT", "1.0"))     # 1.0x if enabled

# =========================
# EXITS
# =========================
TAKE_PROFIT_PCT         = float(os.getenv("TAKE_PROFIT_PCT", "0.04"))  # +4% TP
MAX_HOLD_MINUTES        = int(os.getenv("MAX_HOLD_MINUTES", "25"))
FLATTEN_BEFORE_CLOSE_MIN= int(os.getenv("FLATTEN_BEFORE_CLOSE_MIN", "5"))

# =========================
# Data container
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
        clock = api.get_clock()
        return bool(getattr(clock, "is_open", False))
    except Exception as e:
        logging.warning(f"clock check failed: {e}")
        return True

def minutes_to_close() -> Optional[int]:
    try:
        clock = api.get_clock()
        if not clock or not clock.is_open:
            return None
        now = datetime.now(timezone.utc)
        close_ts = getattr(clock, "next_close", None)
        if not close_ts:
            return None
        close_dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00")) if isinstance(close_ts, str) else close_ts
        return int((close_dt - now).total_seconds() / 60.0)
    except Exception as e:
        logging.warning(f"minutes_to_close failed: {e}")
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
    dollars_cap  = min(FIXED_DOLLARS_PER_TRADE, buying_power * RISK_PCT_OF_BP)
    dollars_cap  = min(dollars_cap, max(DAILY_MAX_SPEND - today_spend, 0.0))
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
        vol_sum = 0.0
        tpv_sum = 0.0
        for b in prev5:
            tp = (float(b.h) + float(b.l) + float(b.c)) / 3.0
            tpv_sum += tp * float(b.v)
            vol_sum += float(b.v)
        if vol_sum <= 0:
            return None
        return tpv_sum / vol_sum
    except Exception as e:
        logging.warning(f"VWAP calc failed for {symbol}: {e}")
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
            prev5  = [float(b.v) for b in bars_1m[-6:-1]]
            vol_5m_avg = (sum(prev5) / len(prev5)) if prev5 else None

        vwap_value = compute_vwap_from_last5(symbol)

        prev_close = None
        daily_change_pct = None
        try:
            bars_day = api.get_bars(symbol, TimeFrame.Day, limit=2)
            if bars_day and len(bars_day) >= 2:
                prev_close = float(bars_day[-2].c)
                if prev_close > 0:
                    daily_change_pct = (last_price - prev_close) / prev_close
        except Exception as e:
            logging.warning(f"daily change calc failed for {symbol}: {e}")

        return Snapshot(
            symbol=symbol,
            last_price=last_price,
            bid=bid,
            ask=ask,
            vwap=vwap_value,
            min1_change_pct=min1_change_pct,
            high_5m=high_5m,
            vol_1m=vol_1m,
            vol_5m_avg=vol_5m_avg,
            prev_close=prev_close,
            daily_change_pct=daily_change_pct
        )
    except Exception as e:
        logging.warning(f"fetch_snapshot failed for {symbol}: {e}")
        return None

# =========================
# Diagnostics helpers
# =========================
def fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:.2f}%" if x is not None else "None"

def fmt_price(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "None"

def fmt_ratio(x: Optional[float]) -> str:
    return f"{x:.2f}x" if x is not None else "None"

# =========================
# Entry decision (LITE)
# =========================
def should_buy(s: Snapshot) -> Tuple[bool, str]:
    vwap_ok = (s.vwap is None) or (s.last_price >= s.vwap)
    vol_ratio = None
    if s.vol_1m is not None and s.vol_5m_avg not in (None, 0):
        vol_ratio = s.vol_1m / s.vol_5m_avg
    spread_ok_flag = spread_ok(s.bid, s.ask, s.last_price)

    logging.info(
        f"CHK {s.symbol} | day={fmt_pct(s.daily_change_pct)} "
        f"| 1m={fmt_pct(s.min1_change_pct)} "
        f"| vwap_ok={vwap_ok} "
        f"| 5m_high={fmt_price(s.high_5m)}<=curr={fmt_price(s.last_price)} "
        f"| vol_ratio={fmt_ratio(vol_ratio)} "
        f"| spread_ok={spread_ok_flag}"
    )

    if s.last_price <= 0:
        return False, "bad_price"

    # LITE: do NOT reject if daily is None; only reject when present and below threshold
    if (s.daily_change_pct is not None) and (s.daily_change_pct < DAILY_CHANGE_MIN_PCT):
        return False, "daily_change_below_threshold"

    # LITE: spread must be reasonable to avoid bad fills
    if not spread_ok_flag:
        return False, "bad_spread"

    # OPTIONAL filters (off by default)
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
# Dynamic SL & Trailing
# =========================
def compute_dynamic_levels(symbol: str, last_price: float) -> Dict[str, float]:
    try:
        bars = api.get_bars(symbol, TimeFrame.Minute, limit=11)
        if not bars or len(bars) < 2:
            return {"stop_loss": round(last_price * 0.98, 2), "trailing": 0.015}
        ranges = [float(b.h) - float(b.l) for b in bars[-10:]]
        avg_range = mean(ranges) if ranges else last_price * 0.01
        stop_loss_price = round(last_price - (avg_range * 1.5), 2)
        trailing_pct    = max(min(avg_range / last_price, 0.05), 0.005)
        return {"stop_loss": stop_loss_price, "trailing": trailing_pct}
    except Exception as e:
        logging.warning(f"dynamic levels failed for {symbol}: {e}")
        return {"stop_loss": round(last_price * 0.98, 2), "trailing": 0.015}

# =========================
# Position state / exits
# =========================
positions_state: Dict[str, Dict] = {}

def update_position_state(symbol: str, entry_price: float):
    positions_state[symbol] = {
        "entry_price": entry_price,
        "entry_time": datetime.now(timezone.utc),
        "high_water": entry_price
    }

def handle_trailing_and_time_exit(symbol: str, last_price: float):
    st = positions_state.get(symbol)
    if not st:
        return
    if last_price > st["high_water"]:
        st["high_water"] = last_price
    age_min = (datetime.now(timezone.utc) - st["entry_time"]).total_seconds() / 60.0
    if age_min >= MAX_HOLD_MINUTES:
        close_position(symbol, reason=f"time_stop {age_min:.1f}m")
        return
    dyn = compute_dynamic_levels(symbol, last_price)
    if st["high_water"] > 0:
        drawdown = (st["high_water"] - last_price) / st["high_water"]
        if drawdown >= dyn["trailing"]:
            close_position(symbol, reason=f"trailing_stop {drawdown:.3f}")

def close_position(symbol: str, reason: str = ""):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
        api.close_position(symbol)
        logging.info(f"CLOSE {symbol} (reason={reason})")
        positions_state.pop(symbol, None)
    except Exception as e:
        logging.exception(f"close_position failed for {symbol}: {e}")

def already_in_position(symbol: str) -> bool:
    try:
        pos = api.get_position(symbol)
        in_pos = float(pos.qty) != 0.0
        if not in_pos:
            positions_state.pop(symbol, None)
        return in_pos
    except Exception:
        positions_state.pop(symbol, None)
        return False

def place_bracket_buy(symbol: str, qty: int, last_price: float):
    dyn = compute_dynamic_levels(symbol, last_price)
    stop_price = dyn["stop_loss"]
    tp_price   = round(last_price * (1 + TAKE_PROFIT_PCT), 2)
    order = api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="market",
        time_in_force="day",
        order_class="bracket",
        stop_loss={"stop_price": stop_price},
        take_profit={"limit_price": tp_price},
    )
    logging.info(f"BUY {symbol} qty={qty} @~{last_price:.2f} TP={tp_price:.2f} SL={stop_price:.2f}")
    return order

# =========================
# Trading routines
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

    if already_in_position(symbol):
        handle_trailing_and_time_exit(symbol, snap.last_price)
        return 0.0

    ok, reason = should_buy(snap)
    if ok:
        qty = compute_qty(snap.last_price, today_spend)
        if qty >= 1:
            try:
                place_bracket_buy(symbol, qty, snap.last_price)
                est_val = qty * snap.last_price
                update_position_state(symbol, snap.last_price)
                return est_val
            except Exception as e:
                logging.exception(f"submit_order failed for {symbol}: {e}")
                return 0.0
        else:
            logging.info(f"SKIP {symbol}: qty<1 (price={snap.last_price:.2f})")
            return 0.0
    else:
        logging.info(
            f"NO_ENTRY {symbol}: {reason} "
            f"| day={fmt_pct(snap.daily_change_pct)} "
            f"| 1m={fmt_pct(snap.min1_change_pct)} "
            f"| vwap={fmt_price(snap.vwap)} "
            f"| 5m_high={fmt_price(snap.high_5m)} "
            f"| vol1m={snap.vol_1m if snap.vol_1m is not None else 'None'} "
            f"| vol5mAvg={snap.vol_5m_avg if snap.vol_5m_avg is not None else 'None'} "
            f"| spread_ok={spread_ok(snap.bid, snap.ask, snap.last_price)}"
        )
        return 0.0

# =========================
# Main loop
# =========================
def run():
    logging.info("Starting LITE day-trading bot (daily% + spread; optional filters off by default)...")
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
