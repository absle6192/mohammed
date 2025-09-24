import os
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone

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

# -------- الدخول & الحماية --------
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.00005"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
TOP_K = int(os.getenv("TOP_K", "2"))
ALLOCATE_FROM_CASH = os.getenv("ALLOCATE_FROM_CASH", "true").lower() == "true"
FALLBACK_NOTIONAL_PER_TRADE = float(os.getenv("NOTIONAL_PER_TRADE", "6250"))

# -------- Trailing Stop --------
TRAIL_PCT   = float(os.getenv("TRAIL_PCT", "0.7"))   # 0.7% trailing
TRAIL_PRICE = float(os.getenv("TRAIL_PRICE", "0.0")) # 0 لتعطيله

# -------- إعادة الدخول --------
NO_REENTRY_TODAY = os.getenv("NO_REENTRY_TODAY", "true").lower() == "true"
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))

# -------- دورة التشغيل --------
INTERVAL_SECONDS   = int(os.getenv("INTERVAL_SECONDS", "30"))
MAX_CYCLE_SECONDS  = int(os.getenv("MAX_CYCLE_SECONDS", "20"))

# -------- Extended Hours / Pre-market sell --------
EXTENDED_HOURS = os.getenv("EXTENDED_HOURS", "true").lower() == "true"
AUTO_SELL_BEFORE_OPEN = os.getenv("AUTO_SELL_BEFORE_OPEN", "true").lower() == "true"
SELL_BID_OFFSET_CENTS = int(os.getenv("SELL_BID_OFFSET_CENTS", "2"))  # خصم 2 سنت من أفضل Bid

if not API_KEY or not API_SECRET:
    log.error("Missing API keys in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# =========================
# Helpers (Time)
# =========================
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
def market_open_now() -> bool:
    try:
        clock = api.get_clock()
        return bool(clock.is_open)
    except Exception as e:
        log.error(f"clock error: {e}")
        return True  # fail-open

def last_trade_price(symbol: str) -> Optional[float]:
    try:
        trade = api.get_latest_trade(symbol)
        return float(trade.price)
    except Exception as e:
        log.debug(f"last_trade_price error {symbol}: {e}")
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

def latest_quote(symbol: str):
    try:
        return api.get_latest_quote(symbol)
    except Exception as e:
        log.debug(f"latest_quote error {symbol}: {e}")
        return None

# =========================
# Entry Signal
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
    states = {
        "has_pos": has_open_position(symbol),
        "has_open_order": open_orders.get(symbol, False),
        "sold_today": sold_today(symbol),
        "cooldown": in_cooldown(symbol),
        "max_positions_reached": count_open_positions() >= MAX_OPEN_POSITIONS
    }
    return states

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
# Orders
# =========================
def place_limit_buy_auto(symbol: str, budget: float) -> Optional[str]:
    """Limit Buy عند سعر العرض (Ask) مع extended_hours=True"""
    try:
        quote = latest_quote(symbol)
        ask_price = float(quote.ap) if quote and quote.ap > 0 else last_trade_price(symbol)

        if not ask_price:
            log.warning(f"[SKIP] {symbol} no valid price")
            return None

        qty = int(budget // ask_price)
        if qty < 1:
            log.warning(f"[SKIP] {symbol} budget too small for price={ask_price:.2f}")
            return None

        o = api.submit_order(
            symbol=symbol,
            side="buy",
            type="limit",
            qty=str(qty),
            limit_price=str(round(ask_price, 2)),
            time_in_force="day",
            extended_hours=EXTENDED_HOURS
        )
        log.info(f"[BUY] {symbol} qty={qty} @ {ask_price:.2f}")
        return o.id
    except Exception as e:
        log.error(f"Limit BUY failed {symbol}: {e}")
        return None

def place_limit_sell_auto(symbol: str, qty: float) -> Optional[str]:
    """
    يضع أمر بيع Limit قبل/بعد السوق عند أفضل Bid - خصم بسيط (SELL_BID_OFFSET_CENTS).
    يستخدم extended_hours=True لضمان التنفيذ في الفترات الممتدة.
    """
    try:
        if qty <= 0:
            return None

        quote = latest_quote(symbol)
        if not quote or float(quote.bp) <= 0:
            # كحل بديل، نستخدم آخر سعر تداول
            ref = last_trade_price(symbol)
            if not ref:
                log.warning(f"[SKIP SELL] {symbol} no bid/last price")
                return None
            px = max(0.01, ref - SELL_BID_OFFSET_CENTS / 100.0)
        else:
            px = max(0.01, float(quote.bp) - SELL_BID_OFFSET_CENTS / 100.0)

        o = api.submit_order(
            symbol=symbol,
            side="sell",
            type="limit",
            qty=str(int(qty)),
            limit_price=str(round(px, 2)),
            time_in_force="day",
            extended_hours=EXTENDED_HOURS
        )
        log.info(f"[SELL-LIMIT] {symbol} qty={int(qty)} @ {px:.2f} (ext-hours)")
        return o.id
    except Exception as e:
        log.error(f"Limit SELL failed {symbol}: {e}")
        return None

def place_trailing_stop(symbol: str, qty: float) -> Optional[str]:
    try:
        if TRAIL_PRICE > 0:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_price=str(TRAIL_PRICE), qty=str(qty),
                extended_hours=EXTENDED_HOURS
            )
        else:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_percent=str(TRAIL_PCT), qty=str(qty),
                extended_hours=EXTENDED_HOURS
            )
        log.info(f"[TRAIL] {symbol} qty={qty}")
        return o.id
    except Exception as e:
        log.error(f"TRAIL failed {symbol}: {e}")
        return None

def try_attach_trailing_stop(symbol: str):
    try:
        pos = api.get_position(symbol)
        qty = float(pos.qty)
        if qty > 0:
            place_trailing_stop(symbol, qty)
    except Exception as e:
        log.debug(f"attach trail skipped {symbol}: {e}")

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

# =========================
# Pre-market exit helper
# =========================
def premarket_mass_exit(open_map: Dict[str, bool]):
    """
    إذا السوق مغلق (قبل الافتتاح) و AUTO_SELL_BEFORE_OPEN=True:
    ضع أوامر بيع Limit لكل المراكز المفتوحة التي ليس عليها أوامر مفتوحة.
    """
    if not AUTO_SELL_BEFORE_OPEN:
        return
    if market_open_now():
        return

    try:
        positions = api.list_positions()
    except Exception as e:
        log.warning(f"list_positions failed: {e}")
        return

    for p in positions:
        sym = p.symbol
        if open_map.get(sym, False):
            continue
        try:
            qty = float(p.qty)
        except Exception:
            continue
        if qty <= 0:
            continue
        # ألغِ أي أمر قديم ثم ضع بيع ليمت ممتد الساعات
        cancel_symbol_open_orders(sym)
        place_limit_sell_auto(sym, qty)
        time.sleep(0.5)

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
        f"interval_s={INTERVAL_SECONDS} "
        f"extended_hours={EXTENDED_HOURS} auto_sell_premarket={AUTO_SELL_BEFORE_OPEN}"
    )

    log.info("Bot started.")
    while True:
        cycle_started = time.time()
        try:
            heartbeat("Cycle begin")

            record_today_sells(api, SYMBOLS)
            open_map = open_orders_map()

            # ===== NEW: بيع قبل الافتتاح =====
            premarket_mass_exit(open_map)

            # ==== 1) حساب المومنتم لكل سهم واختيار أفضل K ====
            candidates = []
            for symbol in SYMBOLS:
                mom = momentum_for_last_min(symbol)
                if mom is None:
                    log.info(f"{symbol}: ❌ no bar data; skip")
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

            # ==== 2) حساب عدد الفتحات المتاحة ====
            currently_open_syms = set(list_open_positions_symbols())
            open_count = len(currently_open_syms)
            slots_left = max(0, min(MAX_OPEN_POSITIONS, TOP_K) - open_count)
            symbols_to_open = [s for s in best if s not in currently_open_syms][:slots_left]

            log.info(f"BEST={best} | currently_open={list(currently_open_syms)} | to_open={symbols_to_open} | slots_left={slots_left}")

            # ==== 3) الميزانية لكل مركز ====
            if symbols_to_open:
                if ALLOCATE_FROM_CASH:
                    cash_or_bp = get_buying_power_cash()
                    per_budget = (cash_or_bp / len(symbols_to_open)) if cash_or_bp > 0 else FALLBACK_NOTIONAL_PER_TRADE
                else:
                    per_budget = FALLBACK_NOTIONAL_PER_TRADE

                log.info(f"Per-position budget ≈ ${per_budget:.2f}")

                # ==== 4) تنفيذ الشراء وتعليق Trailing ====
                for sym in symbols_to_open:
                    cancel_symbol_open_orders(sym)
                    buy_id = place_limit_buy_auto(sym, per_budget)
                    if buy_id:
                        time.sleep(1.5)
                        try_attach_trailing_stop(sym)

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
