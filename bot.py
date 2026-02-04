# ========= IMPORTS =========
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


# ========= HELPERS =========
def env(name, default=None):
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_float(name, default):
    return float(env(name, default))


def env_int(name, default):
    return int(env(name, default))


def send_telegram(text):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)


def is_paper(base_url: str) -> bool:
    return "paper" in base_url.lower()


# ========= SETTINGS =========
POSITION_USD = env_float("POSITION_USD", "25000")
TP_USD = env_float("TP_USD", "125")
SL_USD = env_float("SL_USD", "75")
DAILY_MAX_LOSS = env_float("DAILY_MAX_LOSS", "225")
MAX_TRADES_PER_DAY = env_int("MAX_TRADES_PER_DAY", "6")

INTERVAL_SEC = env_int("INTERVAL_SEC", "15")
LOOKBACK_MIN = env_int("LOOKBACK_MIN", "3")
THRESH_PCT = env_float("THRESH_PCT", "0.0015")

VOLUME_MULT = env_float("VOLUME_MULT", "1.8")
VOLUME_BASE_MIN = env_int("VOLUME_BASE_MIN", "20")

AUTO_TRADE = env("AUTO_TRADE", "OFF").upper() == "ON"


# ========= MAIN =========
def main():
    base_url = env("APCA_API_BASE_URL")
    key = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")

    tickers = [t.strip().upper() for t in env("TICKERS").split(",")]

    data = StockHistoricalDataClient(key, secret)
    trading = TradingClient(key, secret, paper=is_paper(base_url))

    start_day = datetime.now(timezone.utc).date()
    start_equity = float(trading.get_account().equity)
    trades_today = 0
    open_trades = {}

    send_telegram(
        "🤖 BOT STARTED (CONFIRMED ONLY)\n"
        f"Tickers: {', '.join(tickers)}\n"
        f"Position: {POSITION_USD}$\n"
        f"TP: +{TP_USD}$ | SL: -{SL_USD}$\n"
        f"Daily Max Loss: -{DAILY_MAX_LOSS}$\n"
        f"Max Trades/Day: {MAX_TRADES_PER_DAY}\n"
        f"AUTO_TRADE: {'ON' if AUTO_TRADE else 'OFF'}"
    )

    def daily_pnl():
        return float(trading.get_account().equity) - start_equity

    def close_position(symbol, reason):
        pos = open_trades[symbol]
        side = OrderSide.SELL if pos["side"] == "LONG" else OrderSide.BUY

        trading.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=pos["qty"],
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        )

        send_telegram(
            f"🏁 CLOSE ({reason})\n"
            f"{symbol} | {pos['side']}\n"
            f"PnL Trade: {pos['pnl']:.2f}$\n"
            f"PnL Today: {daily_pnl():.2f}$"
        )

        del open_trades[symbol]

    while True:
        try:
            now = datetime.now(timezone.utc)

            # ===== New Day Reset =====
            if now.date() != start_day:
                start_day = now.date()
                trades_today = 0
                start_equity = float(trading.get_account().equity)
                open_trades.clear()
                send_telegram("🆕 New Trading Day")

            # ===== Fetch Data =====
            start = now - timedelta(minutes=VOLUME_BASE_MIN + LOOKBACK_MIN + 5)
            bars = data.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=tickers,
                    timeframe=TimeFrame.Minute,
                    start=start,
                    end=now,
                    feed="iex",
                )
            ).df

            if bars is None or len(bars) == 0:
                time.sleep(INTERVAL_SEC)
                continue

            for sym in tickers:
                try:
                    df = bars.xs(sym, level=0).sort_index()
                except Exception:
                    continue

                if len(df) < LOOKBACK_MIN + 3:
                    continue

                price = float(df["close"].iloc[-2])

                # ===== Manage Open Trade =====
                if sym in open_trades:
                    tr = open_trades[sym]
                    if tr["side"] == "LONG":
                        tr["pnl"] = (price - tr["entry"]) * tr["qty"]
                    else:
                        tr["pnl"] = (tr["entry"] - price) * tr["qty"]

                    if tr["pnl"] >= TP_USD:
                        close_position(sym, "TAKE PROFIT")
                    elif tr["pnl"] <= -SL_USD:
                        close_position(sym, "STOP LOSS")
                    continue

                # ===== Daily Guards =====
                if not AUTO_TRADE:
                    continue
                if trades_today >= MAX_TRADES_PER_DAY:
                    continue
                if daily_pnl() <= -DAILY_MAX_LOSS:
                    send_telegram("🛑 DAILY MAX LOSS HIT — BOT STOPPED")
                    return

                # ===== Indicators =====
                ma = df["close"].iloc[-(LOOKBACK_MIN+1):-1].mean()
                diff = (price - ma) / ma

                vol_last = df["volume"].iloc[-2]
                vol_base = df["volume"].iloc[-(VOLUME_BASE_MIN+2):-2].mean()

                if vol_base <= 0:
                    continue
                if vol_last < vol_base * VOLUME_MULT:
                    continue

                # ===== CONFIRMED SIGNAL ONLY =====
                side = None
                if diff >= THRESH_PCT:
                    side = "LONG"
                elif diff <= -THRESH_PCT:
                    side = "SHORT"

                if side is None:
                    continue

                # ===== ENTER =====
                qty = int(POSITION_USD // price)
                if qty <= 0:
                    continue

                order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
                trading.submit_order(
                    MarketOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=order_side,
                        time_in_force=TimeInForce.DAY,
                    )
                )

                open_trades[sym] = {
                    "side": side,
                    "qty": qty,
                    "entry": price,
                    "pnl": 0.0,
                }

                trades_today += 1

                send_telegram(
                    f"🚀 ENTRY CONFIRMED\n"
                    f"{sym} | {side}\n"
                    f"Qty: {qty}\n"
                    f"Entry: {price:.2f}\n"
                    f"Trades Today: {trades_today}/{MAX_TRADES_PER_DAY}"
                )

        except Exception as e:
            send_telegram(f"⚠️ ERROR: {type(e).__name__}: {e}")

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
