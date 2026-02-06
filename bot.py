import os
import time
import math
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


# ===================== telegram =====================
def send_telegram(msg: str) -> None:
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


# ===================== market data =====================
def get_last_two_closed_1m_bars(data_client: StockHistoricalDataClient, sym: str):
    """
    Return (prev_closed, last_closed) using 1m bars.
    We take [-3] and [-2] to avoid the currently-forming bar.
    """
    req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, limit=3)
    bars = data_client.get_stock_bars(req).data.get(sym, [])
    if len(bars) < 3:
        return None, None
    return bars[-3], bars[-2]

def get_latest_quote(data_client: StockHistoricalDataClient, sym: str):
    req = StockLatestQuoteRequest(symbol_or_symbols=sym)
    q = data_client.get_stock_latest_quote(req).data.get(sym)
    return q


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
        # alpaca-py MarketOrderRequest supports qty or notional depending on account/settings
        # safest: send qty computed from latest reference price if notional fails
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

    # LIMIT
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

    # close position within range
    close_pos = (c - l) / rng            # 0..1 (near high = 1)
    close_pos_short = (h - c) / rng      # 0..1 (near low = 1)

    direction = direction.upper()
    if direction == "LONG":
        # نبي شمعة قوية: جسم واضح + إغلاق قريب من الأعلى + ظل علوي صغير
        return (body_pct >= body_min) and (close_pos >= close_pos_min) and (upper_wick_pct <= wick_max)
    else:
        # SHORT: جسم واضح + إغلاق قريب من الأسفل + ظل سفلي صغير
        return (body_pct >= body_min) and (close_pos_short >= close_pos_min) and (lower_wick_pct <= wick_max)

def volume_passes_filter(last_bar, prev_bar, min_vol_ratio: float) -> bool:
    # مقارنة حجم آخر شمعة مع متوسط بسيط (شمعتين) — تقدر توسعها لاحقًا
    v_last = float(last_bar.volume)
    v_prev = float(prev_bar.volume)
    avg = max((v_last + v_prev) / 2.0, 1.0)
    return (v_last / avg) >= min_vol_ratio

def quote_passes_filters(q, direction: str, max_spread_pct: float, min_imbalance: float) -> tuple[bool, str]:
    """
    Uses bid/ask + sizes.
    LONG wants bid_size > ask_size (imbalance > min_imbalance) and spread small.
    SHORT wants ask_size > bid_size (inverse imbalance).
    """
    if q is None:
        return False, "no_quote"

    bid = float(getattr(q, "bid_price", 0.0) or 0.0)
    ask = float(getattr(q, "ask_price", 0.0) or 0.0)
    bid_sz = float(getattr(q, "bid_size", 0.0) or 0.0)
    ask_sz = float(getattr(q, "ask_size", 0.0) or 0.0)

    if bid <= 0 or ask <= 0 or ask <= bid:
        return False, f"bad_bidask bid={bid} ask={ask}"

    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid

    if spread_pct > max_spread_pct:
        return False, f"spread_too_wide {spread_pct:.4f}"

    # imbalance
    imb = (bid_sz + 1.0) / (ask_sz + 1.0)

    direction = direction.upper()
    if direction == "LONG":
        if imb < min_imbalance:
            return False, f"imb_low {imb:.2f}"
    else:
        if (1.0 / imb) < min_imbalance:
            return False, f"imb_not_short {imb:.2f}"

    return True, f"ok spread={spread_pct:.4f} imb={imb:.2f}"


# ===================== main =====================
def main():
    symbols = [s.strip().upper() for s in env("SYMBOLS").split(",") if s.strip()]
    interval_sec = env_int("INTERVAL_SEC", "15")

    paper = env_bool("ALPACA_PAPER", "true")
    notional_usd = env_float("NOTIONAL_USD", "25000")
    stop_loss_usd = env_float("STOP_LOSS_USD", "150")
    daily_target_usd = env_float("DAILY_TARGET_USD", "300")

    daily_mode = env("DAILY_TARGET_MODE", "GATE").upper().strip()  # GATE or HALT
    entry_type = env("ENTRY_TYPE", "MARKET").upper().strip()       # MARKET or LIMIT

    limit_offset_bps = env_float("LIMIT_OFFSET_BPS", "1")
    offset = limit_offset_bps / 10000.0

    start_delay_sec = env_int("START_DELAY_SEC", "120")

    # --- filters ---
    enable_confirm = env_bool("ENABLE_CONFIRM", "true")
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.2")
    max_spread_pct = env_float("MAX_SPREAD_PCT", "0.002")          # 0.2%
    min_imbalance = env_float("MIN_IMBALANCE", "1.2")              # bid/ask size ratio
    candle_body_min = env_float("CANDLE_BODY_MIN", "0.5")          # 0..1
    candle_close_pos_min = env_float("CANDLE_CLOSE_POS_MIN", "0.7")# 0..1
    wick_max = env_float("WICK_MAX", "0.35")                       # 0..1

    # risk control
    max_open_positions = env_int("MAX_OPEN_POSITIONS", "2")
    cooldown_after_close_sec = env_int("COOLDOWN_AFTER_CLOSE_SEC", "90")

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
    daily_realized = 0.0  # approximate
    last_seen_position_qty: dict[str, float] = {}
    last_signal_minute: dict[str, str] = {}

    halted_for_day = False

    was_open = None
    open_seen_at: datetime | None = None
    ready_to_trade = False

    last_any_close_time: datetime | None = None

    send_telegram(
        "🚀 BOT STARTED (PAPER TRADING)\n"
        f"📊 Symbols: {', '.join(symbols)}\n"
        f"⏱ Interval: {interval_sec}s\n"
        f"💰 Notional/Trade: ${notional_usd:,.0f}\n"
        f"🛑 Stop/Trade: -${stop_loss_usd:,.0f}\n"
        f"🎯 Daily Target: +${daily_target_usd:,.0f} ({daily_mode})\n"
        f"🧾 Entry: {entry_type}{'' if entry_type=='MARKET' else f' (offset {limit_offset_bps} bps)'}\n"
        f"🧠 Confirm: {'ON' if enable_confirm else 'OFF'} "
        f"(VOLx≥{min_vol_ratio}, spread≤{max_spread_pct:.3f}, imb≥{min_imbalance}, body≥{candle_body_min}, closepos≥{candle_close_pos_min})\n"
        f"🔒 Max open positions: {max_open_positions}\n"
        f"🧊 Cooldown after close: {cooldown_after_close_sec}s\n"
        f"⏳ Start after open: {start_delay_sec}s\n"
        f"🕒 UTC: {utc_now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print("[BOOT] started", flush=True)

    while True:
        try:
            now = utc_now()

            # reset day at UTC midnight
            if now.date() != trading_day:
                trading_day = now.date()
                daily_realized = 0.0
                halted_for_day = False
                last_signal_minute.clear()
                open_seen_at = None
                ready_to_trade = False
                last_any_close_time = None
                send_telegram(f"🗓 New UTC day: {trading_day} — counters reset. ✅")

            clock = trading.get_clock()
            is_open = bool(clock.is_open)

            if was_open is None:
                was_open = is_open

            if not is_open:
                if was_open:
                    send_telegram("🛑 Market is CLOSED now. Canceling open orders.")
                cancel_all_open_orders(trading)
                open_seen_at = None
                ready_to_trade = False
                was_open = is_open
                time.sleep(interval_sec)
                continue

            if (was_open is False) and is_open:
                open_seen_at = now
                ready_to_trade = False
                send_telegram(f"🔔 Market OPEN detected.\n⏳ Waiting {start_delay_sec}s before placing ANY orders.")

            was_open = is_open

            if open_seen_at is None:
                open_seen_at = now
                ready_to_trade = False
                send_telegram(f"🔔 Market is already OPEN.\n⏳ Safety wait {start_delay_sec}s before trading.")

            elapsed = (now - open_seen_at).total_seconds()
            if elapsed < start_delay_sec:
                time.sleep(min(interval_sec, 10))
                continue
            else:
                if not ready_to_trade:
                    ready_to_trade = True
                    send_telegram("✅ Trading enabled now (post-open delay passed).")

            # cooldown after any close
            if last_any_close_time is not None:
                if (now - last_any_close_time).total_seconds() < cooldown_after_close_sec:
                    time.sleep(interval_sec)
                    continue

            # ----- positions -----
            positions = {p.symbol: p for p in trading.get_all_positions()}
            open_count = len(positions)

            unreal_total = 0.0
            for sym, p in positions.items():
                try:
                    unreal_total += float(p.unrealized_pl)
                except Exception:
                    pass

            daily_total = daily_realized + unreal_total

            # daily target gate
            if (not halted_for_day) and (daily_total >= daily_target_usd):
                if daily_mode == "HALT":
                    send_telegram(
                        f"🎯 Daily target reached!\nTotal (real+unreal): +${daily_total:,.2f}\n🛑 Closing all positions and halting for today."
                    )
                    for sym in list(positions.keys()):
                        try:
                            close_position_market(trading, sym)
                            send_telegram(f"✅ Closed {sym} (daily target).")
                        except Exception as e:
                            print("[CLOSE_FAIL]", sym, repr(e), flush=True)
                    halted_for_day = True
                    cancel_all_open_orders(trading)
                    last_any_close_time = utc_now()
                else:
                    # GATE: يمنع فتح صفقات جديدة فقط
                    halted_for_day = True
                    send_telegram(
                        f"🎯 Daily target reached (GATE).\nTotal (real+unreal): +${daily_total:,.2f}\n🚫 No NEW entries today (existing positions still managed)."
                    )

            # stop loss per position
            for sym, p in positions.items():
                try:
                    upl = float(p.unrealized_pl)
                except Exception:
                    continue
                if upl <= -abs(stop_loss_usd):
                    send_telegram(f"🛑 STOP HIT {sym}\nUnrealized: ${upl:,.2f}\nClosing position now.")
                    try:
                        close_position_market(trading, sym)
                        last_any_close_time = utc_now()
                    except Exception as e:
                        print("[STOP_CLOSE_FAIL]", sym, repr(e), flush=True)

            # closure notices
            for sym in list(last_seen_position_qty.keys()):
                prev_qty = last_seen_position_qty.get(sym, 0.0)
                if sym not in positions and prev_qty != 0.0:
                    send_telegram(f"✅ Position closed: {sym}")
                    last_seen_position_qty[sym] = 0.0
                    last_any_close_time = utc_now()

            for sym, p in positions.items():
                try:
                    last_seen_position_qty[sym] = float(p.qty)
                except Exception:
                    last_seen_position_qty[sym] = 0.0

            # If halted (GATE/HALT) -> do not open new trades
            if halted_for_day:
                time.sleep(interval_sec)
                continue

            # cap open positions
            if open_count >= max_open_positions:
                time.sleep(interval_sec)
                continue

            # ----- entry logic -----
            for sym in symbols:
                # refresh open positions count each loop (لأنك ممكن تفتح صفقة داخل اللوب)
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

                # ====== CONFIRM FILTERS (candle + volume + bid/ask) ======
                if enable_confirm:
                    if not candle_passes_filters(last_bar, direction, candle_body_min, candle_close_pos_min, wick_max):
                        continue
                    if not volume_passes_filter(last_bar, prev_bar, min_vol_ratio):
                        continue
                    q = get_latest_quote(data_client, sym)
                    ok, why = quote_passes_filters(q, direction, max_spread_pct, min_imbalance)
                    if not ok:
                        continue

                # entry price reference
                q = get_latest_quote(data_client, sym)
                if q is not None and getattr(q, "bid_price", None) and getattr(q, "ask_price", None):
                    bid = float(q.bid_price)
                    ask = float(q.ask_price)
                    ref_price = (bid + ask) / 2.0
                else:
                    ref_price = last_close

                if entry_type == "LIMIT":
                    if long_signal:
                        limit_price = round_price(ref_price * (1.0 + offset))
                    else:
                        limit_price = round_price(ref_price * (1.0 - offset))
                else:
                    limit_price = ref_price  # فقط مرجع لحساب qty

                try:
                    oid = place_order(
                        trading=trading,
                        sym=sym,
                        side=side,
                        notional_usd=notional_usd,
                        entry_type=entry_type,
                        limit_price=limit_price,
                    )
                    last_signal_minute[sym] = candle_minute_key

                    send_telegram(
                        f"📣 ENTRY {direction} | {sym}\n"
                        f"Type: {entry_type}\n"
                        f"Ref close: {last_close} vs prev {prev_close}\n"
                        f"Stop: -${stop_loss_usd:,.0f}\n"
                        f"Daily target: +${daily_target_usd:,.0f} ({daily_mode})\n"
                        f"Order id: {oid}"
                    )
                except Exception as e:
                    print("[ENTRY_FAIL]", sym, direction, repr(e), flush=True)

            time.sleep(interval_sec)

        except Exception as e:
            print("[FATAL_LOOP_ERROR]", repr(e), flush=True)
            send_telegram(f"⚠️ Bot loop error:\n{e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
