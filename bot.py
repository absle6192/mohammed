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

# entry & risk
# تم تقليل شرط الدخول: 0.05% على شمعة الدقيقة
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.0002"))
NOTIONAL_PER_TRADE = float(os.getenv("NOTIONAL_PER_TRADE", "6250"))   # ميزانية الصفقة (سنحوّلها إلى qty)
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "8"))

# trailing stop
TRAIL_PCT   = float(os.getenv("TRAIL_PCT", "0.7"))   # 0.7% trailing
TRAIL_PRICE = float(os.getenv("TRAIL_PRICE", "0.0")) # 0 لتعطيله، نستخدم TRAIL_PCT غالباً

# re-entry guards
NO_REENTRY_TODAY = os.getenv("NO_REENTRY_TODAY", "true").lower() == "true"
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))  # بعد أي بيع على نفس السهم

# loop cadence & watchdog
INTERVAL_SECONDS   = int(os.getenv("INTERVAL_SECONDS", "60"))  # دورة كل دقيقة
MAX_CYCLE_SECONDS  = int(os.getenv("MAX_CYCLE_SECONDS", "20")) # تحذير لو الدورة طولت

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
sold_registry: Dict[str, datetime] = {}  # آخر وقت بيع لكل سهم (UTC)

def record_today_sells(api: REST, symbols: List[str]) -> None:
    """يسجل أحدث عمليات البيع لليوم الحالي لكل سهم في القائمة."""
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
        # fail-open لإبقاء اللوب حي في حال تعثر API
        return True

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
    يحسب المومنتم للدقيقة الأخيرة: (close - open) / open
    يعيد None إذا لم تتوفر بيانات.
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
    """يرجع حالة الحمايات لكل سهم لغايات اللوق."""
    states = {
        "has_pos": has_open_position(symbol),
        "has_open_order": open_orders.get(symbol, False),
        "sold_today": sold_today(symbol),
        "cooldown": in_cooldown(symbol),
        "max_positions_reached": count_open_positions() >= MAX_OPEN_POSITIONS
    }
    return states

def can_open_new_long(symbol: str, states: Dict[str, bool]) -> Tuple[bool, str]:
    """يرجع (مسموح, سبب_المنع)"""
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
# Place Orders (QTY-based to enable trailing stop)
# =========================
def place_market_buy_qty(symbol: str, qty: int) -> Optional[str]:
    try:
        o = api.submit_order(
            symbol=symbol,
            side="buy",
            type="market",
            time_in_force="day",
            qty=str(qty)  # whole shares
        )
        log.info(f"[BUY] {symbol} qty={qty}")
        return o.id
    except Exception as e:
        log.error(f"BUY failed {symbol}: {e}")
        return None

def place_trailing_stop(symbol: str, qty: float) -> Optional[str]:
    try:
        if TRAIL_PRICE > 0:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_price=str(TRAIL_PRICE), qty=str(qty)
            )
        else:
            o = api.submit_order(
                symbol=symbol, side="sell", type="trailing_stop",
                time_in_force="day", trail_percent=str(TRAIL_PCT), qty=str(qty)
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
# Main loop
# =========================
def main_loop():
    # طباعة إعدادات البداية لمرة واحدة
    log.info(f"SYMBOLS LOADED: {SYMBOLS}")
    log.info(f"SETTINGS: thr={MOMENTUM_THRESHOLD} "
             f"budget={NOTIONAL_PER_TRADE} "
             f"max_pos={MAX_OPEN_POSITIONS} "
             f"trail_pct={TRAIL_PCT} trail_price={TRAIL_PRICE} "
             f"no_reentry_today={NO_REENTRY_TODAY} cooldown_min={COOLDOWN_MINUTES} "
             f"interval_s={INTERVAL_SECONDS}")

    log.info("Bot started.")
    while True:
        cycle_started = time.time()
        try:
            if not market_open_now():
                heartbeat("Market closed - sleeping")
            else:
                heartbeat("Market open - cycle begin")

                # تحديث سجل البيعات & Snapshot للأوامر المفتوحة
                record_today_sells(api, SYMBOLS)
                open_map = open_orders_map()

                # إدارة الدخول (الخروج عبر Trailing Stop)
                for symbol in SYMBOLS:
                    mom = momentum_for_last_min(symbol)
                    if mom is None:
                        log.info(f"{symbol}: ❌ no bar data / bad open; skip")
                        continue

                    states = guard_states(symbol, open_map)
                    allowed, reason = can_open_new_long(symbol, states)

                    # لوق تفصيلي قبل قرار الدخول
                    log.info(
                        f"{symbol}: mom={mom:.5f} thr={MOMENTUM_THRESHOLD} | "
                        f"guards: pos={states['has_pos']}, "
                        f"open_order={states['has_open_order']}, "
                        f"sold_today={states['sold_today']}, "
                        f"cooldown={states['cooldown']}, "
                        f"maxpos={states['max_positions_reached']}"
                    )

                    if mom < MOMENTUM_THRESHOLD:
                        log.info(f"{symbol}: ✋ momentum below threshold")
                        continue
                    if not allowed:
                        log.info(f"{symbol}: ✋ guard blocked -> {reason}")
                        continue

                    # حساب الكمية الصحيحة بناءً على الميزانية
                    price = last_trade_price(symbol)
                    if not price or price <= 0:
                        log.warning(f"[SKIP] {symbol} no price available.")
                        continue

                    qty = int(NOTIONAL_PER_TRADE // price)  # whole shares
                    if qty < 1:
                        log.warning(f"[SKIP] {symbol} price too high for budget ${NOTIONAL_PER_TRADE:.2f} (last={price:.2f})")
                        continue

                    log.info(f"{symbol}: ✅ ENTRY signal confirmed at price={price:.2f}, qty={qty}")
                    cancel_symbol_open_orders(symbol)  # safety

                    buy_id = place_market_buy_qty(symbol, qty)
                    if buy_id:
                        # نمهل قليلاً حتى تتحدث الكمية ثم نربط Trailing
                        time.sleep(1.5)
                        try_attach_trailing_stop(symbol)

                # تحديث سجل البيعات مرة أخرى لالتقاط أي خروج سريع
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
