# bot.py
# -----------------------------------------
# Simple Alpaca paper-trading bot (VWAP check + fractional orders)
# Works with: alpaca-trade-api >= 3.x
# -----------------------------------------

import os
import time
import logging
from typing import Optional, List

from alpaca_trade_api.rest import REST, TimeFrame, APIError


# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ========= Env & Client =========
API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL   = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY environment variables.")

api = REST(API_KEY, API_SECRET, base_url=BASE_URL)


# ========= Universe =========
SYMBOLS: List[str] = [
    "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "NFLX", "BA", "MU", "JPM", "PEP",
    "KO", "COST", "PG", "MRK", "HD",
]


# ========= Helpers =========
def get_daily_vwap(symbol: str) -> Optional[float]:
    """
    Get today's VWAP from daily bars (v2). Returns None if not available.
    """
    try:
        bars = api.get_bars(symbol, TimeFrame.Day, limit=1)
        df = bars.df
        if df is None or df.empty:
            return None
        # Alpaca v2 includes 'vwap' on daily bars
        return float(df["vwap"].iloc[-1])
    except Exception as e:
        logging.error(f"VWAP fetch error {symbol}: {e}")
        return None


def get_last_price(symbol: str) -> Optional[float]:
    """
    Get latest trade price from snapshot. Returns None if not available.
    """
    try:
        snap = api.get_snapshot(symbol)
        # latest trade price property name is 'p' on the trade object
        price = getattr(snap.latest_trade, "p", None)
        return float(price) if price is not None else None
    except Exception as e:
        logging.error(f"Last price fetch error {symbol}: {e}")
        return None


def buy_fractional(symbol: str, pct_of_bp: float = 0.05, min_notional: float = 5.0):
    """
    Buy using notional (fractional). Default 5% of current Buying Power,
    with a floor of $5 to avoid tiny orders.
    """
    try:
        acct = api.get_account()
        bp = float(acct.buying_power)
        notional = round(max(bp * pct_of_bp, min_notional), 2)

        order = api.submit_order(
            symbol=symbol,
            side="buy",
            type="market",
            time_in_force="day",
            notional=notional,  # <-- fractional (no qty)
        )
        logging.info(f"BUY {symbol} notional=${notional} id={order.id}")
        return order

    except APIError as e:
        logging.error(f"Order error on {symbol}: {e}")
        # graceful retry with smaller notional
        try:
            fallback = round(max(min_notional, notional * 0.5), 2)
            order2 = api.submit_order(
                symbol=symbol,
                side="buy",
                type="market",
                time_in_force="day",
                notional=fallback,
            )
            logging.info(f"BUY-RETRY {symbol} notional=${fallback} id={order2.id}")
            return order2
        except Exception as e2:
            logging.error(f"Retry failed on {symbol}: {e2}")
            return None
    except Exception as e:
        logging.error(f"Unexpected order error on {symbol}: {e}")
        return None


def sell_all_positions():
    """
    Market-sell everything (useful if you want a flat-close routine).
    """
    try:
        positions = api.list_positions()
        for p in positions:
            qty = abs(int(float(p.qty)))
            if qty == 0:
                continue
            side = "sell" if p.side == "long" else "buy"
            api.submit_order(
                symbol=p.symbol,
                side=side,
                type="market",
                time_in_force="day",
                qty=qty,
            )
            logging.info(f"FLAT {p.symbol} qty={qty}")
    except Exception as e:
        logging.error(f"Flat error: {e}")


# ========= Strategy (example) =========
def should_buy(symbol: str) -> bool:
    """
    Example rule: buy if last price > daily VWAP (simple momentum filter).
    """
    vwap = get_daily_vwap(symbol)
    last = get_last_price(symbol)

    if vwap is None or last is None:
        logging.warning(f"Skip {symbol}: missing data (vwap={vwap}, last={last})")
        return False

    logging.info(f"{symbol}: last={last:.2f} | vwap={vwap:.2f}")
    return last > vwap


# ========= Main Loop =========
def main_loop(sleep_seconds: int = 60):
    # sanity: print account
    acct = api.get_account()
    logging.info(
        f"Account ok: buying_power={acct.buying_power}, "
        f"status={acct.status}"
    )

    while True:
        try:
            for sym in SYMBOLS:
                try:
                    if should_buy(sym):
                        buy_fractional(sym, pct_of_bp=0.03, min_notional=10.0)
                except Exception as sym_err:
                    logging.error(f"Symbol loop error {sym}: {sym_err}")

            time.sleep(sleep_seconds)

        except KeyboardInterrupt:
            logging.info("Interrupted by user, exiting.")
            break
        except Exception as e:
            logging.error(f"Run loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    logging.info(
        f"Starting bot | base_url={BASE_URL} | symbols="
        + ",".join(SYMBOLS)
    )
    main_loop(sleep_seconds=60)
