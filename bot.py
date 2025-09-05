import os
import time
import math
import uuid
import logging
from datetime import datetime, timedelta, timezone

from alpaca_trade_api.rest import REST, TimeFrame, TimeFrameUnit

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

# ---------- API (from ENV ONLY) ----------
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_FEED  = os.getenv("APCA_API_DATA_FEED", "iex")  # أنت مخلّيها sip في Render

if not API_KEY or not API_SECRET:
    log.error("Missing API keys. Set APCA_API_KEY_ID / APCA_API_SECRET_KEY in environment.")
    raise SystemExit(1)

api = REST(API_KEY, API_SECRET, BASE_URL)

# ---------- CONFIG (INLINE) ----------
SYMBOLS = ["AAPL","MSFT","AMZN","NVDA","AMD","TSLA","META","GOOGL"]

TOTAL_CAPITAL       = 50000
NUM_SLOTS           = 8
PER_TRADE_DOLLARS   = TOTAL_CAPITAL // NUM_SLOTS

MOMENTUM_LOOKBACK_MIN = 1
MOMENTUM_THRESHOLD    = 0.001     # +0.3% على آخر دقيقة
TAKE_PROFIT_PCT       = 0.012     # +1.2%
STOP_LOSS_PCT         = 0.010     # -1.0%
USE_TRAILING_STOP     = False
TRAIL_PCT             = 0.008     # 0.8% (لو فعلت التريلينغ)

MIN_DOLLAR_VOLUME_1M  = 200000    # سيولة دنيا لشمعة 1د
MAX_SPREAD_PCT        = 0.004     # 0.4% أقصى سبريد

FLATTEN_BEFORE_CLOSE_MIN = 10     # لا فتح صفقات جديدة قبل الإغلاق بـ N دقيقة
POLL_SECONDS             = 2.0

# ---------- Day state ----------
locked_today = set()
_day_key = None

def now_utc(): return datetime.now(timezone.utc)

def reset_day_if_needed():
    global _day_key, locked_today
    k = now_utc().date().isoformat()
    if k != _day_key:
        _day_key = k
        locked_today = set()
        log.info("New trading day -> cleared locks.")

def market_open() -> bool:
    try:
        return bool(api.get_clock().is_open)
    except Exception as e:
        log.warning(f"clock error: {e}")
        return True

def minutes_to_close() -> int:
    try:
        nxt = api.get_clock().next_close
        return max(0, int((nxt - now_utc()).total_seconds() // 60))
    except Exception:
        return 999

# ---------- Retry helper ----------
def _retryable(e: Exception) -> bool:
    s = str(e).lower()
    return ("500" in s) or ("timeout" in s) or ("temporar" in s) or ("connection" in s)

def submit_with_retry(fn, what: str, tries=4, base=1.2):
    last = None
    for i in range(tries):
        try:
            r = fn()
            if i: log.info(f"{what}: succeeded on retry #{i}")
            return r
        except Exception as e:
            last = e
            if _retryable(e) and i < tries-1:
                sleep = base*(i+1)
                log.warning(f"{what}: retryable error -> retry in {sleep:.1f}s | {e}")
                time.sleep(sleep)
                continue
            break
    log.error(f"{what}: failed after retries -> {last}")
    raise last

# ---------- Orders & positions ----------
def list_open_orders(sym: str):
    try:
        return [o for o in api.list_orders(status="open") if o.symbol == sym]
    except Exception as e:
        log.warning(f"list_open_orders({sym}) error: {e}")
        return []

def get_qty(sym: str) -> int:
    try:
        for p in api.list_positions():
            if p.symbol == sym:
                return int(p.qty)
        return 0
    except Exception as e:
        log.warning(f"get_qty({sym}) error: {e}")
        return 0

def cancel_child_orders(sym: str):
    for o in list_open_orders(sym):
        try:
            api.cancel_order(o.id)
            log.info(f"{sym}: canceled {o.side} {o.type} {o.id}")
        except Exception as e:
            log.warning(f"{sym}: cancel {o.id} failed: {e}")

def has_active_stop(sym: str) -> bool:
    for o in list_open_orders(sym):
        if o.side == "sell" and o.type in ("stop","stop_limit","trailing_stop"):
            return True
    return False

def lock_for_today(sym: str):
    locked_today.add(sym)
    log.info(f"{sym}: sold today -> LOCKED for the rest of the day.")

# ---------- Market data & signal ----------
def _bars_1m(sym: str, limit=3):
    end = now_utc(); start = end - timedelta(minutes=limit+1)
    return api.get_bars(sym, TimeFrame(TimeFrameUnit.Minute,1),
                        start.isoformat(), end.isoformat(),
                        feed=DATA_FEED, limit=limit)

def last_trade(sym: str):
    try: return api.get_latest_trade(sym, feed=DATA_FEED)
    except Exception as e:
        log.warning(f"{sym}: last_trade error: {e}"); return None

def last_quote(sym: str):
    try: return api.get_latest_quote(sym, feed=DATA_FEED)
    except Exception as e:
        log.warning(f"{sym}: last_quote error: {e}"); return None

def calc_momentum(sym: str) -> float:
    try:
        bars = _bars_1m(sym, 3)
        if not bars or len(bars) < 2: return 0.0
        a, b = bars[-2].c, bars[-1].c
        return (b-a)/a if a>0 else 0.0
    except Exception as e:
        log.warning(f"{sym}: momentum error: {e}")
        return 0.0

def spread_ok(sym: str) -> bool:
    q = last_quote(sym)
    if not q or q.ask_price<=0 or q.bid_price<=0: return False
    return (q.ask_price - q.bid_price)/q.ask_price <= MAX_SPREAD_PCT

def liquidity_ok(sym: str) -> bool:
    try:
        b = _bars_1m(sym, 2)
        if not b: return False
        last = b[-1]
        dollar = (last.v or 0) * ((last.h + last.l)/2.0)
        return dollar >= MIN_DOLLAR_VOLUME_1M
    except Exception:
        return False

# ---------- Place orders ----------
def place_bracket_buy(sym: str, dollars: float):
    lt = last_trade(sym)
    if not lt or lt.price <= 0:
        raise RuntimeError(f"{sym}: no last trade price.")
    qty = max(1, int(dollars // lt.price))
    tp  = round(lt.price*(1+TAKE_PROFIT_PCT), 2)
    slp = round(lt.price*(1-STOP_LOSS_PCT),   2)

    def _submit():
        return api.submit_order(
            symbol=sym, qty=qty, side="buy", type="market", time_in_force="day",
            order_class="bracket",
            take_profit={"limit_price": tp},
            stop_loss=({"stop_price": slp} if not USE_TRAILING_STOP
                       else {"trail_percent": round(TRAIL_PCT*100,3)}),
            client_order_id=str(uuid.uuid4())
        )
    submit_with_retry(_submit, f"{sym} BUY (bracket)")
    log.info(f"{sym}: BUY {qty} @~{lt.price:.2f} -> TP {tp} / {'TRAIL '+str(TRAIL_PCT*100)+'%' if USE_TRAILING_STOP else 'SL '+str(slp)}")

def place_protective_stop(sym: str, qty: int, stop_price: float):
    if qty <= 0: return
    def _submit():
        return api.submit_order(
            symbol=sym, qty=qty, side="sell", type="stop",
            stop_price=round(stop_price,2), time_in_force="day",
            client_order_id=str(uuid.uuid4())
        )
    submit_with_retry(_submit, f"{sym} protective STOP @{stop_price:.2f}")
    log.info(f"{sym}: placed protective STOP @{stop_price:.2f}")

# ---------- Housekeeping ----------
def after_fill_housekeeping(sym: str):
    qty = get_qty(sym)

    # لو مافي مركز: الغِ الأوامر اليتيمة واقفل السهم لليوم
    if qty <= 0:
        cancel_child_orders(sym)
        lock_for_today(sym)
        return

    # لو مافي STOP فعّال (أحيانًا يفشل إنشاء أولي بسبب 500)، نضيف وقف وقائي
    if not has_active_stop(sym):
        lt = last_trade(sym)
        if lt and lt.price>0:
            sp = lt.price*(1-STOP_LOSS_PCT)
            log.warning(f"{sym}: No active STOP -> placing protective at {sp:.2f}")
            try: place_protective_stop(sym, qty, sp)
            except Exception as e: log.error(f"{sym}: protective stop failed: {e}")

# ---------- Core loop ----------
def open_positions_count():
    try: return len(api.list_positions())
    except Exception as e:
        log.warning(f"list_positions error: {e}"); return 0

def eligible_to_buy(sym: str) -> bool:
    return (sym not in locked_today and
            spread_ok(sym) and
            liquidity_ok(sym) and
            calc_momentum(sym) >= MOMENTUM_THRESHOLD)

def main():
    log.info(f"Bot started | CAPITAL={TOTAL_CAPITAL} | SLOTS={NUM_SLOTS} | PER_TRADE={PER_TRADE_DOLLARS} | FEED={DATA_FEED}")

    while True:
        try:
            reset_day_if_needed()

            if not market_open():
                time.sleep(5); continue

            if minutes_to_close() <= FLATTEN_BEFORE_CLOSE_MIN:
                time.sleep(POLL_SECONDS); continue

            # مزامنة بعد أي تنفيذ يدوي
            for s in SYMBOLS:
                after_fill_housekeeping(s)

            # دخول صفقات جديدة حتى امتلاء الـ Slots
            slots_left = max(0, NUM_SLOTS - open_positions_count())
            if slots_left > 0:
                for s in SYMBOLS:
                    if slots_left <= 0: break
                    if get_qty(s) > 0 or s in locked_today: continue
                    if eligible_to_buy(s):
                        try:
                            place_bracket_buy(s, PER_TRADE_DOLLARS)
                            slots_left -= 1
                        except Exception as e:
                            log.warning(f"{s}: buy failed: {e}")

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Interrupted, exiting."); break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(2.0)

if __name__ == "__main__":
    main()
