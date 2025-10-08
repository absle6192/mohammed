import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo  # NEW: for ET session detection

from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bot")

# =========================
# Environment / Config
# =========================
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv(
    "SYMBOLS",
    "TSLA,NVDA,AAPL,MSFT,AMZN,META,GOOGL,AMD"
).split(",") if s.strip()]

# -------- Entry & Protection --------
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.00005"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
TOP_K = int(os.getenv("TOP_K", "2"))

# Allocation
ALLOCATE_FROM_CASH = os.getenv("ALLOCATE_FROM_CASH", "true").lower() == "true"
FALLBACK_NOTIONAL_PER_TRADE = float(os.getenv("NOTIONAL_PER_TRADE", "6250"))

# -------- Trailing Stop --------
TRAIL_PCT   = float(os.getenv("TRAIL_PCT", "0.7"))   # 0.7% trailing
TRAIL_PRICE = float(os.getenv("TRAIL_PRICE", "0.0")) # 0 disables price-based trailing

# -------- Re-entry --------
NO_REENTRY_TODAY = os.getenv("NO_REENTRY_TODAY", "true").lower() == "true"
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))

# -------- Main loop cadence --------
INTERVAL_SECONDS   = int(os.getenv("INTERVAL_SECONDS", "30"))
MAX_CYCLE_SECONDS  = int(os.getenv("MAX_CYCLE_SECONDS", "20"))

# -------- Pre-Market limit padding (USD) --------
PRE_SLIPPAGE_USD = float(os.getenv("PRE_SLIPPAGE_USD", "0.05"))  # small add to BUY limit
SELL_PAD_USD     = float(os.getenv("SELL_PAD_USD", "0.02"))      # how much below Bid for PRE sell

# ===== allow/deny auto-sell in pre-market via ENV =====
ALLOW_PRE_AUTO_SELL = os.getenv("ALLOW_PRE_AUTO_SELL", "false").lower() == "true"

if not API_KEY or not API_SECRET:
    log.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Time helpers & Sessions
# =========================
ET = ZoneInfo("America/New_York")

def utc_now():
    return datetime.now(timezone.utc)

def utc_today():
    return utc_now().date()

def heartbeat(msg: str):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log.info(f"✅ {msg} at {now}Z")

def sleep_until_next_interval(interval_seconds: int, started_at: float):
    elapsed = time.time() - started_at
    sleep_left = max(0.0, interval_seconds - elapsed)
    time.sleep(sleep_left)

def current_session_et(dt: datetime | None = None) -> str:
    """
    Returns: 'pre' | 'regular' | 'closed'
    - pre:     04:00–09:30 ET
    - regular: 09:30–16:00 ET
    - else: closed (After-Hours and overnight both treated as closed for this bot)
    """
    now = (dt or datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    from datetime import time as _t
    PRE_START  = _t(4, 0)
    REG_START  = _t(9, 30)
    REG_END    = _t(16, 0)
    if PRE_START <= t < REG_START:
        return "pre"
    if REG_START <= t < REG_END:
        return "regular"
    return "closed"

def can_trade_now() -> tuple[bool, bool]:
    """
    Returns (should_trade, extended_hours_flag)
    - pre: trade allowed, extended_hours=True, LIMIT-only
    - regular: trade allowed, extended_hours=False
    - closed: not allowed
    """
    s = current_session_et()
    if s == "pre":
        return True, True
    if s == "regular":
        return True, False
    return False, False

# =========================
# Re-entry Registry
# =========================
sold_registry: Dict[str, datetime] = {}

def record_today_sells(api: REST, symbols: List[str]) -> None:
    try:
        closed = api.list_orders(status="closed", limit=200, direction="desc")
    except Exception as e:
        log.warning(f"list_orders failed: {e}")
        return
    for o in closed:
        try:
            if o.side != "sell" or o.symbol not in symbols:
                continue
            if not getattr(o, "filled_at", None):
                continue
            filled_at = o.filled_at if o.filled_at.tzinfo else o.filled_at.replace(tzinfo=timezone.utc)
            if filled_at.date() == utc_today():
                sold_registry[o.symbol] = filled_at
        except Exception:
            continue

def sold_today(symbol: str) -> bool:
    if not NO_REENTRY_TODAY:
        return False
    ts = sold_registry.get(symbol)
    return bool(ts and ts.date() == utc_today())

def in_cooldown(symbol: str) -> bool:
    if COOLDOWN_MINUTES <= 0:
        return False
    ts = sold_registry.get(symbol)
    return bool(ts and utc_now() < ts + timedelta(minutes=COOLDOWN_MINUTES))

# =========================
# Market / Orders helpers
# =========================
def last_trade_price(symbol: str) -> Optional[float]:
    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)
    except Exception as e:
        log.debug(f"last_trade_price error {symbol}: {e}")
        return None

def latest_bid(symbol: str) -> float:
    try:
        q = api.get_latest_quote(symbol)
        return float(getattr(q, "bidprice", 0) or 0)
    except Exception:
        return 0.0

def has_open_position(symbol: str) -> bool:
    try:
        pos = api.get_position(symbol)
        return abs(float(pos.qty)) > 0
    except Exception:
        return False

def list_open_positions_symbols() -> List[str]:
    try:
        return [p.symbol for p in api.list_positions()]
    except Exception:
        return []

def count_open_positions() -> int:
    try:
        return len(api.list_positions())
    except Exception:
        return 0

def open_orders_map() -> Dict[str, bool]:
    m: Dict[str, bool] = {}
    try:
        for o in api.list_orders(status="open"):
            m[o.symbol] = True
    except Exception:
        pass
    return m

def cancel_symbol_open_orders(symbol: str):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                try:
                    api.cancel_order(o.id)
                    log.info(f"Canceled open order: {symbol} ({o.side} {o.type})")
                except Exception as e:
                    log.warning(f"Cancel failed for {symbol}: {e}")
    except Exception:
        pass

# =========================
# Entry Signal (1-min momentum)
# =========================
def momentum_for_last_min(symbol: str) -> Optional[float]:
    """
    Momentum for last minute: (close - open) / open
    """
    try:
        bars = api.get_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), limit=2).df
        if bars.empty:
            return None
        last = bars.iloc[-1]
        if last["open"] <= 0:
            return None
        return float((last["close"] - last["open"]) / last["open"])
    except Exception as e:
        log.debug(f"momentum calc error {symbol}: {e}")
        return None

# =========================
# Guards
# =========================
def guard_states(symbol: str, open_orders: Dict[str, bool]) -> Dict[str, bool]:
    return {
        "has_pos": has_open_position(symbol),
        "has_open_order": open_orders.get(symbol, False),
        "sold_today": sold_today(symbol),
        "cooldown": in_cooldown(symbol),
        "max_positions_reached": count_open_positions() >= MAX_OPEN_POSITIONS
    }

def can_open_new_long(symbol: str, states: Dict[str, bool]) -> Tuple[bool, str]:
    if states["has_pos"]:
        return False, "already have position"
    if states["has_open_order"]:
        return False, "open order exists"
    if states["sold_today"]:
        return False, "no-reentry-today"
    if states["cooldown"]:
        return False, "in cooldown"
    if states["max_positions_reached"]:
        return False, "max positions reached"
    return True, ""

# =========================
# Pre-Market lock (your custom rule)
# =========================
PREMARKET_LOCK: Dict[str, str] = {}

def record_prebuy(symbol: str):
    PREMARKET_LOCK[symbol.upper()] = datetime.now(ET).date().isoformat()

def clear_lock_if_no_position(symbol: str):
    sym = symbol.upper()
    try:
        pos = api.get_position(sym)
        if float(pos.qty) == 0:
            PREMARKET_LOCK.pop(sym, None)
    except Exception:
        PREMARKET_LOCK.pop(sym, None)

def should_allow_auto_sell(symbol: str) -> bool:
    """
    Deny auto-sell if:
    - symbol is locked due to a pre-market buy
    - and current session is still 'pre'
    Allow sell if:
    - session is 'regular' OR
    - symbol not locked OR
    - no position anymore
    - OR ALLOW_PRE_AUTO_SELL=True (override)
    """
    sym = symbol.upper()
    clear_lock_if_no_position(sym)
    session = current_session_et()
    if ALLOW_PRE_AUTO_SELL:
        return True
    if sym in PREMARKET_LOCK and session == "pre":
        return False
    return True

# =========================
# Orders
# =========================
def place_market_buy_qty_regular(symbol: str, qty: int) -> Optional[str]:
    try:
        o = api.submit_order(
            symbol=symbol, side="buy", type="market",
            time_in_force="day", qty=str(qty), extended_hours=False
        )
        log.info(f"[BUY-REG/MKT] {symbol} qty={qty}")
        return o.id
    except Exception as e:
        log.error(f"BUY regular market failed {symbol}: {e}")
        return None

def place_limit_buy_qty_premarket(symbol: str, qty: int, ref_price: float) -> Optional[str]:
    try:
        limit_price = float(ref_price) + PRE_SLIPPAGE_USD
        o = api.submit_order(
            symbol=symbol, side="buy", type="limit", time_in_force="day",
            qty=str(qty), limit_price=str(limit_price), extended_hours=True
        )
        log.info(f"[BUY-PRE/LMT] {symbol} qty={qty} limit={limit_price:.2f}")
        return o.id
    except Exception as e:
        log.error(f"BUY pre-market limit failed {symbol}: {e}")
        return None

def place_limit_sell_extended(symbol: str, qty: float, ref_bid: Optional[float] = None, pad: Optional[float] = None) -> Optional[str]:
    """Sell LIMIT in pre/after-hours at (Bid - pad) with extended_hours=True."""
    try:
        bid = ref_bid if (ref_bid is not None and ref_bid > 0) else latest_bid(symbol)
        if bid <= 0:
            log.warning(f"[SELL-PRE] {symbol}: no bid available.")
            return None
        p = SELL_PAD_USD if pad is None else pad
        limit_price = round(bid - p, 2)
        o = api.submit_order(
            symbol=symbol, side="sell", type="limit",
            time_in_force="day", qty=str(qty),
            limit_price=str(limit_price), extended_hours=True
        )
        log.info(f"[SELL-EXT/LMT] {symbol} qty={qty} limit={limit_price}")
        return o.id
    except Exception as e:
        log.error(f"SELL extended limit failed {symbol}: {e}")
        return None

def place_trailing_stop_regular(symbol: str, qty: float) -> Optional[str]:
    try:
        if TRAIL_PRICE > 0:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_price=str(TRAIL_PRICE), qty=str(qty),
                extended_hours=False
            )
        else:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_percent=str(TRAIL_PCT), qty=str(qty),
                extended_hours=False
            )
        log.info(f"[TRAIL-REG] {symbol} qty={qty}")
        return o.id
    except Exception as e:
        log.error(f"TRAIL failed {symbol}: {e}")
        return None

def try_attach_trailing_stop_if_allowed(symbol: str):
    if current_session_et() != "regular":
        log.info(f"[TRAIL SKIP] {symbol}: not regular session.")
        return
    if not should_allow_auto_sell(symbol):
        log.info(f"[TRAIL SKIP] {symbol}: pre-market lock active.")
        return
    try:
        pos = api.get_position(symbol)
        qty = float(pos.qty)
        if qty > 0:
            place_trailing_stop_regular(symbol, qty)
    except Exception as e:
        log.debug(f"attach trail skipped {symbol}: {e}")

# --------- NEW: manual quick exit before open ----------
def force_exit_pre(symbol: str, pad: float = None):
    """Close long position in PRE by sending LIMIT at Bid - pad."""
    try:
        pos = api.get_position(symbol)
        qty = float(pos.qty)
        if qty <= 0:
            log.info(f"[EXIT-PRE] {symbol}: no long qty.")
            return None
        bid = latest_bid(symbol)
        if bid <= 0:
            log.warning(f"[EXIT-PRE] {symbol}: no bid.")
            return None
        return place_limit_sell_extended(symbol, qty, ref_bid=bid, pad=pad)
    except Exception as e:
        log.error(f"[EXIT-PRE] {symbol} failed: {e}")
        return None

# --------- NEW: auto-fix market sells in PRE ----------
def auto_fix_premarket_market_sells():
    """If there are open SELL/MARKET orders during PRE, cancel and replace with LIMIT+extended."""
    if current_session_et() != "pre":
        return
    try:
        open_os = api.list_orders(status="open")
        for o in open_os:
            if o.side == "sell" and o.type == "market":
                try:
                    api.cancel_order(o.id)
                    bid = latest_bid(o.symbol)
                    qty = float(o.qty)
                    place_limit_sell_extended(o.symbol, qty, ref_bid=bid)
                    log.info(f"[AUTO-FIX] Replaced SELL/MARKET with SELL/LIMIT for {o.symbol}")
                except Exception as e:
                    log.warning(f"[AUTO-FIX] failed for {o.symbol}: {e}")
    except Exception as e:
        log.debug(f"[AUTO-FIX] list_orders failed: {e}")

# =========================
# Allocation helpers
# =========================
def get_buying_power_cash() -> float:
    try:
        acct = api.get_account()
        cash = float(getattr(acct, "cash", "0") or 0)
        if cash and cash > 0:
            return cash
        bp = float(getattr(acct, "buying_power", "0") or 0)
        return bp
    except Exception as e:
        log.warning(f"account read failed: {e}")
        return 0.0

def compute_qty_for_budget(symbol: str, budget: float) -> int:
    price = last_trade_price(symbol)
    if not price or price <= 0:
        log.warning(f"[SKIP] {symbol} no price available.")
        return 0
    qty = int(budget // price)
    if qty < 1:
        log.warning(f"[SKIP] {symbol} budget too small: ${budget:.2f}, price={price:.2f}")
        return 0
    return qty

# =========================
# Main loop
# =========================
def main_loop():
    log.info(f"SYMBOLS LOADED: {SYMBOLS}")
    log.info(
        "SETTINGS: "
        f"thr={MOMENTUM_THRESHOLD} max_pos={MAX_OPEN_POSITIONS} top_k={TOP_K} "
        f"allocate_from_cash={ALLOCATE_FROM_CASH} "
        f"trail_pct={TRAIL_PCT} trail_price={TRAIL_PRICE} "
        f"no_reentry_today={NO_REENTRY_TODAY} cooldown_min={COOLDOWN_MINUTES} "
        f"interval_s={INTERVAL_SECONDS} pre_slip_usd={PRE_SLIPPAGE_USD} "
        f"sell_pad_usd={SELL_PAD_USD} allow_pre_auto_sell={ALLOW_PRE_AUTO_SELL}"
    )

    log.info("Bot started.")
    while True:
        cycle_started = time.time()
        try:
            session = current_session_et()
            if session == "closed":
                heartbeat("Out of allowed sessions (no trading) - sleeping")
            else:
                heartbeat(f"Session={session} - cycle begin")

                # NEW: fix any SELL/MARKET stuck during PRE
                auto_fix_premarket_market_sells()

                record_today_sells(api, SYMBOLS)
                open_map = open_orders_map()

                # ==== 1) compute momentum and pick best K ====
                candidates = []  # (symbol, momentum, price)
                for symbol in SYMBOLS:
                    mom = momentum_for_last_min(symbol)
                    if mom is None:
                        log.info(f"{symbol}: ❌ no bar data / bad open; skip")
                        continue

                    states = guard_states(symbol, open_map)
                    allowed, reason = can_open_new_long(symbol, states)

                    log.info(
                        f"{symbol}: mom={mom:.5f} thr={MOMENTUM_THRESHOLD} | "
                        f"guards: pos={states['has_pos']}, "
                        f"open_order={states['has_open_order']}, "
                        f"sold_today={states['sold_today']}, "
                        f"cooldown={states['cooldown']}, "
                        f"maxpos={states['max_positions_reached']}"
                    )

                    if mom < MOMENTUM_THRESHOLD:
                        continue
                    if not allowed:
                        continue

                    price = last_trade_price(symbol)
                    if not price or price <= 0:
                        continue

                    candidates.append((symbol, mom, price))

                candidates.sort(key=lambda x: x[1], reverse=True)
                best = [c[0] for c in candidates[:TOP_K]]

                # ==== 2) capacity ====
                currently_open_syms = set(list_open_positions_symbols())
                open_count = len(currently_open_syms)
                slots_left = max(0, min(MAX_OPEN_POSITIONS, TOP_K) - open_count)
                symbols_to_open = [s for s in best if s not in currently_open_syms][:slots_left]

                log.info(f"BEST={best} | open={list(currently_open_syms)} | to_open={symbols_to_open} | slots_left={slots_left}")

                # ==== 3) per-position budget ====
                if symbols_to_open:
                    if ALLOCATE_FROM_CASH:
                        cash_or_bp = get_buying_power_cash()
                        per_budget = (cash_or_bp / len(symbols_to_open)) if cash_or_bp > 0 else FALLBACK_NOTIONAL_PER_TRADE
                    else:
                        per_budget = FALLBACK_NOTIONAL_PER_TRADE

                    log.info(f"Per-position budget ≈ ${per_budget:.2f}")

                    # ==== 4) execute buys + attach trailing ====
                    for sym in symbols_to_open:
                        cancel_symbol_open_orders(sym)  # safety
                        qty = compute_qty_for_budget(sym, per_budget)
                        if qty < 1:
                            continue

                        price = last_trade_price(sym)
                        if price is None:
                            continue

                        trade_ok, ext = can_trade_now()
                        if not trade_ok:
                            continue

                        if ext:
                            # PRE-MARKET: LIMIT only + lock sells (unless override)
                            buy_id = place_limit_buy_qty_premarket(sym, qty, ref_price=price)
                            if buy_id and not ALLOW_PRE_AUTO_SELL:
                                record_prebuy(sym)
                            # no trailing during PRE
                        else:
                            # REGULAR: MARKET buy allowed, then attach trailing
                            buy_id = place_market_buy_qty_regular(sym, qty)
                            if buy_id:
                                time.sleep(1.5)
                                try_attach_trailing_stop_if_allowed(sym)

                # Re-scan sells registry after actions
                record_today_sells(api, SYMBOLS)

                elapsed = time.time() - cycle_started
                log.info(f"🫀 Cycle done in {elapsed:.2f}s")

        except Exception as e:
            log.error(f"Loop error: {e}")

        total_elapsed = time.time() - cycle_started
        if total_elapsed > MAX_CYCLE_SECONDS:
            log.warning(f"⚠️ Slow cycle: {total_elapsed:.1f}s (limit {MAX_CYCLE_SECONDS}s)")

        sleep_until_next_interval(INTERVAL_SECONDS, cycle_started)

# =========================
# Entry
# =========================
if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
