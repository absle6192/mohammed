# bot.py — Alpaca Paper bot: live SIP, daily 500 SAR target with full liquidation, auto buy/sell, $27 stop-loss
import os
import time
import logging
from typing import Optional, List
from datetime import date

from alpaca_trade_api.rest import REST, TimeFrame, APIError

# ==============================
# Logging
# ==============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ==============================
# ENV (Render -> Environment Variables)
# ==============================
API_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip()
DATA_FEED  = os.getenv("APCA_API_DATA_FEED", "sip").strip()  # make sure you set 'sip' in Render

if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY.")

api = REST(API_KEY, API_SECRET, base_url=BASE_URL, api_version="v2")

# ==============================
# Symbols universe
# ==============================
SYMBOLS: List[str] = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN",
    "NFLX", "BA", "MU", "JPM", "PEP",
]

# ==============================
# Trading config
# ==============================
# Sizing via fractional notional:
RISK_PCT_OF_BP   = 0.03   # buy up to 3% of current buying power per entry
MIN_NOTIONAL_USD = 10.0   # minimum notional to avoid tiny orders

# Entry/Exit:
# - Buy if last >= VWAP
# - Exit if last < VWAP
CHECK_INTERVAL_SEC = 30

# Daily profit target (≈ 500 SAR)
SAR_PER_USD           = 3.75
DAILY_TARGET_SAR      = 500.0
DAILY_TARGET_USD      = DAILY_TARGET_SAR / SAR_PER_USD
LIQUIDATE_ON_TARGET   = True   # <— as you requested (liquidate all positions on target)

# Fixed per-position stop-loss (unrealized P&L <= -$27 closes the position)
STOP_LOSS_USD = 27.0

# ==============================
# Session state
# ==============================
_session_day: Optional[date] = None
_session_open_equity: Optional[float] = None
_halt_buys_today: bool = False

# ==============================
# Helpers
# ==============================
def reset_daily_session():
    """Call at the first run of the calendar day."""
    global _session_day, _session_open_equity, _halt_buys_today
    _session_day = date.today()
    acct = api.get_account()
    _session_open_equity = float(acct.equity)
    _halt_buys_today = False
    logging.info(
        "New day: %s | open_equity=%.2f | data_feed=%s",
        _session_day.isoformat(), _session_open_equity, DATA_FEED
    )

def ensure_daily_session():
    """Resets daily state if calendar day changed."""
    global _session_day
    today = date.today()
    if _session_day != today:
        reset_daily_session()

def pnl_today_usd() -> float:
    """Account-level realized+unrealized PnL vs session open equity."""
    acct = api.get_account()
    return float(acct.equity) - (_session_open_equity or float(acct.equity))

def enforce_daily_target():
    """If daily PnL >= target: halt new buys and (optionally) liquidate all."""
    global _halt_buys_today
    pnl = pnl_today_usd()
    if pnl >= DAILY_TARGET_USD and not _halt_buys_today:
        _halt_buys_today = True
        logging.info("[TARGET HIT] pnl=%.2f >= %.2f (~500 SAR). Halting buys.", pnl, DAILY_TARGET_USD)
        if LIQUIDATE_ON_TARGET:
            try:
                api.close_all_positions(cancel_orders=True)
                logging.info("All positions liquidated due to target.")
            except Exception as e:
                logging.warning("Liquidation on target failed: %s", e)

def get_daily_vwap(symbol: str) -> Optional[float]:
    """Get today's daily VWAP from bars; fallback to typical price if missing."""
    try:
        bars = api.get_bars(symbol, TimeFrame.Day, limit=1)
        df = bars.df
        if df is None or df.empty:
            return None
        if "vwap" in df.columns:
            return float(df["vwap"].iloc[-1])
        # Fallback: typical price
        h = float(df["high"].iloc[-1]); l = float(df["low"].iloc[-1]); c = float(df["close"].iloc[-1])
        return (h + l + c) / 3.0
    except Exception as e:
        logging.warning("VWAP fetch error %s: %s", symbol, e)
        return None

def get_last_price(symbol: str) -> Optional[float]:
    """Latest trade price from snapshot; fallback to minute bar close."""
    try:
        snap = api.get_snapshot(symbol)
        last = None
        if getattr(snap, "latest_trade", None) is not None:
            last = getattr(snap.latest_trade, "p", None)
        if last is None and getattr(snap, "minute_bar", None) is not None:
            last = getattr(snap.minute_bar, "c", None)
        return float(last) if last is not None else None
    except Exception as e:
        logging.warning("Last price fetch error %s: %s", symbol, e)
        return None

def position_qty(symbol: str) -> float:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty)
    except Exception:
        return 0.0

def position_unrealized_pl(symbol: str) -> Optional[float]:
    try:
        pos = api.get_position(symbol)
        # unrealized_pl can be None if no position
        upl = getattr(pos, "unrealized_pl", None)
        return float(upl) if upl is not None else None
    except Exception:
        return None

def buy_fractional(symbol: str, bp: float) -> bool:
    """Buy using notional (fractional)."""
    notional = round(max(bp * RISK_PCT_OF_BP, MIN_NOTIONAL_USD), 2)
    try:
        o = api.submit_order(
            symbol=symbol, side="buy", type="market", time_in_force="day",
            notional=notional
        )
        logging.info("BUY %s notional=$%.2f id=%s", symbol, notional, o.id)
        return True
    except APIError as e:
        logging.warning("BUY error %s: %s", symbol, e)
        # try half notional once
        try:
            fallback = round(max(MIN_NOTIONAL_USD, notional * 0.5), 2)
            o2 = api.submit_order(
                symbol=symbol, side="buy", type="market", time_in_force="day",
                notional=fallback
            )
            logging.info("BUY-RETRY %s notional=$%.2f id=%s", symbol, fallback, o2.id)
            return True
        except Exception as e2:
            logging.error("BUY-RETRY failed %s: %s", symbol, e2)
            return False
    except Exception as e:
        logging.error("BUY unexpected %s: %s", symbol, e)
        return False

def sell_all(symbol: str) -> bool:
    """Close full position for a symbol at market."""
    qty = position_qty(symbol)
    if qty == 0:
        return True
    try:
        side = "sell" if qty > 0 else "buy"
        api.submit_order(symbol=symbol, side=side, type="market", time_in_force="day", qty=abs(int(qty)))
        logging.info("SELL %s qty=%s", symbol, int(abs(qty)))
        return True
    except Exception as e:
        logging.error("SELL error %s: %s", symbol, e)
        return False

# ==============================
# Strategy: buy if last >= vwap; exit if last < vwap; per-position $27 stop-loss
# ==============================
def trade_cycle():
    ensure_daily_session()
    enforce_daily_target()

    # If buys halted and (optionally) already liquidated, we still enforce stop-losses below.
    acct = api.get_account()
    bp = float(acct.buying_power)

    for sym in SYMBOLS:
        try:
            vwap = get_daily_vwap(sym)
            last = get_last_price(sym)
            if last is None:
                logging.warning("Skip %s: missing last price", sym)
                continue

            # Per-position $27 stop-loss (based on unrealized PnL)
            upl = position_unrealized_pl(sym)
            if upl is not None and upl <= -abs(STOP_LOSS_USD):
                logging.info("STOP-LOSS %s triggered (UPL=%.2f <= -%.2f).", sym, upl, STOP_LOSS_USD)
                sell_all(sym)
                continue  # proceed to next symbol

            # VWAP-based exit
            if vwap is not None and last < vwap and position_qty(sym) > 0:
                sell_all(sym)
                continue

            # Entry (only if target not hit)
            if not _halt_buys_today and vwap is not None and last >= vwap and position_qty(sym) == 0:
                buy_fractional(sym, bp)

        except Exception as e:
            logging.error("Symbol %s error: %s", sym, e)

# ==============================
# Main loop
# ==============================
def main():
    logging.info(
        "Starting bot | base_url=%s | feed=%s | daily_target≈$%.2f (≈%.0f SAR) | stop_loss=$%.2f",
        BASE_URL, DATA_FEED, DAILY_TARGET_USD, DAILY_TARGET_SAR, STOP_LOSS_USD
    )
    reset_daily_session()

    # Connectivity / account print
    try:
        acct = api.get_account()
        logging.info(
            "Account: equity=%.2f cash=%.2f buying_power=%.2f status=%s",
            float(acct.equity), float(acct.cash), float(acct.buying_power), acct.status
        )
    except Exception as e:
        logging.error("Account check failed: %s", e)

    while True:
        try:
            ensure_daily_session()
            trade_cycle()
        except Exception as e:
            logging.error("Loop error: %s", e)
        time.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    main()
