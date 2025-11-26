import os
import time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

SYMBOLS: List[str] = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "TSLA,NVDA,AAPL,CRWD,AMZN,AMD,GOOGL,MU"
).split(",") if s.strip()]

# ===== إعدادات المومنتوم =====
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.00005"))

# أقصى عدد مراكز مفتوحة في نفس الوقت
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "4"))

TOP_K = int(os.getenv("TOP_K", "3"))
TOP_K = min(TOP_K, MAX_OPEN_POSITIONS)

# ===== Allocation Settings =====
ALLOCATE_FROM_CASH = os.getenv("ALLOCATE_FROM_CASH", "true").lower() == "true"
FALLBACK_NOTIONAL_PER_TRADE = float(os.getenv("NOTIONAL_PER_TRADE", "6250"))

STRICT_CASH_ONLY   = os.getenv("STRICT_CASH_ONLY", "true").lower() == "true"
CASH_RESERVE_PCT   = float(os.getenv("CASH_RESERVE_PCT", "0.02"))
PER_TRADE_PCT      = float(os.getenv("PER_TRADE_PCT", "0.0"))

# قيم Trailing لا نستخدمها الآن في الخروج، تركناها فقط جاهزة للمستقبل لو احتجناها
TRAIL_PCT   = float(os.getenv("TRAIL_PCT", "0.3"))   # كنسبة مئوية (0.3 = 0.3%)
TRAIL_PRICE = float(os.getenv("TRAIL_PRICE", "0.0"))

# الآن الافتراضي: يسمح بإعادة الدخول لنفس السهم في نفس اليوم
NO_REENTRY_TODAY = os.getenv("NO_REENTRY_TODAY", "false").lower() == "true"

# الافتراضي الآن 0 دقيقة → ما فيه انتظار إجباري بعد البيع
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "0"))

INTERVAL_SECONDS   = int(os.getenv("INTERVAL_SECONDS", "30"))
MAX_CYCLE_SECONDS  = int(os.getenv("MAX_CYCLE_SECONDS", "20"))

PRE_SLIPPAGE_USD = float(os.getenv("PRE_SLIPPAGE_USD", "0.05"))
SELL_PAD_USD     = float(os.getenv("SELL_PAD_USD", "0.02"))

# هدف ربح ثابت بالدولار لكل صفقة → بيع MARKET فوري
TAKE_PROFIT_USD = float(os.getenv("TAKE_PROFIT_USD", "20"))

ALLOW_PRE_AUTO_SELL = os.getenv("ALLOW_PRE_AUTO_SELL", "false").lower() == "true"

# إعدادات pre-market (سيتم تعطيلها إذا ENABLE_PREMARKET=False)
MAX_PREMARKET_SLOTS       = int(os.getenv("MAX_PREMARKET_SLOTS", "2"))
ORDER_COOLDOWN_SECONDS    = int(os.getenv("ORDER_COOLDOWN_SECONDS", "8"))
MAX_ORDERS_PER_CYCLE_PRE  = int(os.getenv("MAX_ORDERS_PER_CYCLE_PRE", "1"))
MIN_PREMARKET_DOLLAR_VOL  = float(os.getenv("MIN_PREMARKET_DOLLAR_VOL", "500000"))
MAX_SPREAD_BPS            = float(os.getenv("MAX_SPREAD_BPS", "15"))  # 0.15%

INSTANT_FIX_INTERVAL_SEC = float(os.getenv("INSTANT_FIX_INTERVAL_SEC", "0.5"))

_is_submitting = False
_last_submit_ts = 0.0

INSTANT_FIX_DONE: Dict[str, bool] = {}
_instant_fixing = False
_last_instant_fix_ts = 0.0

# مفتاح واحد للتحكم في تداول ما قبل الافتتاح
ENABLE_PREMARKET = False
if not API_KEY or not API_SECRET:
    log.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

def _mask(s: str):
    return "(empty)" if not s else f"{s[:3]}***{s[-3:]}"

log.info(f"BASE_URL={BASE_URL} | KEY={_mask(API_KEY)}")
try:
    acct = api.get_account()
    log.info(f"account status ok, buying_power={getattr(acct, 'buying_power', None)} | cash={getattr(acct,'cash',None)}")
except Exception as e:
    log.error(f"account check failed: {e}")

if not ENABLE_PREMARKET:
    log.info("PREMARKET trading is DISABLED (ENABLE_PREMARKET=False). No extended-hours/pre-market orders will be sent.")

ET = ZoneInfo("America/New_York")

def _can_submit_now() -> bool:
    return (time.time() - _last_submit_ts) >= ORDER_COOLDOWN_SECONDS

def _mark_submit():
    global _last_submit_ts
    _last_submit_ts = time.time()

def utc_now():
    return datetime.now(timezone.utc)

def utc_today():
    return utc_now().date()

def heartbeat(msg: str):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log.info(f"{msg} at {now}Z")

def sleep_until_next_interval(interval_seconds: int, started_at: float):
    elapsed = time.time() - started_at
    sleep_left = max(0.0, interval_seconds - elapsed)
    time.sleep(sleep_left)

def current_session_et(dt: datetime | None = None) -> str:
    now = (dt or datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    from datetime import time as _t
    PRE_START  = _t(4, 10)
    REG_START  = _t(9, 30)
    REG_END    = _t(16, 0)
    if PRE_START <= t < REG_START:
        return "pre"
    if REG_START <= t < REG_END:
        return "regular"
    return "closed"

def can_trade_now() -> tuple[bool, bool]:
    """
    returns (can_trade, is_pre)
    """
    s = current_session_et()
    if s == "pre" and not ENABLE_PREMARKET:
        return False, False
    if s == "pre":
        return True, True
    if s == "regular":
        return True, False
    return False, False

# ===== سجلات البيع / الكول داون =====
sold_registry: Dict[str, datetime] = {}
sold_pre_market: Dict[str, datetime] = {}
sold_regular_lock: Dict[str, datetime] = {}

def record_today_sells(api: REST, symbols: List[str]) -> None:
    try:
        closed = api.list_orders(status="closed", limit=200, direction="desc")
    except Exception as e:
        log.warning(f"list_orders failed: {e}")
        return
    from datetime import time as _t
    REG_START_T = _t(9, 30)
    REG_END_T   = _t(16, 0)
    for o in closed:
        try:
            if (getattr(o, "side", "") != "sell") or o.symbol not in symbols:
                continue
            if not getattr(o, "filled_at", None):
                continue
            filled_at = o.filled_at if o.filled_at.tzinfo else o.filled_at.replace(tzinfo=timezone.utc)
            if filled_at.date() != utc_today():
                continue
            sold_registry[o.symbol] = filled_at
            tt = filled_at.astimezone(ET).time()
            if tt < REG_START_T:
                sold_pre_market[o.symbol] = filled_at
            elif REG_START_T <= tt < REG_END_T:
                sold_regular_lock[o.symbol] = filled_at
        except Exception:
            continue

def recent_regular_sell_symbols(window_sec: int = 20) -> set:
    syms = set()
    try:
        closed = api.list_orders(status="closed", limit=100, direction="desc")
        cutoff = utc_now() - timedelta(seconds=window_sec)
        from datetime import time as _t
        REG_START_T = _t(9, 30)
        REG_END_T   = _t(16, 0)
        for o in closed:
            if getattr(o, "side", "") != "sell":
                continue
            t = getattr(o, "filled_at", None)
            if not t:
                continue
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if t < cutoff or t.date() != utc_today():
                continue
            tt = t.astimezone(ET).time()
            if REG_START_T <= tt < REG_END_T:
                syms.add(o.symbol)
    except Exception as e:
        log.debug(f"recent sells scan failed: {e}")
    return syms

def sold_today(symbol: str) -> bool:
    if not NO_REENTRY_TODAY:
        return False
    ts = sold_registry.get(symbol)
    return bool(ts and ts.date() == utc_today())

def sold_regular_today(symbol: str) -> bool:
    ts = sold_regular_lock.get(symbol)
    return bool(ts and ts.date() == utc_today())

def in_cooldown(symbol: str) -> bool:
    if COOLDOWN_MINUTES <= 0:
        return False
    ts = sold_registry.get(symbol)
    return bool(ts and utc_now() < ts + timedelta(minutes=COOLDOWN_MINUTES))

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
        b = (
            getattr(q, "bidprice", None) or
            getattr(q, "bid_price", None) or
            getattr(q, "bid", None) or
            0
        )
        return float(b or 0.0)
    except Exception:
        return 0.0

def best_ask(symbol: str) -> Optional[float]:
    try:
        q = api.get_latest_quote(symbol)
        a = (
            getattr(q, "askprice", None) or
            getattr(q, "ask_price", None) or
            getattr(q, "ask", None)
        )
        return float(a) if a else None
    except Exception:
        return None

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
        for o in api.list_orders(status="open", limit=500):
            m[o.symbol] = True
    except Exception:
        pass
    return m

def cancel_symbol_open_buy_orders(symbol: str):
    try:
        for o in api.list_orders(status="open", limit=500):
            if o.symbol == symbol and getattr(o, "side", "").lower() == "buy":
                try:
                    api.cancel_order(o.id)
                    log.info(f"Canceled open BUY order: {symbol} ({o.type})")
                except Exception as e:
                    log.warning(f"Cancel failed for {symbol}: {e}")
    except Exception:
        pass

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

def active_buy_slots(api: REST) -> tuple[int, int, int]:
    try:
        p = len(api.list_positions())
    except Exception:
        p = 0
    try:
        orders = api.list_orders(status="open", limit=500)
        ob = sum(1 for o in orders if getattr(o, "side", "").lower() == "buy")
    except Exception:
        ob = 0
    return p, ob, p + ob

def allowed_slots(api: REST) -> int:
    _, _, total = active_buy_slots(api)
    return max(0, MAX_OPEN_POSITIONS - total)

def guard_states(symbol: str, open_orders: Dict[str, bool]) -> Dict[str, bool]:
    _, _, total = active_buy_slots(api)
    return {
        "has_pos": has_open_position(symbol),
        "has_open_order": open_orders.get(symbol, False),
        "sold_today": sold_today(symbol),
        "cooldown": in_cooldown(symbol),
        "max_positions_reached": total >= MAX_OPEN_POSITIONS
    }

def can_open_new_long(symbol: str, states: Dict[str, bool], session: str) -> Tuple[bool, str]:
    if states["has_pos"]:
        return False, "already have position"
    if states["has_open_order"]:
        return False, "open order exists"
    if states["cooldown"]:
        return False, "in cooldown"
    if states["max_positions_reached"]:
        return False, "cap reached"

    # منطق منع إعادة الدخول نفس اليوم (اختياري عبر NO_REENTRY_TODAY)
    if NO_REENTRY_TODAY:
        # يسمح فقط بإعادة الدخول في الريجولار إذا كان البيع الأول في pre-market
        if states["sold_today"] and session == "regular" and symbol in sold_pre_market:
            return True, "re-entry after pre-market sell"
        if session == "regular" and sold_regular_today(symbol):
            return False, "sold earlier in regular session"
        if states["sold_today"] and session != "regular":
            return False, "no-reentry-today"

    # إذا NO_REENTRY_TODAY=False → يسمح بإعادة الدخول لنفس السهم في نفس اليوم
    return True, ""

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
    sym = symbol.upper()
    clear_lock_if_no_position(sym)
    session = current_session_et()
    if ALLOW_PRE_AUTO_SELL:
        return True
    if sym in PREMARKET_LOCK and session == "pre" and ENABLE_PREMARKET:
        return False
    return True

def place_market_buy_qty_regular(symbol: str, qty: int) -> Optional[str]:
    try:
        cid = f"open-{symbol}-{int(time.time()*1000)}"
        o = api.submit_order(
            symbol=symbol, side="buy", type="market",
            time_in_force="day", qty=str(qty),
            extended_hours=False, client_order_id=cid
        )
        log.info(f"[BUY-REG/MKT] {symbol} qty={qty}")
        return o.id
    except Exception as e:
        log.error(f"BUY regular market failed {symbol}: {e}")
        return None

def place_limit_buy_qty_premarket(symbol: str, qty: int, ref_price: float) -> Optional[str]:
    if not ENABLE_PREMARKET:
        log.debug(f"[BUY-PRE/LMT SKIP] Pre-market disabled, skipping buy for {symbol}.")
        return None
    try:
        ask = best_ask(symbol)
        base = ask if (ask and ask > 0) else ref_price
        limit_price = round(float(base) + PRE_SLIPPAGE_USD, 2)
        cid = f"open-pre-{symbol}-{int(time.time()*1000)}"
        o = api.submit_order(
            symbol=symbol, side="buy", type="limit", time_in_force="day",
            qty=str(qty), limit_price=str(limit_price),
            extended_hours=False, client_order_id=cid
        )
        log.info(f"[BUY-PRE/LMT] {symbol} qty={qty} limit={limit_price:.2f}")
        return o.id
    except Exception as e:
        log.error(f"BUY pre-market limit failed {symbol}: {e}")
        return None

def place_limit_sell_extended(symbol: str, qty: float, ref_bid: Optional[float] = None,
                              pad: Optional[float] = None) -> Optional[str]:
    if not ENABLE_PREMARKET and current_session_et() == "pre":
        log.debug(f"[SELL-EXT SKIP] Pre-market disabled, not placing extended sell for {symbol}.")
        return None
    try:
        bid = ref_bid if (ref_bid is not None and ref_bid > 0) else latest_bid(symbol)
        if bid <= 0:
            log.warning(f"[SELL-EXT] {symbol}: no bid available.")
            return None
        p = SELL_PAD_USD if pad is None else pad
        limit_price = round(bid - p, 2)
        cid = f"exit-ext-{symbol}-{int(time.time()*1000)}"
        o = api.submit_order(
            symbol=symbol, side="sell", type="limit", time_in_force="day",
            qty=str(qty), limit_price=str(limit_price),
            extended_hours=False
        )
        log.info(f"[SELL-EXT/LMT] {symbol} qty={qty} limit={limit_price}")
        return o.id
    except Exception as e:
        log.error(f"SELL extended limit failed {symbol}: {e}")
        return None

# دوال Trailing موجودة لكن لا نستدعيها في المنطق الحالي (الخروج الآن بالـ TP فقط)
def place_trailing_stop_regular(symbol: str, qty: float) -> Optional[str]:
    try:
        if TRAIL_PRICE > 0:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_price=str(TRAIL_PRICE),
                qty=str(qty), extended_hours=False
            )
        else:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_percent=str(TRAIL_PCT),
                qty=str(qty), extended_hours=False
            )
        log.info(f"[TRAIL-REG] {symbol} qty={qty}")
        return o.id
    except Exception as e:
        log.error(f"TRAIL failed {symbol}: {e}")
        return None

def try_attach_trailing_stop_if_allowed(symbol: str):
    # حالياً لا نستخدم Trailing Stop بعد الشراء
    return

def force_exit_pre(symbol: str, pad: float = None):
    try:
        pos = api.get_position(symbol)
        qty = float(pos.qty)
        if qty <= 0:
            return None
        bid = latest_bid(symbol)
        if bid <= 0:
            return None
        return place_limit_sell_extended(symbol, qty, ref_bid=bid, pad=pad)
    except Exception as e:
        log.error(f"[EXIT-PRE] {symbol} failed: {e}")
        return None

def _remaining_qty(o) -> float:
    try:
        original_qty = float(getattr(o, "qty", 0) or 0)
        filled_qty   = float(getattr(o, "filled_qty", 0) or 0)
        return max(0.0, original_qty - filled_qty)
    except Exception:
        return float(getattr(o, "qty", 0) or 0)

def instant_fix_market_orders():
    if not ENABLE_PREMARKET:
        return
    if current_session_et() != "pre":
        return
    global _instant_fixing
    if _instant_fixing:
        return
    try:
        _instant_fixing = True
        open_orders = api.list_orders(status="open", limit=500)
        for o in open_orders:
            sym  = getattr(o, "symbol", "").upper()
            side = (getattr(o, "side", "") or "").lower()
            otype = (getattr(o, "type", "") or "").lower()
            if INSTANT_FIX_DONE.get(sym):
                continue
            if side == "sell" and otype == "market":
                try:
                    api.cancel_order(o.id)
                except Exception:
                    pass
                bid = latest_bid(sym)
                qty = _remaining_qty(o)
                if bid > 0 and qty > 0:
                    placed = place_limit_sell_extended(sym, qty, ref_bid=bid)
                    if placed:
                        INSTANT_FIX_DONE[sym] = True
                        log.info(f"[INSTANT-FIX] SELL Market→Limit for {sym}")
            elif side == "buy" and otype == "market":
                try:
                    api.cancel_order(o.id)
                except Exception:
                    pass
                last = last_trade_price(sym) or latest_bid(sym)
                qty_i = int(_remaining_qty(o))
                if last and qty_i > 0:
                    placed = place_limit_buy_qty_premarket(sym, qty_i, ref_price=last)
                    if placed:
                        INSTANT_FIX_DONE[sym] = True
                        log.info(f"[INSTANT-FIX] BUY Market→Limit for {sym}")
    except Exception as e:
        log.debug(f"[INSTANT-FIX] failed: {e}")
    finally:
        _instant_fixing = False

def auto_fix_premarket_market_sells():
    if not ENABLE_PREMARKET:
        return
    if current_session_et() != "pre":
        return
    try:
        open_os = api.list_orders(status="open", limit=500)
        for o in open_os:
            try:
                sym  = getattr(o, "symbol", "").upper()
                side = (getattr(o, "side", "") or "").lower()
                otype= (getattr(o, "type", "") or "").lower()
                if INSTANT_FIX_DONE.get(sym):
                    continue
                if side == "sell" and otype == "market":
                    api.cancel_order(o.id)
                    bid = latest_bid(sym)
                    qty = _remaining_qty(o)
                    if bid > 0 and qty > 0:
                        place_limit_sell_extended(sym, qty, ref_bid=bid)
                        log.info(f"[AUTO-FIX] SELL MKT→LMT {sym}")
                if side == "buy" and otype == "market":
                    api.cancel_order(o.id)
                    last = last_trade_price(sym) or latest_bid(sym)
                    qty  = int(_remaining_qty(o))
                    if last and qty > 0:
                        place_limit_buy_qty_premarket(sym, qty, ref_price=last)
                        log.info(f"[AUTO-FIX] BUY MKT→LMT {sym}")
            except Exception as e:
                log.debug(f"[AUTO-FIX] one failed: {e}")
    except Exception as e:
        log.debug(f"[AUTO-FIX] list_orders failed: {e}")

def rescue_rejected_premarket_market_sells(window_sec: int = 5):
    if not ENABLE_PREMARKET:
        return
    if current_session_et() != "pre":
        return
    cutoff = utc_now() - timedelta(seconds=window_sec)
    try:
        closed = api.list_orders(status="closed", limit=200, direction="desc")
    except Exception as e:
        log.debug(f"[RESCUE] list_orders failed: {e}")
        return
    for o in closed:
        try:
            side   = (getattr(o, "side", "") or "").lower()
            otype  = (getattr(o, "type", "") or "").lower()
            status = (getattr(o, "status", "") or "").lower()
            sym    = getattr(o, "symbol", "").upper()
            t = getattr(o, "filled_at", None) or getattr(o, "updated_at", None) or getattr(o, "created_at", None)
            if not t:
                continue
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            if side != "sell" or otype != "market" or status != "rejected":
                continue
            if t < cutoff:
                continue
            if INSTANT_FIX_DONE.get(sym):
                continue
            qty = float(getattr(o, "qty", 0) or 0)
            if qty <= 0:
                continue
            bid = latest_bid(sym)
            if bid <= 0:
                continue
            placed = place_limit_sell_extended(sym, qty, ref_bid=bid)
            if placed:
                INSTANT_FIX_DONE[sym] = True
                log.info(f"[RESCUE] SELL REJECTED MKT→LMT {sym}")
        except Exception as e:
            log.debug(f"[RESCUE] error: {e}")

def get_cash_balance(strict_cash_only: bool = True) -> float:
    try:
        acct = api.get_account()
        cash = float(getattr(acct, "cash", "0") or 0)
        if strict_cash_only:
            return max(0.0, cash)
        bp = float(getattr(acct, "buying_power", "0") or 0)
        return cash if cash > 0 else bp
    except Exception as e:
        log.warning(f"account read failed: {e}")
        return 0.0

def compute_qty_for_budget(symbol: str, budget: float) -> Tuple[int, float]:
    price = last_trade_price(symbol)
    if not price or price <= 0:
        log.warning(f"[SKIP] {symbol} no price available.")
        return 0, 0.0
    qty = int(budget // price)
    if qty < 1:
        log.warning(f"[SKIP] {symbol} budget too small: ${budget:.2f}, price={price:.2f}")
        return 0, price
    return qty, price

def plan_budgets_for_opens(total_cash: float, open_count: int,
                           to_open_count: int, target_slots: int) -> List[float]:
    if to_open_count <= 0:
        return []
    reserve = max(0.0, total_cash * CASH_RESERVE_PCT)
    usable  = max(0.0, total_cash - reserve)
    remaining_slots = max(1, target_slots - open_count)
    remaining_slots = min(remaining_slots, to_open_count)
    per = usable / remaining_slots if remaining_slots > 0 else 0.0
    return [per for _ in range(remaining_slots)]

def positions_qty_map() -> Dict[str, float]:
    try:
        return {p.symbol: float(p.qty) for p in api.list_positions()}
    except Exception:
        return {}

_last_qty: Dict[str, float] = {}

# ===== تحقق هدف الربح +TP_USD وبيع ماركت فوراً =====
def maybe_take_profit_market(symbol: str, pos, last_price: Optional[float],
                             tp_usd: float) -> bool:
    if tp_usd <= 0:
        return False
    if last_price is None or last_price <= 0:
        return False
    try:
        qty       = float(pos.qty)
        avg_price = float(pos.avg_entry_price)
    except Exception:
        return False
    if qty <= 0:
        return False
    unrealized_pnl = (last_price - avg_price) * qty
    if unrealized_pnl >= tp_usd:
        log.info(f"[TP] {symbol}: unrealized_pnl={unrealized_pnl:.2f} >= {tp_usd:.2f} → SELL MARKET NOW")
        try:
            api.submit_order(
                symbol=symbol,
                qty=str(qty),
                side="sell",
                type="market",
                time_in_force="day",
                extended_hours=False
            )
            now = utc_now()
            sold_registry[symbol] = now
            if current_session_et() == "regular":
                sold_regular_lock[symbol] = now
            return True
        except Exception as e:
            log.error(f"[TP] Failed to submit TP sell for {symbol}: {e}")
            return False
    return False

def main_loop():
    log.info(f"SYMBOLS LOADED: {SYMBOLS}")
    log.info(
        "SETTINGS: "
        f"thr={MOMENTUM_THRESHOLD} max_pos={MAX_OPEN_POSITIONS} top_k={TOP_K} "
        f"allocate_from_cash={ALLOCATE_FROM_CASH} strict_cash_only={STRICT_CASH_ONLY} "
        f"reserve={CASH_RESERVE_PCT} per_trade_pct={PER_TRADE_PCT} "
        f"trail_pct={TRAIL_PCT} trail_price={TRAIL_PRICE} "
        f"no_reentry_today={NO_REENTRY_TODAY} cooldown_minutes={COOLDOWN_MINUTES} "
        f"interval_s={INTERVAL_SECONDS} pre_slip_usd={PRE_SLIPPAGE_USD} "
        f"sell_pad_usd={SELL_PAD_USD} allow_pre_auto_sell={ALLOW_PRE_AUTO_SELL} "
        f"pre_slots={MAX_PREMARKET_SLOTS} pre_cooldown_s={ORDER_COOLDOWN_SECONDS} "
        f"max_orders_per_cycle_pre={MAX_ORDERS_PER_CYCLE_PRE} "
        f"instant_fix_interval_s={INSTANT_FIX_INTERVAL_SEC} "
        f"min_pre_dollar_vol={MIN_PREMARKET_DOLLAR_VOL} "
        f"max_spread_bps={MAX_SPREAD_BPS} pre_enabled={ENABLE_PREMARKET} "
        f"take_profit_usd={TAKE_PROFIT_USD}"
    )
    log.info("Bot started.")

    global _last_instant_fix_ts

    while True:
        cycle_started = time.time()
        try:
            session = current_session_et()
            if session == "pre" and not ENABLE_PREMARKET:
                session = "closed"

            if session == "closed":
                heartbeat("Out of allowed sessions (no trading) - sleeping")
            else:
                heartbeat(f"Session={session} - cycle begin")

                # أولاً: تحقق من كل المراكز المفتوحة وطبّق TP
                if TAKE_PROFIT_USD > 0:
                    try:
                        open_positions = api.list_positions()
                        for pos in open_positions:
                            sym = pos.symbol
                            if sym not in SYMBOLS:
                                continue
                            lp = last_trade_price(sym)
                            maybe_take_profit_market(sym, pos, lp, TAKE_PROFIT_USD)
                    except Exception as e:
                        log.debug(f"[TP] scan failed: {e}")

                # قفل تغيّر الكميات (تسجيل عمليات البيع في جلسة ريجولار)
                pos_now = positions_qty_map()
                if session == "regular":
                    try:
                        syms = set(list(_last_qty.keys()) + list(pos_now.keys()))
                        for s in syms:
                            prev_q = _last_qty.get(s, 0.0)
                            curr_q = pos_now.get(s, 0.0)
                            if prev_q > 0 and curr_q <= 0:
                                now = utc_now()
                                sold_registry[s] = now
                                sold_regular_lock[s] = now
                    except Exception:
                        pass
                _last_qty.clear()
                _last_qty.update(pos_now)

                just_sold = recent_regular_sell_symbols(window_sec=20)
                for s in just_sold:
                    sold_regular_lock[s] = utc_now()

                now_ts = time.time()
                if (now_ts - _last_instant_fix_ts) >= INSTANT_FIX_INTERVAL_SEC:
                    instant_fix_market_orders()
                    if session != "pre" and INSTANT_FIX_DONE:
                        INSTANT_FIX_DONE.clear()
                    _last_instant_fix_ts = now_ts

                auto_fix_premarket_market_sells()
                rescue_rejected_premarket_market_sells()

                record_today_sells(api, SYMBOLS)
                open_map = open_orders_map()

                if session == "pre":
                    target_slots = MAX_PREMARKET_SLOTS
                    p, ob, total = active_buy_slots(api)
                    busy_slots = min(target_slots, p + ob)
                    slots_left = max(0, target_slots - busy_slots)
                    per_cycle_limit = MAX_ORDERS_PER_CYCLE_PRE
                else:
                    target_slots = MAX_OPEN_POSITIONS
                    slots_left = min(target_slots, allowed_slots(api))
                    per_cycle_limit = target_slots

                # بناء المرشحين
                candidates = []
                for symbol in SYMBOLS:
                    mom = momentum_for_last_min(symbol)
                    if mom is None:
                        continue
                    states = guard_states(symbol, open_map)
                    allowed, _ = can_open_new_long(symbol, states, session)

                    bid = ask = 0.0
                    spread_bps = None
                    dollar_vol = 0.0
                    try:
                        q = api.get_latest_quote(symbol)
                        bid = float(getattr(q, "bidprice", getattr(q, "bid_price", 0)) or 0)
                        ask = float(getattr(q, "askprice", getattr(q, "ask_price", 0)) or 0)
                        if ask > 0 and bid > 0 and ask > bid:
                            spread_bps = (ask - bid) / ask * 10000
                        bars = api.get_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), limit=1).df
                        if not bars.empty:
                            last = bars.iloc[-1]
                            dollar_vol = float(last["close"] * last["volume"])
                    except Exception:
                        pass

                    log.info(
                        f"{symbol}: mom={mom:.5f} thr={MOMENTUM_THRESHOLD} | "
                        f"guards pos={states['has_pos']} open_order={states['has_open_order']} "
                        f"sold_today={states['sold_today']} cooldown={states['cooldown']} "
                        f"maxpos={states['max_positions_reached']} | "
                        f"pre_filters spread_bps={None if spread_bps is None else round(spread_bps,1)} "
                        f"dollar_vol={int(dollar_vol)}"
                    )

                    if session == "pre":
                        if (spread_bps is None) or (spread_bps > MAX_SPREAD_BPS):
                            continue
                        if dollar_vol < MIN_PREMARKET_DOLLAR_VOL:
                            continue

                    if mom < MOMENTUM_THRESHOLD:
                        continue
                    if not allowed:
                        continue
                    price = last_trade_price(symbol)
                    if not price or price <= 0:
                        continue
                    candidates.append((symbol, mom, price))

                candidates.sort(key=lambda x: x[1], reverse=True)
                ordered_syms = [c[0] for c in candidates]
                best_preview = ordered_syms[:TOP_K]

                open_syms = set(list_open_positions_symbols())
                symbols_to_open: List[str] = []
                for s in ordered_syms:
                    if s in open_syms:
                        continue
                    if len(symbols_to_open) >= slots_left:
                        break
                    symbols_to_open.append(s)

                if session == "pre" and symbols_to_open:
                    symbols_to_open = symbols_to_open[:min(len(symbols_to_open), per_cycle_limit)]

                log.info(
                    f"BEST={best_preview} | open={list(open_syms)} "
                    f"| to_open={symbols_to_open} | slots_left={slots_left} | target_slots={target_slots}"
                )

                if symbols_to_open:
                    if ALLOCATE_FROM_CASH:
                        total_cash = get_cash_balance(strict_cash_only=STRICT_CASH_ONLY)
                        budgets = plan_budgets_for_opens(
                            total_cash, len(open_syms), len(symbols_to_open), target_slots
                        )
                        if not budgets:
                            budgets = [FALLBACK_NOTIONAL_PER_TRADE for _ in symbols_to_open]
                    else:
                        budgets = [FALLBACK_NOTIONAL_PER_TRADE for _ in symbols_to_open]

                    remaining_cash_for_cycle = get_cash_balance(strict_cash_only=STRICT_CASH_ONLY)
                    reserve_amount = remaining_cash_for_cycle * CASH_RESERVE_PCT
                    remaining_cash_for_cycle = max(0.0, remaining_cash_for_cycle - reserve_amount)

                    orders_sent_this_cycle = 0

                    for sym, budget in zip(symbols_to_open, budgets):
                        if orders_sent_this_cycle >= per_cycle_limit:
                            break
                        if allowed_slots(api) <= 0:
                            break
                        if session == "pre":
                            if not _can_submit_now():
                                log.info("[PRE] cooldown active, skip this cycle.")
                                break
                        else:
                            time.sleep(0.3)

                        budget = min(budget, remaining_cash_for_cycle)
                        if budget <= 1.0:
                            continue

                        record_today_sells(api, SYMBOLS)

                        # لو NO_REENTRY_TODAY=True فقط وقتها نقفل إعادة الشراء في نفس الجلسة
                        if NO_REENTRY_TODAY and session == "regular" and (
                            sold_regular_today(sym) or sym in recent_regular_sell_symbols(10)
                        ):
                            log.info(f"[SKIP BUY] {sym}: locked for regular session (NO_REENTRY_TODAY)")
                            continue

                        cancel_symbol_open_buy_orders(sym)
                        qty, price = compute_qty_for_budget(sym, budget)
                        if qty < 1 or not price:
                            continue

                        trade_ok, ext = can_trade_now()
                        if not trade_ok:
                            continue

                        est_notional = qty * price
                        if est_notional > remaining_cash_for_cycle:
                            max_affordable_qty = int(remaining_cash_for_cycle // price)
                            if max_affordable_qty < 1:
                                continue
                            qty = max_affordable_qty
                            est_notional = qty * price

                        buy_id = None
                        try:
                            global _is_submitting
                            if _is_submitting:
                                continue
                            _is_submitting = True

                            if ext and ENABLE_PREMARKET:
                                buy_id = place_limit_buy_qty_premarket(sym, qty, ref_price=price)
                                if buy_id and not ALLOW_PRE_AUTO_SELL:
                                    record_prebuy(sym)
                                if buy_id and session == "pre":
                                    _mark_submit()
                            else:
                                buy_id = place_market_buy_qty_regular(sym, qty)
                                # ما عاد نربط Trailing بعد الشراء، الخروج فقط عن طريق TP
                        finally:
                            _is_submitting = False

                        if buy_id:
                            remaining_cash_for_cycle = max(0.0, remaining_cash_for_cycle - est_notional)
                            orders_sent_this_cycle += 1

                record_today_sells(api, SYMBOLS)
                elapsed = time.time() - cycle_started
                log.info(f"Cycle done in {elapsed:.2f}s")

        except Exception as e:
            log.error(f"Loop error: {e}")

        total_elapsed = time.time() - cycle_started
        if total_elapsed > MAX_CYCLE_SECONDS:
            log.warning(f"Slow cycle: {total_elapsed:.1f}s (limit {MAX_CYCLE_SECONDS}s)")

        session = current_session_et()
        dynamic_interval = 0.3 if session == "pre" and ENABLE_PREMARKET else INTERVAL_SECONDS
        sleep_until_next_interval(dynamic_interval, cycle_started)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
