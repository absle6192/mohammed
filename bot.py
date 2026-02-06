import os
import time
import math
import requests
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest


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
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "disable_web_page_preview": True,
    }
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
    req = StockBarsRequest(
        symbol_or_symbols=sym,
        timeframe=TimeFrame.Minute,
        limit=3,
    )
    bars = data_client.get_stock_bars(req).data.get(sym, [])
    if len(bars) < 3:
        return None, None
    return bars[-3], bars[-2]


# ===================== trading helpers =====================
def round_price(p: float) -> float:
    return round(p, 2)


def place_limit_notional(
    trading: TradingClient,
    sym: str,
    side: OrderSide,
    notional_usd: float,
    limit_price: float,
) -> str:
    qty = math.floor(notional_usd / max(limit_price, 0.01))
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
    """
    Cancel all open orders. Returns count of canceled orders (best-effort).
    """
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


# ===================== main =====================
def main():
    # ---- config ----
    symbols = [s.strip().upper() for s in env("SYMBOLS").split(",") if s.strip()]
    interval_sec = env_int("INTERVAL_SEC", "15")

    paper = env_bool("ALPACA_PAPER", "true")
    notional_usd = env_float("NOTIONAL_USD", "25000")
    stop_loss_usd = env_float("STOP_LOSS_USD", "150")
    daily_target_usd = env_float("DAILY_TARGET_USD", "300")

    limit_offset_bps = env_float("LIMIT_OFFSET_BPS", "1")  # 1 bps = 0.01%
    offset = limit_offset_bps / 10000.0

    # ✅ خيار 2: بعد الافتتاح بدقيقتين
    start_delay_sec = env_int("START_DELAY_SEC", "120")

    # ---- clients ----
    data_client = StockHistoricalDataClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
    )
    trading = TradingClient(
        api_key=env("APCA_API_KEY_ID"),
        secret_key=env("APCA_API_SECRET_KEY"),
        paper=paper,
    )

    # ---- state ----
    trading_day = utc_now().date()
    daily_realized = 0.0  # approximate
    last_seen_position_qty: dict[str, float] = {}
    last_signal_minute: dict[str, str] = {}

    halted_for_day = False

    # market-open gating
    was_open = None
    open_seen_at: datetime | None = None
    ready_to_trade = False

    # Startup message
    send_telegram(
        "🚀 BOT STARTED (PAPER TRADING)\n"
        f"📊 Symbols: {', '.join(symbols)}\n"
        f"⏱ Interval: {interval_sec}s\n"
        f"💰 Notional/Trade: ${notional_usd:,.0f}\n"
        f"🛑 Stop/Trade: -${stop_loss_usd:,.0f}\n"
        f"🎯 Daily Target: +${daily_target_usd:,.0f}\n"
        f"🧾 Entry: LIMIT (offset {limit_offset_bps} bps)\n"
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
                send_telegram(f"🗓 New UTC day: {trading_day} — counters reset. ✅")
                print("[DAY_RESET]", trading_day, flush=True)

            # ----- market clock -----
            clock = trading.get_clock()
            is_open = bool(clock.is_open)

            # On close: cancel any open orders and block trading
            if was_open is None:
                was_open = is_open

            if not is_open:
                if was_open:  # just transitioned open -> closed
                    send_telegram("🛑 Market is CLOSED now. Canceling open orders.")
                canceled = cancel_all_open_orders(trading)
                if canceled > 0:
                    print(f"[CANCEL] canceled={canceled} (market closed)", flush=True)
                open_seen_at = None
                ready_to_trade = False
                was_open = is_open
                time.sleep(interval_sec)
                continue

            # is_open == True here
            if (was_open is False) and is_open:
                # just opened
                open_seen_at = now
                ready_to_trade = False
                send_telegram(
                    "🔔 Market OPEN detected.\n"
                    f"⏳ Waiting {start_delay_sec}s before placing ANY orders."
                )

            was_open = is_open

            # If bot started during open and open_seen_at is None, be safe: wait delay from now once
            if open_seen_at is None:
                open_seen_at = now
                ready_to_trade = False
                send_telegram(
                    "🔔 Market is already OPEN.\n"
                    f"⏳ Safety wait {start_delay_sec}s before trading."
                )

            # Wait 2 minutes after open
            elapsed = (now - open_seen_at).total_seconds()
            if elapsed < start_delay_sec:
                # no orders before delay
                remaining = int(start_delay_sec - elapsed)
                print(f"[WAIT_AFTER_OPEN] remaining={remaining}s", flush=True)
                time.sleep(min(interval_sec, 10))
                continue
            else:
                if not ready_to_trade:
                    ready_to_trade = True
                    send_telegram("✅ Trading enabled now (post-open delay passed).")

            # ----- positions -----
            positions = {p.symbol: p for p in trading.get_all_positions()}
            unreal_total = 0.0
            for sym, p in positions.items():
                try:
                    unreal_total += float(p.unrealized_pl)
                except Exception:
                    pass

            daily_total = daily_realized + unreal_total

            # daily target reached -> close everything & halt
            if (not halted_for_day) and (daily_total >= daily_target_usd):
                send_telegram(
                    f"🎯 Daily target reached!\n"
                    f"Total (real+unreal): +${daily_total:,.2f}\n"
                    f"🛑 Closing all positions and halting for today."
                )
                for sym in list(positions.keys()):
                    try:
                        close_position_market(trading, sym)
                        send_telegram(f"✅ Closed {sym} (daily target).")
                    except Exception as e:
                        print("[CLOSE_FAIL]", sym, repr(e), flush=True)
                halted_for_day = True
                # also cancel any remaining open orders
                cancel_all_open_orders(trading)

            # manage stop loss per position
            for sym, p in positions.items():
                try:
                    upl = float(p.unrealized_pl)
                except Exception:
                    continue
                if upl <= -abs(stop_loss_usd):
                    send_telegram(
                        f"🛑 STOP HIT {sym}\n"
                        f"Unrealized: ${upl:,.2f}\n"
                        f"Closing position now."
                    )
                    try:
                        close_position_market(trading, sym)
                    except Exception as e:
                        print("[STOP_CLOSE_FAIL]", sym, repr(e), flush=True)

            # basic closure notices (best-effort)
            for sym in list(last_seen_position_qty.keys()):
                prev_qty = last_seen_position_qty.get(sym, 0.0)
                if sym not in positions and prev_qty != 0.0:
                    send_telegram(f"✅ Position closed: {sym}")
                    last_seen_position_qty[sym] = 0.0

            for sym, p in positions.items():
                try:
                    last_seen_position_qty[sym] = float(p.qty)
                except Exception:
                    last_seen_position_qty[sym] = 0.0

            # If halted -> do not open new trades
            if halted_for_day:
                time.sleep(interval_sec)
                continue

            # ----- entry logic -----
            for sym in symbols:
                if sym in positions:
                    continue  # already in trade

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

                if long_signal:
                    side = OrderSide.BUY
                    limit_price = round_price(last_close * (1.0 + offset))
                    direction = "LONG"
                else:
                    side = OrderSide.SELL
                    limit_price = round_price(last_close * (1.0 - offset))
                    direction = "SHORT"

                try:
                    oid = place_limit_notional(
                        trading=trading,
                        sym=sym,
                        side=side,
                        notional_usd=notional_usd,
                        limit_price=limit_price,
                    )
                    last_signal_minute[sym] = candle_minute_key

                    send_telegram(
                        f"📣 ENTRY {direction} | {sym}\n"
                        f"Limit: {limit_price}\n"
                        f"Candle close: {last_close} vs prev {prev_close}\n"
                        f"Stop: -${stop_loss_usd:,.0f}\n"
                        f"Daily target: +${daily_target_usd:,.0f}\n"
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
