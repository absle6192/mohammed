import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo  # ET session detection
from decimal import Decimal
import uuid

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
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))  # يُطبّق بعد الافتتاح فقط
TOP_K = int(os.getenv("TOP_K", "2"))                             # أفضل سهمين بعد الافتتاح

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

# -------- Pre-Market price pads (USD) --------
PRE_SLIPPAGE_USD = float(os.getenv("PRE_SLIPPAGE_USD", "0.05"))  # add to BUY limit
SELL_PAD_USD     = float(os.getenv("SELL_PAD_USD", "0.02"))      # under Bid for PRE sell

# ----- allow/deny auto-sell in pre-market via ENV -----
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
    - else: closed (after-hours/overnight both treated as closed)
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

def near_regular_close_window() -> bool:
    """نافذة حماية قبل الإغلاق: من 15:59:30 إلى 16:00:00 ET"""
    now = datetime.now(ET)
    from datetime import time as _t
    start = _t(15, 59, 30)
    end   = _t(16, 0, 0)
    return start <= now.time() < end and now.weekday() < 5

# =========================
# Daily registries (NEW)
# =========================
DAY_KEY: Optional[datetime.date] = None
PRE_BUY_SYMS: Set[str] = set()          # رموز تم شراؤها مرة واحدة في الـpre اليوم
sold_registry: Dict[str, datetime] = {} # موجود سابقًا لكن ننظفه يوميًا أيضًا

def _reset_daily_if_needed():
    """إعادة ضبط سجلات اليوم عند تغيّر التاريخ (ET)."""
    global DAY_KEY, PRE_BUY_SYMS, sold_registry
    today = datetime.now(ET).date()
    if DAY_KEY != today:
        DAY_KEY = today
        PRE_BUY_SYMS.clear()
        sold_registry.clear()
        PREMARKET_LOCK.clear()
        PRE_EXT_ORDERS.clear()
        log.info("🔄 Daily registries reset.")

# =========================
# Re-entry Registry (existing helpers + minor keep)
# =========================
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

def _was_sell_premarket(ts: datetime) -> bool:
    from datetime import time as _t
    ts_et = ts.astimezone(ET)
    return ts_et.weekday() < 5 and ts_et.time() < _t(9, 30)

def _is_after_or_at_regular_open_now() -> bool:
    from datetime import time as _t
    now_et = datetime.now(ET)
    return now_et.weekday() < 5 and now_et.time() >= _t(9, 30)

def sold_today(symbol: str) -> bool:
    if not NO_REENTRY_TODAY:
        return False
    ts = sold_registry.get(symbol)
    if not ts or ts.date() != utc_today():
        return False
    # allow re-entry after market open if the SELL happened premarket
    if _is_after_or_at_regular_open_now() and _was_sell_premarket(ts):
        return False
    return True

def in_cooldown(symbol: str) -> bool:
    if COOLDOWN_MINUTES <= 0:
        return False
    ts = sold_registry.get(symbol)
    if not ts:
        return False
    # bypass cooldown at/after regular open if the prior sell was premarket
    if _is_after_or_at_regular_open_now() and _was_sell_premarket(ts):
        return False
    return bool(utc_now() < ts + timedelta(minutes=COOLDOWN_MINUTES))

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

def fallback_price(symbol: str) -> float:
    p = last_trade_price(symbol)
    if p and p > 0:
        return p
    try:
        bars = api.get_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), limit=1).df
        if not bars.empty:
            close = float(bars.iloc[-1]["close"])
            if close > 0:
                return close
    except Exception:
        pass
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
        # سنستخدم حدّ المراكز فقط بعد الافتتاح، لذلك الإخبار هنا معلوماتي
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
    return True, ""

# =========================
# Pre-Market lock (existing)
# =========================
PREMARKET_LOCK: Dict[str, str] = {}           # symbol -> ISO date
PRE_CLIENT_PREFIX = "PRE-"                    # لتعليم أوامر ما قبل الافتتاح
PRE_EXT_ORDERS: Dict[str, bool] = {}          # order_id -> True (للغاء قبل after-hours)

def record_prebuy(symbol: str):
    PREMARKET_LOCK[symbol.upper()] = datetime.now(ET).date().isoformat()
    PRE_BUY_SYMS.add(symbol.upper())  # NEW: لا تشتريه مرة ثانية في الـpre اليوم

def clear_lock_if_no_position(symbol: str):
    sym = symbol.upper()
    try:
        pos = api.get_position(sym)
        if float(pos.qty) == 0:
            PREMARKET_LOCK.pop(sym, None)
    except Exception:
        PREMARKET_LOCK.pop(sym, None)

def should_allow_auto_sell(symbol: str) -> bool:
    sym = symbol.upper()
    clear_lock_if_no_position(sym)
    session = current_session_et()
    if ALLOW_PRE_AUTO_SELL:
        return True
    if sym in PREMARKET_LOCK and session == "pre":
        return False
    return True

def refresh_pre_ext_registry():
    try:
        for o in api.list_orders(status="open"):
            cid = getattr(o, "client_order_id", "") or ""
            if cid.startswith(PRE_CLIENT_PREFIX):
                PRE_EXT_ORDERS[o.id] = True
    except Exception:
        pass

def cancel_pre_ext_orders_before_afterhours():
    if not near_regular_close_window():
        return
    refresh_pre_ext_registry()
    if not PRE_EXT_ORDERS:
        return
    try:
        for oid in list(PRE_EXT_ORDERS.keys()):
            try:
                api.cancel_order(oid)
                PRE_EXT_ORDERS.pop(oid, None)
                log.info(f"[SAFETY] Canceled PRE order before after-hours: {oid}")
            except Exception as e:
                log.warning(f"[SAFETY] Cancel PRE order failed {oid}: {e}")
    except Exception as e:
        log.debug(f"[SAFETY] scan/cancel failed: {e}")

# =========================
# Orders
# =========================
def _make_client_id(prefix: str, symbol: str) -> str:
    return f"{prefix}{symbol}-{uuid.uuid4().hex[:10]}"

def place_market_buy_qty_regular(symbol: str, qty: int) -> Optional[str]:
    try:
        o = api.submit_order(
            symbol=symbol, side="buy", type="market",
            time_in_force="day", qty=str(qty), extended_hours=False,
            client_order_id=_make_client_id("REG-BUY-", symbol)
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
            qty=str(qty), limit_price=str(limit_price), extended_hours=True,
            client_order_id=_make_client_id(PRE_CLIENT_PREFIX + "BUY-", symbol)
        )
        PRE_EXT_ORDERS[o.id] = True
        log.info(f"[BUY-PRE/LMT] {symbol} qty={qty} limit={limit_price:.2f}")
        return o.id
    except Exception as e:
        log.error(f"BUY pre-market limit failed {symbol}: {e}")
        return None

def place_limit_sell_extended(symbol: str, qty: float,
                              ref_bid: Optional[float] = None,
                              pad: Optional[float] = None) -> Optional[str]:
    try:
        bid = ref_bid if (ref_bid is not None and ref_bid > 0) else latest_bid(symbol)
        ref = bid if bid > 0 else fallback_price(symbol)
        if ref <= 0:
            log.warning(f"[SELL-PRE] {symbol}: no bid/trade price available.")
            return None
        p = SELL_PAD_USD if pad is None else pad
        limit_price = round(max(ref - p, 0.01), 2)
        o = api.submit_order(
            symbol=symbol, side="sell", type="limit",
            time_in_force="day", qty=str(qty),
            limit_price=str(limit_price), extended_hours=True,
            client_order_id=_make_client_id(PRE_CLIENT_PREFIX + "SELL-", symbol)
        )
        PRE_EXT_ORDERS[o.id] = True
        log.info(f"[SELL-EXT/LMT] {symbol} qty={qty} ref={ref} pad={p} limit={limit_price}")
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
                extended_hours=False, client_order_id=_make_client_id("REG-TRAIL-", symbol)
            )
        else:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_percent=str(TRAIL_PCT), qty=str(qty),
                extended_hours=False, client_order_id=_make_client_id("REG-TRAIL-", symbol)
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

# --------- Manual quick exit before open ----------
def force_exit_pre(symbol: str, pad: float = None):
    try:
        pos = api.get_position(symbol)
        qty = float(pos.qty)
        if qty <= 0:
            log.info(f"[EXIT-PRE] {symbol}: no long qty.")
            return None
        return place_limit_sell_extended(symbol, qty, pad=pad)
    except Exception as e:
        log.error(f"[EXIT-PRE] {symbol} failed: {e}")
        return None

# --------- Auto-fix market sells in PRE ----------
_LAST_FIXED_AT: Dict[str, float] = {}  # order_id -> epoch seconds

def _should_fix_sell_in_pre(order) -> bool:
    try:
        if order.side != "sell":
            return False
        typ = (order.type or "").lower()
        ext = bool(getattr(order, "extended_hours", False))
        return (typ == "market") or (not ext)
    except Exception:
        return False

def auto_fix_premarket_market_sells(pulses: int = 3, pulse_sleep: float = 0.35):
    if current_session_et() != "pre":
        return
    for _ in range(max(1, pulses)):
        try:
            orders = api.list_orders(status="open")
            now = time.time()
            for o in orders:
                if not _should_fix_sell_in_pre(o):
                    continue
                last = _LAST_FIXED_AT.get(o.id, 0.0)
                if now - last < 2.0:
                    continue
                try:
                    api.cancel_order(o.id)
                except Exception as e:
                    log.warning(f"[AUTO-FIX] cancel failed {o.symbol}: {e}")
                    _LAST_FIXED_AT[o.id] = now
                    continue
                try:
                    qty = float(Decimal(str(o.qty)))
                except Exception:
                    log.warning(f"[AUTO-FIX] bad qty for {o.symbol}")
                    _LAST_FIXED_AT[o.id] = now
                    continue
                try:
                    place_limit_sell_extended(o.symbol, qty)
                    log.info(f"[AUTO-FIX] SELL -> LIMIT+extended for {o.symbol}")
                except Exception as e:
                    log.warning(f"[AUTO-FIX] replace failed {o.symbol}: {e}")
                _LAST_FIXED_AT[o.id] = now
        except Exception as e:
            log.debug(f"[AUTO-FIX] list_orders failed: {e}")
        time.sleep(max(0.0, pulse_sleep))

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
            _reset_daily_if_needed()

            session = current_session_et()
            if session == "closed":
                heartbeat("Out of allowed sessions (no trading) - sleeping")
            else:
                heartbeat(f"Session={session} - cycle begin")

                # إصلاح سريع لأوامر بيع الـpre
                auto_fix_premarket_market_sells()

                # حماية: الغاء أوامر PRE قبل after-hours
                cancel_pre_ext_orders_before_afterhours()

                # نبضة ثانية للإصلاح
                auto_fix_premarket_market_sells()

                record_today_sells(api, SYMBOLS)
                open_map = open_orders_map()

                # ==== 2) compute momentum ====
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

                # ترتيب حسب الزخم الأعلى
                candidates.sort(key=lambda x: x[1], reverse=True)

                # ==== 3) اختيار المرشحين وفق الجلسة ====
                currently_open_syms = set(list_open_positions_symbols())

                if session == "pre":
                    # قبل الافتتاح: اشترِ كل المرشحين (مرّة واحدة فقط لكل رمز في الـpre)
                    best_list = [c[0] for c in candidates]  # بدون قطع TOP_K
                    # استبعد ما تم شراؤه في الـpre مسبقًا اليوم، وما عليه مركز/أمر مفتوح
                    symbols_to_open = [
                        s for s in best_list
                        if (s not in PRE_BUY_SYMS) and (s not in currently_open_syms) and (s not in open_map)
                    ]
                    # لا نطبق MAX_OPEN_POSITIONS في الـpre حسب طلبك
                    slots_left = len(symbols_to_open)
                else:
                    # بعد الافتتاح: التزم بأفضل سهمين فقط، وبحد مراكز متزامنة
                    best_list = [c[0] for c in candidates[:TOP_K]]
                    open_count = len(currently_open_syms)
                    slots_left = max(0, min(MAX_OPEN_POSITIONS, TOP_K) - open_count)
                    symbols_to_open = [s for s in best_list if s not in currently_open_syms][:slots_left]

                log.info(f"BEST={best_list} | open={list(currently_open_syms)} | to_open={symbols_to_open} | slots_left={slots_left}")

                # ==== 4) per-position budget ====
                if symbols_to_open:
                    if ALLOCATE_FROM_CASH:
                        cash_or_bp = get_buying_power_cash()
                        per_budget = (cash_or_bp / len(symbols_to_open)) if cash_or_bp > 0 else FALLBACK_NOTIONAL_PER_TRADE
                    else:
                        per_budget = FALLBACK_NOTIONAL_PER_TRADE

                    log.info(f"Per-position budget ≈ ${per_budget:.2f}")

                    # ==== 5) execute buys + attach trailing ====
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
                            # PRE-MARKET: LIMIT only + سجل أن هذا الرمز تم شراؤه في الـpre اليوم
                            buy_id = place_limit_buy_qty_premarket(sym, qty, ref_price=price)
                            if buy_id:
                                record_prebuy(sym)   # يمنع إعادة شراء نفس الرمز في الـpre
                            # لا نرفق trailing في الـpre
                        else:
                            # REGULAR: MARKET buy allowed, ثم trailing
                            buy_id = place_market_buy_qty_regular(sym, qty)
                            if buy_id:
                                time.sleep(1.5)
                                try_attach_trailing_stop_if_allowed(sym)

                # تحديث سجلات البيع
                record_today_sells(api, SYMBOLS)

                # نبضة أخيرة قبل النوم تلتقط أي أمر بيع ظهر خلال الدورة
                auto_fix_premarket_market_sells()

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
