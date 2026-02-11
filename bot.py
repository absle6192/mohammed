import os
import time
import math
import random
import requests
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
)

# ===================== env helpers =====================
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

def env_int(name: str, default: str) -> int:
    return int(env(name, default))

def env_float(name: str, default: str) -> float:
    return float(env(name, default))

def env_bool(name: str, default: str = "false") -> bool:
    v = env(name, default).lower()
    return v in ("1", "true", "yes", "y", "on")

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

# ===================== telegram (with throttle) =====================
_LAST_TG_SENT: dict[str, float] = {}

def tg_throttle(key: str, cooldown_sec: int) -> bool:
    now = time.time()
    last = _LAST_TG_SENT.get(key, 0.0)
    if now - last >= cooldown_sec:
        _LAST_TG_SENT[key] = now
        return True
    return False

def send_telegram(msg: str, throttle_key: str | None = None, cooldown_sec: int = 120) -> None:
    # Optional throttle to avoid spam
    if throttle_key is not None:
        if not tg_throttle(throttle_key, cooldown_sec):
            return

    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print("[TELEGRAM_FAIL]", r.status_code, r.text, flush=True)
    except Exception as e:
        print("[TELEGRAM_EXCEPTION]", repr(e), flush=True)

# ===================== alpaca safe wrappers =====================
def _looks_like_error_dict(d: dict) -> bool:
    err_keys = {"code", "message", "error", "status", "status_code"}
    return any(k in d for k in err_keys)

def _unwrap_data_or_raise(resp, label: str):
    """
    alpaca-py expected: resp.data is a dict keyed by symbol.
    BUT sometimes returns the dict directly (already unwrapped),
    especially for latest quote endpoints.
    """
    if resp is None:
        raise RuntimeError(f"{label}: None response")

    if isinstance(resp, dict):
        # If it looks like an error dict -> raise
        if _looks_like_error_dict(resp) and not any(k.isalpha() and len(k) <= 6 for k in resp.keys()):
            code = resp.get("code") or resp.get("status") or resp.get("status_code")
            msg = resp.get("message") or resp.get("error") or str(resp)
            raise RuntimeError(f"{label}: dict error code={code} msg={msg}")

        # Otherwise: it's probably actual data dict keyed by symbol
        return resp

    if hasattr(resp, "data"):
        return resp.data

    raise RuntimeError(f"{label}: missing .data (type={type(resp)})")

def call_with_retry(fn, label: str, retries: int = 3, base_sleep: float = 0.8):
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            sleep_s = base_sleep * (2 ** i) + random.uniform(0, 0.25)
            time.sleep(sleep_s)
    raise last_err

# ===================== market data =====================
def get_last_two_closed_1m_bars(data_client: StockHistoricalDataClient, sym: str):
    """
    Return (prev_closed, last_closed) using 1m bars.
    We take [-3] and [-2] to avoid the currently-forming bar.
    """
    def _do():
        req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, limit=3)
        return data_client.get_stock_bars(req)

    resp = call_with_retry(_do, label=f"bars:{sym}", retries=3, base_sleep=0.8)
    data = _unwrap_data_or_raise(resp, label=f"bars:{sym}")
    bars = data.get(sym, [])

    if len(bars) < 3:
        return None, None
    return bars[-3], bars[-2]

def get_latest_quote(data_client: StockHistoricalDataClient, sym: str):
    def _do():
        req = StockLatestQuoteRequest(symbol_or_symbols=sym)
        return data_client.get_stock_latest_quote(req)

    resp = call_with_retry(_do, label=f"quote:{sym}", retries=3, base_sleep=0.6)
    data = _unwrap_data_or_raise(resp, label=f"quote:{sym}")
    return data.get(sym)

# ===================== quote helpers (support dict OR object) =====================
def qget(q, key: str, default=None):
    if q is None:
        return default
    if isinstance(q, dict):
        return q.get(key, default)
    return getattr(q, key, default)

# ===================== trading helpers =====================
def round_price(p: float) -> float:
    return round(p, 2)

def compute_qty_from_notional(notional_usd: float, ref_price: float) -> int:
    qty = math.floor(notional_usd / max(ref_price, 0.01))
    return max(qty, 0)

def place_order(
    trading: TradingClient,
    sym: str,
    side: OrderSide,
    notional_usd: float,
    entry_type: str,
    limit_price: float | None = None,
) -> str:
    entry_type = entry_type.upper().strip()
    if entry_type == "MARKET":
        if limit_price is None:
            raise RuntimeError("limit_price(ref) required to compute qty for MARKET fallback")
        qty = compute_qty_from_notional(notional_usd, limit_price)
        if qty <= 0:
            raise RuntimeError(f"qty computed <=0 for {sym} (market) ref {limit_price}")

        req = MarketOrderRequest(
            symbol=sym,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        o = trading.submit_order(req)
        return o.id

    if limit_price is None:
        raise RuntimeError("limit_price required for LIMIT orders")

    qty = compute_qty_from_notional(notional_usd, limit_price)
    if qty <= 0:
        raise RuntimeError(f"qty computed <= 0 for {sym} at price {limit_price}")

    req = LimitOrderRequest(
        symbol=sym,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=round_price(limit_price),
    )
    o = trading.submit_order(req)
    return o.id

def cancel_all_open_orders(trading: TradingClient) -> int:
    try:
        orders = trading.get_orders(status="open")
    except Exception:
        orders = []
    n = 0
    for o in orders:
        try:
            trading.cancel_order_by_id(o.id)
            n += 1
        except Exception as e:
            print("[CANCEL_FAIL]", getattr(o, "id", "?"), repr(e), flush=True)
    return n

def close_position_market(trading: TradingClient, sym: str) -> None:
    trading.close_position(sym)

# ===================== filters =====================
def candle_passes_filters(last_bar, direction: str, body_min: float, close_pos_min: float, wick_max: float) -> bool:
    o = float(last_bar.open)
    c = float(last_bar.close)
    h = float(last_bar.high)
    l = float(last_bar.low)

    rng = max(h - l, 1e-9)
    body = abs(c - o)
    body_pct = body / rng

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    upper_wick_pct = upper_wick / rng
    lower_wick_pct = lower_wick / rng

    close_pos = (c - l) / rng
    close_pos_short = (h - c) / rng

    direction = direction.upper()
    if direction == "LONG":
        return (body_pct >= body_min) and (close_pos >= close_pos_min) and (upper_wick_pct <= wick_max)
    else:
        return (body_pct >= body_min) and (close_pos_short >= close_pos_min) and (lower_wick_pct <= wick_max)

def volume_passes_filter(last_bar, prev_bar, min_vol_ratio: float) -> bool:
    v_last = float(last_bar.volume)
    v_prev = float(prev_bar.volume)
    avg = max((v_last + v_prev) / 2.0, 1.0)
    return (v_last / avg) >= min_vol_ratio

def quote_passes_filters(q, direction: str, max_spread_pct: float, min_imbalance: float) -> tuple[bool, str]:
    if q is None:
        return False, "no_quote"

    bid = float(qget(q, "bid_price", 0.0) or 0.0)
    ask = float(qget(q, "ask_price", 0.0) or 0.0)
    bid_sz = float(qget(q, "bid_size", 0.0) or 0.0)
    ask_sz = float(qget(q, "ask_size", 0.0) or 0.0)

    if bid <= 0 or ask <= 0 or ask <= bid:
        return False, f"bad_bidask bid={bid} ask={ask}"

    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid

    if spread_pct > max_spread_pct:
        return False, f"spread_too_wide {spread_pct:.4f}"

    imb = (bid_sz + 1.0) / (ask_sz + 1.0)

    direction = direction.upper()
    if direction == "LONG":
        if imb < min_imbalance:
            return False, f"imb_low {imb:.2f}"
    else:
        if (1.0 / imb) < min_imbalance:
            return False, f"imb_not_short {imb:.2f}"

    return True, f"ok spread={spread_pct:.4f} imb={imb:.2f}"

# ===================== Arabic formatting helpers =====================
def fmt_dir_ar(direction: str) -> str:
    d = direction.upper()
    return "شراء (LONG)" if d == "LONG" else "شورت (SHORT)"

def fmt_money(x: float) -> str:
    try:
        return f"${x:,.2f}"
    except Exception:
        return str(x)

def build_signal_msg_ar(
    direction: str,
    sym: str,
    last_close: float,
    prev_close: float,
    entry_type: str,
    suggested_price: float,
    max_spread_pct: float,
    min_vol_ratio: float,
    min_imbalance: float,
    candle_body_min: float,
    candle_close_pos_min: float,
    auto_trading: bool,
    in_alert_only: bool,
) -> str:
    note = "AUTO_TRADING=OFF ✅" if (not auto_trading) else "تم الوصول للحد اليومي → تنبيهات فقط ✅"
    return (
        "📣 إشارة (بدون تنفيذ تلقائي)\n"
        f"🎯 {fmt_dir_ar(direction)} | {sym}\n"
        f"📌 إغلاق مرجعي: {last_close:.2f} (السابق {prev_close:.2f})\n"
        f"💡 دخول مقترح: {entry_type} @ {round_price(suggested_price):.2f}\n"
        f"🧪 شروط السيولة/الفلترة:\n"
        f"• Spread ≤ {max_spread_pct:.3f}\n"
        f"• VOLx ≥ {min_vol_ratio}\n"
        f"• Imbalance ≥ {min_imbalance}\n"
        f"• جسم الشمعة ≥ {candle_body_min}\n"
        f"• موضع الإغلاق ≥ {candle_close_pos_min}\n"
        f"🧷 ملاحظة: {note}"
    )

def build_entry_msg_ar(direction: str, sym: str, entry_type: str, last_close: float, prev_close: float,
                      stop_loss_usd: float, auto_entries_today: int, max_auto_entries_per_day: int, oid: str) -> str:
    return (
        "✅ تم إرسال أمر دخول\n"
        f"🎯 {fmt_dir_ar(direction)} | {sym}\n"
        f"🧾 نوع الأمر: {entry_type}\n"
        f"📌 إغلاق مرجعي: {last_close:.2f} (السابق {prev_close:.2f})\n"
        f"🛑 ستوب للصفقة: -{fmt_money(abs(stop_loss_usd))}\n"
        f"📊 عدد الدخول اليوم: {auto_entries_today}/{max_auto_entries_per_day}\n"
        f"🆔 Order id: {oid}"
    )

# ===================== main =====================
def main():
    symbols = [s.strip().upper() for s in env("SYMBOLS").split(",") if s.strip()]
    interval_sec = env_int("INTERVAL_SEC", "30")

    paper = env_bool("ALPACA_PAPER", "true")
    notional_usd = env_float("NOTIONAL_USD", "25000")
    stop_loss_usd = env_float("STOP_LOSS_USD", "150")
    daily_target_usd = env_float("DAILY_TARGET_USD", "300")

    daily_mode = env("DAILY_TARGET_MODE", "GATE").upper().strip()
    entry_type = env("ENTRY_TYPE", "MARKET").upper().strip()

    limit_offset_bps = env_float("LIMIT_OFFSET_BPS", "1")
    offset = limit_offset_bps / 10000.0

    start_delay_sec = env_int("START_DELAY_SEC", "120")

    enable_confirm = env_bool("ENABLE_CONFIRM", "true")
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.2")
    max_spread_pct = env_float("MAX_SPREAD_PCT", "0.002")
    min_imbalance = env_float("MIN_IMBALANCE", "1.2")
    candle_body_min = env_float("CANDLE_BODY_MIN", "0.5")
    candle_close_pos_min = env_float("CANDLE_CLOSE_POS_MIN", "0.7")
    wick_max = env_float("WICK_MAX", "0.35")

    max_open_positions = env_int("MAX_OPEN_POSITIONS", "2")
    cooldown_after_close_sec = env_int("COOLDOWN_AFTER_CLOSE_SEC", "90")

    max_auto_entries_per_day = env_int("MAX_AUTO_ENTRIES_PER_DAY", "3")
    alert_only_after_limit = env_bool("ALERT_ONLY_AFTER_LIMIT", "true")
    alert_cooldown_sec = env_int("ALERT_COOLDOWN_SEC", "60")

    # ✅ AUTO TRADING SWITCH
    auto_trading = env_bool("AUTO_TRADING", "true")

    # ✅ NEW: HEARTBEAT (طمأنة أنه شغال)
    heartbeat_sec = env_int("HEARTBEAT_SEC", "300")  # افتراضي 5 دقائق
    heartbeat_enabled = env_bool("HEARTBEAT_ON", "true")
    heartbeat_log = env_bool("HEARTBEAT_LOG", "false")  # لو تبي يطبع باللوق
    scan_log = env_bool("SCAN_LOG", "false")  # لو تبي يطبع كل دورة Scan

    data_client = StockHistoricalDataClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
    )
    trading = TradingClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
        paper=paper,
    )

    trading_day = utc_now().date()
    daily_realized = 0.0
    last_seen_position_qty: dict[str, float] = {}
    last_signal_minute: dict[str, str] = {}
    last_alert_time: dict[str, float] = {}

    halted_for_day = False
    auto_entries_today = 0

    was_open = None
    open_seen_at: datetime | None = None
    ready_to_trade = False
    last_any_close_time: datetime | None = None

    # heartbeat trackers
    last_heartbeat_ts = 0.0
    last_any_signal_ts = 0.0

    send_telegram(
        "🚀 تشغيل البوت (تجريبي PAPER)\n"
        f"📊 الأسهم: {', '.join(symbols)}\n"
        f"⏱ الفاصل: {interval_sec} ثانية\n"
        f"💰 مبلغ الصفقة: ${notional_usd:,.0f}\n"
        f"🛑 ستوب/صفقة: -${abs(stop_loss_usd):,.0f}\n"
        f"🎯 هدف يومي: +${daily_target_usd:,.0f} ({daily_mode})\n"
        f"🧾 نوع الدخول: {entry_type}{'' if entry_type=='MARKET' else f' (إزاحة {limit_offset_bps} bps)'}\n"
        f"🧠 فلترة: {'مفعلة' if enable_confirm else 'متوقفة'} "
        f"(VOLx≥{min_vol_ratio}, spread≤{max_spread_pct:.3f}, imb≥{min_imbalance}, body≥{candle_body_min}, closepos≥{candle_close_pos_min})\n"
        f"🔒 أقصى مراكز مفتوحة: {max_open_positions}\n"
        f"🧊 تهدئة بعد الإغلاق: {cooldown_after_close_sec}s\n"
        f"⏳ بدء بعد الافتتاح: {start_delay_sec}s\n"
        f"🤖 حد الدخول/اليوم: {max_auto_entries_per_day} ثم {'تنبيهات فقط' if alert_only_after_limit else 'توقف'}\n"
        f"🧷 التداول التلقائي: {'ON' if auto_trading else 'OFF (تنبيهات فقط)'}\n"
        f"🕒 UTC: {utc_now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print("[BOOT] started", flush=True)

    while True:
        try:
            now = utc_now()

            if now.date() != trading_day:
                trading_day = now.date()
                daily_realized = 0.0
                halted_for_day = False
                auto_entries_today = 0
                last_signal_minute.clear()
                last_alert_time.clear()
                open_seen_at = None
                ready_to_trade = False
                last_any_close_time = None
                last_heartbeat_ts = 0.0
                last_any_signal_ts = 0.0
                send_telegram(f"🗓 يوم جديد (UTC): {trading_day} — تم تصفير العدادات ✅")

            clock = trading.get_clock()
            is_open = bool(clock.is_open)

            if was_open is None:
                was_open = is_open

            if not is_open:
                if was_open:
                    send_telegram("🛑 السوق أغلق الآن. جاري إلغاء الأوامر المفتوحة…")
                cancel_all_open_orders(trading)
                open_seen_at = None
                ready_to_trade = False
                was_open = is_open
                time.sleep(interval_sec)
                continue

            if (was_open is False) and is_open:
                open_seen_at = now
                ready_to_trade = False
                send_telegram(f"🔔 تم رصد افتتاح السوق.\n⏳ انتظار {start_delay_sec} ثانية قبل أي دخول.")

            was_open = is_open

            if open_seen_at is None:
                open_seen_at = now
                ready_to_trade = False
                send_telegram(f"🔔 السوق مفتوح مسبقًا.\n⏳ انتظار أمان {start_delay_sec} ثانية قبل التداول.")

            elapsed = (now - open_seen_at).total_seconds()
            if elapsed < start_delay_sec:
                time.sleep(min(interval_sec, 10))
                continue
            else:
                if not ready_to_trade:
                    ready_to_trade = True
                    send_telegram("✅ تم تفعيل التداول الآن (انتهى تأخير ما بعد الافتتاح).")

            # cooldown after any close (mostly relevant in auto mode)
            if last_any_close_time is not None:
                if (now - last_any_close_time).total_seconds() < cooldown_after_close_sec:
                    time.sleep(interval_sec)
                    continue

            # Only track positions/stop rules if auto trading is ON
            positions = {}
            open_count = 0
            if auto_trading:
                positions = {p.symbol: p for p in trading.get_all_positions()}
                open_count = len(positions)

                # stop loss per position
                for sym, p in positions.items():
                    try:
                        upl = float(p.unrealized_pl)
                    except Exception:
                        continue
                    if upl <= -abs(stop_loss_usd):
                        send_telegram(f"🛑 ضرب الستوب: {sym}\nالخسارة غير المحققة: {fmt_money(upl)}\nجاري إغلاق المركز الآن…")
                        try:
                            close_position_market(trading, sym)
                            last_any_close_time = utc_now()
                        except Exception as e:
                            print("[STOP_CLOSE_FAIL]", sym, repr(e), flush=True)

                # closure notices
                for sym in list(last_seen_position_qty.keys()):
                    prev_qty = last_seen_position_qty.get(sym, 0.0)
                    if sym not in positions and prev_qty != 0.0:
                        send_telegram(f"✅ تم إغلاق المركز: {sym}")
                        last_seen_position_qty[sym] = 0.0
                        last_any_close_time = utc_now()

                for sym, p in positions.items():
                    try:
                        last_seen_position_qty[sym] = float(p.qty)
                    except Exception:
                        last_seen_position_qty[sym] = 0.0

                # cap open positions
                if open_count >= max_open_positions:
                    time.sleep(interval_sec)
                    continue

                # daily target logic (optional; keep your current behavior)
                unreal_total = 0.0
                for sym, p in positions.items():
                    try:
                        unreal_total += float(p.unrealized_pl)
                    except Exception:
                        pass
                daily_total = daily_realized + unreal_total
                if (not halted_for_day) and (daily_total >= daily_target_usd):
                    halted_for_day = True
                    send_telegram(
                        f"🎯 تحقق الهدف اليومي (GATE).\nالإجمالي (محقق+غير محقق): +{fmt_money(daily_total)}\n🚫 لن يتم فتح صفقات جديدة اليوم."
                    )

                if halted_for_day:
                    time.sleep(interval_sec)
                    continue

            # ----- auto entries limit -> alert-only (used for alerts too) -----
            in_alert_only = False
            if auto_entries_today >= max_auto_entries_per_day:
                in_alert_only = bool(alert_only_after_limit)

            # ====== دورة مسح (Scan) ======
            any_signal_this_cycle = False

            if scan_log:
                print(f"[SCAN] {utc_now().strftime('%H:%M:%S')} symbols={len(symbols)} auto={auto_trading} entries={auto_entries_today}/{max_auto_entries_per_day}", flush=True)

            # ----- entry logic / signals -----
            for sym in symbols:
                if auto_trading:
                    positions = {p.symbol: p for p in trading.get_all_positions()}
                    open_count = len(positions)
                    if open_count >= max_open_positions:
                        break
                    if sym in positions:
                        continue

                prev_bar, last_bar = get_last_two_closed_1m_bars(data_client, sym)
                if prev_bar is None or last_bar is None:
                    continue

                candle_minute_key = last_bar.timestamp.replace(second=0, microsecond=0).isoformat()
                if last_signal_minute.get(sym) == candle_minute_key:
                    continue

                prev_close = float(prev_bar.close)
                last_close = float(last_bar.close)

                long_signal = last_close > prev_close
                short_signal = last_close < prev_close
                if not (long_signal or short_signal):
                    continue

                direction = "LONG" if long_signal else "SHORT"
                side = OrderSide.BUY if long_signal else OrderSide.SELL

                if enable_confirm:
                    if not candle_passes_filters(last_bar, direction, candle_body_min, candle_close_pos_min, wick_max):
                        continue
                    if not volume_passes_filter(last_bar, prev_bar, min_vol_ratio):
                        continue
                    q0 = get_latest_quote(data_client, sym)
                    ok, why = quote_passes_filters(q0, direction, max_spread_pct, min_imbalance)
                    if not ok:
                        continue

                q = get_latest_quote(data_client, sym)
                bid = float(qget(q, "bid_price", 0.0) or 0.0)
                ask = float(qget(q, "ask_price", 0.0) or 0.0)

                ref_price = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last_close

                if entry_type == "LIMIT":
                    limit_price = round_price(ref_price * (1.0 + offset)) if long_signal else round_price(ref_price * (1.0 - offset))
                else:
                    limit_price = ref_price

                # ====== ALERT ONLY mode (or AUTO_TRADING OFF) ======
                if (not auto_trading) or in_alert_only:
                    now_ts = time.time()
                    last_ts = last_alert_time.get(sym, 0.0)
                    if now_ts - last_ts >= float(alert_cooldown_sec):
                        last_alert_time[sym] = now_ts
                        last_signal_minute[sym] = candle_minute_key
                        any_signal_this_cycle = True
                        last_any_signal_ts = now_ts

                        send_telegram(
                            build_signal_msg_ar(
                                direction=direction,
                                sym=sym,
                                last_close=last_close,
                                prev_close=prev_close,
                                entry_type=entry_type,
                                suggested_price=limit_price,
                                max_spread_pct=max_spread_pct,
                                min_vol_ratio=min_vol_ratio,
                                min_imbalance=min_imbalance,
                                candle_body_min=candle_body_min,
                                candle_close_pos_min=candle_close_pos_min,
                                auto_trading=auto_trading,
                                in_alert_only=in_alert_only,
                            )
                        )
                    continue

                # ====== AUTO TRADING ON ======
                if auto_entries_today >= max_auto_entries_per_day and not alert_only_after_limit:
                    continue
                if auto_entries_today >= max_auto_entries_per_day:
                    continue

                try:
                    oid = place_order(
                        trading=trading,
                        sym=sym,
                        side=side,
                        notional_usd=notional_usd,
                        entry_type=entry_type,
                        limit_price=limit_price,
                    )
                    auto_entries_today += 1
                    last_signal_minute[sym] = candle_minute_key
                    any_signal_this_cycle = True
                    last_any_signal_ts = time.time()

                    send_telegram(
                        build_entry_msg_ar(
                            direction=direction,
                            sym=sym,
                            entry_type=entry_type,
                            last_close=last_close,
                            prev_close=prev_close,
                            stop_loss_usd=stop_loss_usd,
                            auto_entries_today=auto_entries_today,
                            max_auto_entries_per_day=max_auto_entries_per_day,
                            oid=oid,
                        )
                    )

                    if auto_entries_today >= max_auto_entries_per_day and alert_only_after_limit:
                        send_telegram(
                            f"🟡 تم الوصول للحد اليومي ({auto_entries_today}/{max_auto_entries_per_day}).\nمن الآن: تنبيهات فقط ✅",
                            throttle_key="auto_limit_notice",
                            cooldown_sec=300
                        )

                except Exception as e:
                    msg = str(e)
                    print("[ENTRY_FAIL]", sym, direction, repr(e), flush=True)

                    if "429" in msg or "too many" in msg.lower():
                        send_telegram("⚠️ تم تقييد الطلبات مؤقتًا من Alpaca (Rate limit).\n"
                                      f"{msg}",
                                      throttle_key="rate_limit", cooldown_sec=120)
                        time.sleep(30)
                    else:
                        send_telegram("⚠️ فشل تنفيذ الدخول\n"
                                      f"السهم: {sym}\n"
                                      f"الاتجاه: {fmt_dir_ar(direction)}\n"
                                      f"السبب: {msg}",
                                      throttle_key=f"entry_fail_{sym}", cooldown_sec=90)

            # ====== HEARTBEAT: إذا ما فيه فرص ======
            if heartbeat_enabled and ready_to_trade:
                now_ts = time.time()
                if not any_signal_this_cycle:
                    if now_ts - last_heartbeat_ts >= float(heartbeat_sec):
                        last_heartbeat_ts = now_ts
                        msg = (
                            "🤖 أنا شغّال الآن ✅\n"
                            "🔍 أبحث عن فرص… ما لقيت شروط دخول مناسبة حاليًا.\n"
                            f"📊 اليوم: دخول تلقائي {auto_entries_today}/{max_auto_entries_per_day}\n"
                            f"🧷 وضع: {'تنبيهات فقط' if (not auto_trading) else ('تنبيهات فقط' if in_alert_only else 'تداول تلقائي')}"
                        )
                        send_telegram(msg, throttle_key="heartbeat", cooldown_sec=int(max(60, heartbeat_sec - 1)))
                        if heartbeat_log:
                            print(f"[HEARTBEAT] sent at {utc_now().strftime('%H:%M:%S')}", flush=True)

            time.sleep(interval_sec)

        except Exception as e:
            msg = str(e)
            print("[FATAL_LOOP_ERROR]", repr(e), flush=True)

            send_telegram(f"⚠️ خطأ داخل حلقة البوت:\n{msg}", throttle_key="loop_error", cooldown_sec=120)

            if "429" in msg or "too many" in msg.lower():
                time.sleep(30)
            else:
                time.sleep(10)

if __name__ == "__main__":
    main()
