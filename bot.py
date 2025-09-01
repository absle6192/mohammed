import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timedelta, timezone
from alpaca_trade_api.rest import REST, TimeFrame

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# =========================
# Hardcoded API Credentials (Paper)
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# Quick sanity print
print("BASE_URL =", BASE_URL)
print("KEY_PREFIX =", API_KEY[:6], "SECRET_LEN =", len(API_SECRET))

# =========================
# Symbols universe
# =========================
SYMBOLS = ["AAPL","MSFT","NVDA","TSLA","AMZN","NFLX","BA","MU","PLTR","SMCI"]

# =========================
# Risk / Sizing
# =========================
RISK_PCT_OF_BP          = 0.01   # 1% of buying power per trade
FIXED_DOLLARS_PER_TRADE = 0      # 0 = disabled
DAILY_MAX_SPEND         = 0      # 0 = disabled
MAX_SHARES_PER_TRADE    = 999999
MAX_PRICE_PER_SHARE     = 600
LOOP_SLEEP_SECONDS      = 5      # fast loop

# =========================
# Entry Filters (technical)
# =========================
MOMENTUM_THRESHOLD = 0.003      # +0.3% last 1-min momentum
VOLUME_SPIKE_MULT  = 1.2        # 1-min vol >= 1.2x avg previous 5 bars
SPREAD_CENTS_LIMIT = 0.05       # pass if spread <= 5 cents
SPREAD_PCT_LIMIT   = 0.002      # OR spread <= 0.2% of price

# =========================
# Exits (per trade)
# =========================
TAKE_PROFIT_PCT          = 0.01   # +1%
STOP_LOSS_PCT            = 0.01   # -1%
USE_TRAILING_STOP        = True
TRAIL_PCT                = 0.015  # 1.5% drop from high-water after entry
MAX_HOLD_MINUTES         = 25
FLATTEN_BEFORE_CLOSE_MIN = 5

# =========================
# Daily P&L limits (auto halt/flatten)
# =========================
DAILY_TARGET_SAR   = 500
SAR_PER_USD        = 3.75
DAILY_TARGET_USD   = 0         # 0 -> derive from SAR
DAILY_MAX_LOSS_USD = 27        # ≈ 100 SAR
HALT_AFTER_TARGET  = True

# =========================
# News (disabled for now)
# =========================
NEWS_ENABLED  = False
NEWS_REQUIRED = False

# =========================
# API client
# =========================
api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Daily P&L tracking
# =========================
_session_open_equity: Optional[float] = None
_halt_trading_today  = False

def _daily_target_usd() -> float:
    return DAILY_TARGET_USD if DAILY_TARGET_USD > 0 else (DAILY_TARGET_SAR / SAR_PER_USD)

def _ensure_open_equity():
    global _session_open_equity
    if _session_open_equity is None:
        acct = api.get_account()
        _session_open_equity = float(acct.equity)
        logging.info(f"session_open_equity={_session_open_equity:.2f}")

def _pnl_today_usd() -> float:
    acct = api.get_account()
    base = _session_open_equity if _session_open_equity is not None else float(acct.equity)
    return float(acct.equity) - base

def close_all_positions_now(reason: str):
    try:
        api.close_all_positions(cancel_orders=True)
        logging.info(f"Flattened all positions. reason={reason}")
    except Exception as e:
        logging.warning(f"close_all_positions failed: {e}")

def enforce_daily_limits() -> bool:
    """
    Returns True if trading should halt (and closes all positions) because:
      - Daily max loss hit, or
      - Daily target reached (and HALT_AFTER_TARGET=True)
    """
    global _halt_trading_today
    if _halt_trading_today:
        return True

    _ensure_open_equity()
    pnl = _pnl_today_usd()
    tgt = _daily_target_usd()

    if DAILY_MAX_LOSS_USD > 0 and pnl <= -abs(DAILY_MAX_LOSS_USD):
        _halt_trading_today = True
        logging.info(f"[HALT] Daily max loss hit: pnl={pnl:.2f} <= -{DAILY_MAX_LOSS_USD:.2f}")
        close_all_positions_now("daily_max_loss")
        return True

    if tgt > 0 and pnl >= abs(tgt):
        logging.info(f"[REACHED] Daily target: pnl={pnl:.2f} >= {tgt:.2f}")
        if HALT_AFTER_TARGET:
            _halt_trading_today = True
            close_all_positions_now("daily_target")
            return True

    return _halt_trading_today

# =========================
# Data container
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
# Market / helpers
# =========================
def market_is_open() -> bool:
    try:
        clock = api.get_clock()
        return bool(getattr(clock, "is_open", False))
    except Exception as e:
        logging.warning(f"clock check failed: {e}")
        return True  # be permissive on paper

def minutes_to_close() -> Optional[int]:
    try:
        clock = api.get_clock()
        if not clock or not clock.is_open:
            return None
        now = datetime.now(timezone.utc)
        close_ts = getattr(clock, "next_close", None)
        if not close_ts:
            return None
        close_dt = datetime.fromisoformat(close_ts.replace("Z","+00:00")) if isinstance(close_ts, str) else close_ts
        return int((close_dt - now).total_seconds() / 60.0)
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

    dollars_cap = buying_power * RISK_PCT_OF_BP
    if FIXED_DOLLARS_PER_TRADE > 0:
        dollars_cap = min(dollars_cap, FIXED_DOLLARS_PER_TRADE)
    if DAILY_MAX_SPEND > 0:
        remaining_daily = max(DAILY_MAX_SPEND - today_spend, 0.0)
        dollars_cap = min(dollars_cap, remaining_daily)

    qty = int(dollars_cap // last_price)
    return max(min(qty, MAX_SHARES_PER_TRADE), 0)

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

        # 1-min momentum
        min1_change_pct = None
        if bars_1m and len(bars_1m) >= 2:
            p_now  = float(bars_1m[-1].c)
            p_prev = float(bars_1m[-2].c)
            if p_prev > 0:
                min1_change_pct = (p_now - p_prev) / p_prev

        # 5-minute high from last 5 bars (excluding current)
        high_5m = None
        if bars_1m and len(bars_1m) >= 6:
            high_5m = max(float(b.h) for b in bars_1m[-6:-1])

        # volume spike
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
# Position State (for manual trailing/time exits)
# =========================
positions_state: Dict[str, Dict] = {}  # symbol -> {entry_price, entry_time, high_water}

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

    # update high-water
    if last_price > st["high_water"]:
        st["high_water"] = last_price

    # time stop
    age_min = (datetime.now(timezone.utc) - st["entry_time"]).total_seconds() / 60.0
    if age_min >= MAX_HOLD_MINUTES:
        close_position(symbol, reason=f"time_stop {age_min:.1f}m")
        return

    # trailing stop
    if USE_TRAILING_STOP and st["high_water"] > 0:
        dd = (st["high_water"] - last_price) / st["high_water"]
        if dd >= TRAIL_PCT:
            close_position(symbol, reason=f"trailing_stop {dd:.3f}")
            return

def close_position(symbol: str, reason: str = ""):
    try:
        # cancel open orders for this symbol (e.g., bracket legs)
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
# Trading routines
# =========================
def trade_symbol(symbol: str, today_spend: float) -> float:
    # End-of-day flatten safety
    mtc = minutes_to_close()
    if mtc is not None and mtc <= FLATTEN_BEFORE_CLOSE_MIN:
        close_all_positions_now("end_of_day")
        return 0.0

    snap = fetch_snapshot(symbol)
    if not snap:
        logging.info(f"NO_DATA {symbol}")
        return 0.0

    # manage existing position
    if already_in_position(symbol):
        handle_trailing_and_time_exit(symbol, snap.last_price)
        return 0.0

    # entry
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
    logging.info("Starting bot (no-news mode, TP/SL 1%, 1% risk, daily limits active)...")
    today_date = None
    today_spend = 0.0
    global _session_open_equity, _halt_trading_today

    while True:
        try:
            now_date = time.strftime("%Y-%m-%d")
            if today_date != now_date:
                today_date = now_date
                today_spend = 0.0
                positions_state.clear()
                _session_open_equity = None
                _halt_trading_today  = False
                logging.info(f"New day: {today_date}")

            if market_is_open():
                # daily limits gate
                if enforce_daily_limits():
                    time.sleep(LOOP_SLEEP_SECONDS)
                    continue

                for sym in SYMBOLS:
                    added = trade_symbol(sym, today_spend)
                    if DAILY_MAX_SPEND > 0:
                        today_spend += added
            else:
                logging.info("Market closed. Waiting...")

        except Exception as e:
            logging.exception(f"Run loop error: {e}")

        time.sleep(LOOP_SLEEP_SECONDS)

if __name__ == "__main__":
    run()
