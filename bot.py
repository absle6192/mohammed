import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


# ----------------- helpers -----------------
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_float(name: str, default: str) -> float:
    return float(env(name, default))


def env_int(name: str, default: str) -> int:
    return int(env(name, default))


def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def strength_label(vol_ratio: float) -> str:
    if vol_ratio >= 3.0:
        return "🔥🔥🔥 نار (Very Strong)"
    if vol_ratio >= 2.5:
        return "🔥🔥 قوية جدًا (Strong+)"
    if vol_ratio >= 2.0:
        return "🔥 قوية (Strong)"
    if vol_ratio >= 1.3:
        return "✅ متوسطة (OK)"
    return "⚠️ ضعيفة (Weak)"


def candle_filter_light_completed(df_all: pd.DataFrame, side: str, close_pos_min: float = 0.65) -> bool:
    """
    فلتر شموع خفيف لكن على شموع مكتملة:
    - نستخدم آخر شمعة مكتملة = -2
    - والشمعة اللي قبلها = -3
    """
    if df_all is None or len(df_all) < 4:
        return False

    last = df_all.iloc[-2]   # completed candle
    prev = df_all.iloc[-3]

    o = float(last["open"])
    h = float(last["high"])
    l = float(last["low"])
    c = float(last["close"])
    prev_c = float(prev["close"])

    rng = h - l
    if rng <= 0:
        return False

    close_pos = (c - l) / rng  # 0 at low, 1 at high

    if side == "LONG":
        return (c >= o) and (c > prev_c) and (close_pos >= close_pos_min)

    return (c <= o) and (c < prev_c) and (close_pos <= (1.0 - close_pos_min))


# ----------------- trading helpers -----------------
def is_paper_from_base_url(base_url: str) -> bool:
    return "paper" in base_url.lower()


def floor_qty(qty: float) -> int:
    return max(0, int(math.floor(qty)))


def compute_qty_by_risk(price: float, risk_usd: float, stop_pct: float, max_notional: float) -> int:
    """
    risk = qty * price * stop_pct  => qty = risk / (price*stop_pct)
    ثم نطبّق سقف notional
    """
    if price <= 0 or stop_pct <= 0:
        return 0
    qty_risk = risk_usd / (price * stop_pct)
    qty_cap = max_notional / price
    return floor_qty(min(qty_risk, qty_cap))


def wait_for_fill(trading: TradingClient, order_id: str, timeout_sec: int = 25) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        o = trading.get_order_by_id(order_id)
        if o.status in ("filled", "canceled", "rejected", "expired"):
            return {
                "status": o.status,
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "filled_qty": float(o.filled_qty) if o.filled_qty else None,
                "symbol": o.symbol,
                "side": o.side,
            }
        time.sleep(1)
    return {"status": "timeout", "filled_avg_price": None, "filled_qty": None}


# ----------------- main logic -----------------
def main():
    base_url = env("APCA_API_BASE_URL")  # e.g. https://paper-api.alpaca.markets
    key_id = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")

    tickers = [t.strip().upper() for t in env("TICKERS").split(",") if t.strip()]

    # ===== إشارات =====
    mode = env("MODE", "EARLY").upper()
    if mode not in ("EARLY", "CONFIRM", "BOTH"):
        mode = "EARLY"

    interval_sec = env_int("INTERVAL_SEC", "15")
    lookback_min = env_int("LOOKBACK_MIN", "3")
    thresh_pct = env_float("THRESH_PCT", "0.0008")

    volume_mult = env_float("VOLUME_MULT", "1.2")
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.1")

    cooldown_min = env_int("COOLDOWN_MIN", "6")

    recent_window_min = env_int("RECENT_WINDOW_MIN", "10")
    max_recent_move_pct = env_float("MAX_RECENT_MOVE_PCT", "0.003")

    candle_filter_mode = env("CANDLE_FILTER", "LIGHT").upper()  # LIGHT/OFF
    candle_close_pos_min = env_float("CANDLE_CLOSE_POS_MIN", "0.65")

    # ✅ baseline حجم أطول
    volume_base_min = env_int("VOLUME_BASE_MIN", "20")

    # ===== AUTO TRADE =====
    auto_trade = env("AUTO_TRADE", "OFF").upper() == "ON"

    daily_profit_target = env_float("DAILY_PROFIT_TARGET", "400")
    daily_max_loss = env_float("DAILY_MAX_LOSS", "300")
    max_trades_per_day = env_int("MAX_TRADES_PER_DAY", "6")

    risk_per_trade_usd = env_float("RISK_PER_TRADE_USD", "60")
    stop_pct = env_float("STOP_PCT", "0.0025")              # 0.25%
    take_profit_pct = env_float("TAKE_PROFIT_PCT", "0.004") # 0.40%

    # ✅ Protect Profit A = 0.20%
    protect_profit_at = env_float("PROTECT_PROFIT_AT", "0.0020")

    max_notional_per_trade = env_float("MAX_NOTIONAL_PER_TRADE", "25000")

    # ✅ NEW: close any trade once profit >= X dollars
    min_profit_usd = env_float("MIN_PROFIT_USD", "0")  # مثال: 20

    # clients
    data_client = StockHistoricalDataClient(key_id, secret)
    trading = TradingClient(key_id, secret, paper=is_paper_from_base_url(base_url))

    # day tracking (UTC day)
    start_day = datetime.now(timezone.utc).date()
    account0 = trading.get_account()
    start_equity = float(account0.equity)
    trades_today = 0

    # cooldown per (sym, mode_tag, side)
    last_signal_time: dict[tuple[str, str, str], datetime] = {}

    open_trades: dict[str, dict] = {}

    send_telegram(
        "✅ Bot Started\n"
        f"Watching: {', '.join(tickers)}\n"
        f"MODE: {mode}\n"
        f"Interval: {interval_sec}s | Lookback: {lookback_min}m\n"
        f"Threshold: {thresh_pct*100:.2f}%\n"
        f"Volume: mult x{volume_mult} | min ratio x{min_vol_ratio}\n"
        f"Volume baseline: {volume_base_min}m\n"
        f"Late-entry: abs(move {recent_window_min}m) <= {max_recent_move_pct*100:.2f}%\n"
        f"Candle: {candle_filter_mode} | ClosePosMin: {candle_close_pos_min}\n"
        f"AUTO_TRADE: {'ON' if auto_trade else 'OFF'}\n"
        f"Daily TP: +{daily_profit_target}$ | Daily SL: -{daily_max_loss}$\n"
        f"Risk/Trade: {risk_per_trade_usd}$ | Stop: {stop_pct*100:.2f}% | TP: {take_profit_pct*100:.2f}%\n"
        f"Protect Profit (A): at +{protect_profit_at*100:.2f}% => SL to Breakeven\n"
        f"Min Profit USD: {min_profit_usd}$ (close when >=)\n"
        f"Max Notional/Trade: {max_notional_per_trade}$ | Max Trades/Day: {max_trades_per_day}\n"
        f"Equity start: {start_equity:.2f}$ (UTC day)\n"
    )

    def daily_pnl_now() -> float:
        acc = trading.get_account()
        eq = float(acc.equity)
        return eq - start_equity

    def daily_guards_ok() -> bool:
        nonlocal trades_today
        pnl = daily_pnl_now()
        if pnl >= daily_profit_target:
            return False
        if pnl <= -daily_max_loss:
            return False
        if trades_today >= max_trades_per_day:
            return False
        return True

    def close_position(symbol: str, reason: str) -> None:
        if symbol not in open_trades:
            return

        tr = open_trades[symbol]
        side = tr["side"]
        qty = tr["qty"]
        entry = tr["entry_price"]

        close_side = OrderSide.SELL if side == "LONG" else OrderSide.BUY

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=close_side,
            time_in_force=TimeInForce.DAY,
        )

        try:
            o = trading.submit_order(req)
            info = wait_for_fill(trading, o.id)
            pnl_day = daily_pnl_now()

            if info["status"] == "filled":
                exit_price = info["filled_avg_price"]
                if exit_price is None:
                    exit_price = entry

                if side == "LONG":
                    pnl_trade = (exit_price - entry) * qty
                else:
                    pnl_trade = (entry - exit_price) * qty

                send_telegram(
                    f"🏁 Close ({reason})\n"
                    f"Symbol: {symbol}\n"
                    f"Side: {side}\n"
                    f"Qty: {qty}\n"
                    f"Entry: {entry:.4f}\n"
                    f"Exit: {exit_price:.4f}\n"
                    f"PnL Trade: {pnl_trade:.2f}$\n"
                    f"PnL Today: {pnl_day:.2f}$"
                )
            else:
                send_telegram(f"⚠️ Close not completed: {symbol}\nStatus: {info['status']}")
        except Exception as e:
            send_telegram(f"❌ Close failed: {symbol}\n{type(e).__name__}: {e}")
        finally:
            open_trades.pop(symbol, None)

    def place_entry(symbol: str, side: str, price_now: float) -> None:
        nonlocal trades_today

        if not auto_trade:
            return

        if not daily_guards_ok():
            pnl = daily_pnl_now()
            send_telegram(f"⛔️ AUTO_TRADE STOP (daily limits)\nPnL Today: {pnl:.2f}$")
            return

        qty = compute_qty_by_risk(price_now, risk_per_trade_usd, stop_pct, max_notional_per_trade)
        if qty <= 0:
            return

        order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )

        try:
            o = trading.submit_order(req)
            info = wait_for_fill(trading, o.id)
            pnl = daily_pnl_now()

            if info["status"] == "filled":
                entry_price = info["filled_avg_price"] or price_now

                if side == "LONG":
                    tp = entry_price * (1 + take_profit_pct)
                    sl = entry_price * (1 - stop_pct)
                else:
                    tp = entry_price * (1 - take_profit_pct)
                    sl = entry_price * (1 + stop_pct)

                trades_today += 1
                open_trades[symbol] = {
                    "side": side,
                    "qty": qty,
                    "entry_price": float(entry_price),
                    "tp": float(tp),
                    "sl": float(sl),
                    "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "protected": False,  # 🛡️ Protect Profit activated?
                }

                send_telegram(
                    f"✅ Entry Filled\n"
                    f"Symbol: {symbol}\n"
                    f"Side: {side}\n"
                    f"Qty: {qty}\n"
                    f"Entry: {entry_price:.4f}\n"
                    f"TP: {tp:.4f} | SL: {sl:.4f}\n"
                    f"Trades Today: {trades_today}/{max_trades_per_day}\n"
                    f"PnL Today: {pnl:.2f}$"
                )
            else:
                send_telegram(f"⚠️ Entry not filled: {symbol} {side}\nStatus: {info['status']}")
        except Exception as e:
            send_telegram(f"❌ Entry failed: {symbol} {side}\n{type(e).__name__}: {e}")

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Reset daily counters on UTC day change
            if now.date() != start_day:
                start_day = now.date()
                trades_today = 0
                account0 = trading.get_account()
                start_equity = float(account0.equity)
                open_trades.clear()
                send_telegram(f"🆕 New UTC Day\nEquity start: {start_equity:.2f}$")

            # ---- fetch bars ----
            need_min = max(lookback_min, recent_window_min, volume_base_min) + 6
            start = now - timedelta(minutes=need_min)

            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame.Minute,
                start=start,
                end=now,
                feed="iex",
            )

            bars = data_client.get_stock_bars(req).df
            if bars is None or len(bars) == 0:
                time.sleep(interval_sec)
                continue

            for sym in tickers:
                try:
                    df_all = bars.xs(sym, level=0).copy()
                except Exception:
                    continue

                df_all = df_all.sort_index()
                if len(df_all) < 6:
                    continue

                # ===== use completed candle as "now" =====
                price_now = float(df_all["close"].iloc[-2])  # completed candle close

                # ===== manage open trade first =====
                if sym in open_trades:
                    tr = open_trades[sym]
                    side = tr["side"]
                    qty = int(tr["qty"])
                    entry = float(tr["entry_price"])

                    # unrealized pct + pnl usd
                    if side == "LONG":
                        unrealized_pct = (price_now - entry) / entry
                        pnl_usd = (price_now - entry) * qty
                    else:
                        unrealized_pct = (entry - price_now) / entry
                        pnl_usd = (entry - price_now) * qty

                    # ✅ NEW RULE: close if profit >= MIN_PROFIT_USD (and positive)
                    if min_profit_usd > 0 and pnl_usd >= min_profit_usd:
                        close_position(sym, f"MIN_PROFIT_USD >= {min_profit_usd}$")
                        continue

                    # ✅ Protect Profit A: at +0.20% => SL to breakeven
                    if (not tr.get("protected", False)) and (unrealized_pct >= protect_profit_at):
                        tr["protected"] = True
                        if side == "LONG":
                            tr["sl"] = max(float(tr["sl"]), entry)
                        else:
                            tr["sl"] = min(float(tr["sl"]), entry)

                        send_telegram(
                            f"🛡️ Protect Profit ON (A)\n"
                            f"Symbol: {sym}\n"
                            f"Side: {side}\n"
                            f"Entry: {entry:.4f}\n"
                            f"Price now: {price_now:.4f}\n"
                            f"Profit now: {unrealized_pct*100:.2f}%\n"
                            f"PnL now: {pnl_usd:.2f}$\n"
                            f"New SL (Breakeven): {float(tr['sl']):.4f}"
                        )

                    # exits TP/SL
                    if side == "LONG":
                        if price_now >= float(tr["tp"]):
                            close_position(sym, "TAKE PROFIT")
                        elif price_now <= float(tr["sl"]):
                            close_position(sym, "STOP / BREAKEVEN")
                    else:  # SHORT
                        if price_now <= float(tr["tp"]):
                            close_position(sym, "TAKE PROFIT")
                        elif price_now >= float(tr["sl"]):
                            close_position(sym, "STOP / BREAKEVEN")

                    continue  # after managing trade, skip signals for this symbol

                # ===== If daily limits hit, don't open new trades =====
                if auto_trade and not daily_guards_ok():
                    continue

                # ===== late-entry filter =====
                df_recent = df_all.tail(recent_window_min + 2)
                if len(df_recent) < 4:
                    continue

                price_then = float(df_recent["close"].iloc[0])
                recent_move = pct(price_now, price_then)
                if abs(recent_move) > max_recent_move_pct:
                    continue

                # ===== MA lookback (completed candles) =====
                df_lb = df_all.tail(lookback_min + 2)
                if len(df_lb) < (lookback_min + 2):
                    continue
                ma = float(df_lb["close"].iloc[-(lookback_min + 1):-1].mean())
                d = pct(price_now, ma)

                # ===== volume baseline (long window) =====
                df_vol = df_all.tail(volume_base_min + 2)
                if len(df_vol) < 5:
                    continue

                vol_last = float(df_vol["volume"].iloc[-2])  # completed candle volume
                vol_base = float(df_vol["volume"].iloc[:-2].mean())
                vol_ratio = (vol_last / vol_base) if vol_base else 0.0

                vol_ok = (vol_base > 0) and (vol_last >= vol_base * volume_mult) and (vol_ratio >= min_vol_ratio)
                if not vol_ok:
                    continue

                # ===== generate signals =====
                signals: list[tuple[str, str]] = []

                if mode in ("EARLY", "BOTH"):
                    if d >= thresh_pct:
                        signals.append(("🟡 EARLY", "LONG"))
                    elif d <= -thresh_pct:
                        signals.append(("🟡 EARLY", "SHORT"))

                if mode in ("CONFIRM", "BOTH"):
                    confirm_thresh = env_float("CONFIRM_THRESH_PCT", str(max(thresh_pct * 1.8, 0.0015)))
                    confirm_vol_mult = env_float("CONFIRM_VOLUME_MULT", str(max(volume_mult * 1.4, 1.8)))
                    confirm_ok = (vol_last >= vol_base * confirm_vol_mult)

                    if confirm_ok:
                        if d >= confirm_thresh:
                            signals.append(("🟢 CONFIRM", "LONG"))
                        elif d <= -confirm_thresh:
                            signals.append(("🟢 CONFIRM", "SHORT"))

                if not signals:
                    continue

                # ===== act on signals =====
                for mode_tag, side in signals:
                    # candle filter on EARLY only
                    candle_ok = True
                    if candle_filter_mode != "OFF" and "EARLY" in mode_tag:
                        candle_ok = candle_filter_light_completed(df_all, side, close_pos_min=candle_close_pos_min)
                        if not candle_ok:
                            continue

                    # cooldown per (sym, mode_tag, side)
                    k = (sym, mode_tag, side)
                    lt = last_signal_time.get(k)
                    if lt and (now - lt) < timedelta(minutes=cooldown_min):
                        continue
                    last_signal_time[k] = now

                    strength = strength_label(vol_ratio)

                    send_telegram(
                        f"{mode_tag} | Signal {side}\n"
                        f"Symbol: {sym}\n"
                        f"Price: {price_now:.2f}\n"
                        f"MA({lookback_min}m): {ma:.2f}\n"
                        f"Diff: {fmt_pct(d)}\n"
                        f"Vol: {vol_last:.0f} vs base {vol_base:.0f} (x{vol_ratio:.2f})\n"
                        f"Recent {recent_window_min}m: {fmt_pct(recent_move)}\n"
                        f"Candle(LIGHT): {'PASS' if candle_ok else 'FAIL'}\n"
                        f"Strength: {strength}\n"
                        f"Time(UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"AUTO_TRADE: {'ON' if auto_trade else 'OFF'}"
                    )

                    if auto_trade:
                        place_entry(sym, side, price_now)

        except Exception as e:
            try:
                send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
