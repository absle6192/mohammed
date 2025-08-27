import os
import time
import logging
from typing import Optional, List, Dict
import requests

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ---------------- ENV ----------------
API_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
TRADING_BASE = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip().rstrip("/")
DATA_BASE = os.getenv("APCA_DATA_BASE_URL", "https://data.alpaca.markets").strip().rstrip("/")
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "AAPL,MSFT,AMZN,GOOGL,TSLA").split(",") if s.strip()]

BUY_THRESHOLD = float(os.getenv("BUY_THRESHOLD", "0.003"))
ORDER_NOTIONAL = float(os.getenv("ORDER_NOTIONAL_USD", "1000"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.01"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.005"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SEC", "5"))

HDR = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}

# -------------- Basic Guards --------------
def require_env():
    missing = []
    if not API_KEY: missing.append("APCA_API_KEY_ID")
    if not API_SECRET: missing.append("APCA_API_SECRET_KEY")
    if not TRADING_BASE: missing.append("APCA_API_BASE_URL")
    if not DATA_BASE: missing.append("APCA_DATA_BASE_URL")
    if missing:
        logging.error("MISSING ENV: %s", ", ".join(missing))
        raise SystemExit(1)

# -------------- HTTP with extra logging --------------
def _req(method: str, url: str, **kw) -> requests.Response:
    for attempt in range(5):
        try:
            r = requests.request(method, url, timeout=10, **kw)
            if r.status_code >= 400:
                body = ""
                try:
                    body = r.text[:500]
                except Exception:
                    body = "<no body>"
                logging.warning("HTTP %s on %s (attempt %d) body=%s",
                                r.status_code, url, attempt+1, body)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1 + attempt)
                    continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            logging.warning("HTTP EXC %s (attempt %d) url=%s", e, attempt+1, url)
            time.sleep(1 + attempt)
    r = requests.request(method, url, timeout=10, **kw)
    r.raise_for_status()
    return r

# -------------- Alpaca helpers --------------
def get_clock() -> Dict:
    return _req("GET", f"{TRADING_BASE}/v2/clock", headers=HDR).json()

def get_latest_bar(symbol: str) -> Optional[Dict]:
    r = _req("GET", f"{DATA_BASE}/v2/stocks/{symbol}/bars/latest", headers=HDR)
    j = r.json()
    return j.get("bar")

def get_latest_trade_price(symbol: str) -> Optional[float]:
    r = _req("GET", f"{DATA_BASE}/v2/stocks/{symbol}/trades/latest", headers=HDR)
    t = r.json().get("trade")
    return float(t["p"]) if t and "p" in t else None

def has_position(symbol: str) -> bool:
    r = requests.get(f"{TRADING_BASE}/v2/positions/{symbol}", headers=HDR, timeout=10)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True

def submit_bracket_market_buy(symbol: str, notional: float, tp_price: float, sl_price: float) -> Dict:
    payload = {
        "symbol": symbol,
        "notional": str(notional),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": f"{tp_price:.2f}"},
        "stop_loss": {"stop_price": f"{sl_price:.2f}"}
    }
    r = _req("POST", f"{TRADING_BASE}/v2/orders", headers=HDR, json=payload)
    return r.json()

# -------------- Math --------------
def pct_change(a: float, b: float) -> float:
    if a == 0: return 0.0
    return (b - a) / a

# -------------- Core Loop --------------
def scan_and_trade(symbols: List[str]):
    logging.info("BOOT: Worker starting")
    logging.info("ENV: TRADING_BASE=%s | DATA_BASE=%s", TRADING_BASE, DATA_BASE)
    logging.info("CONFIG: tickers=%d | threshold=%.4f | notional=%.2f | TP=%.3f | SL=%.3f | interval=%ds",
                 len(symbols), BUY_THRESHOLD, ORDER_NOTIONAL, TAKE_PROFIT_PCT, STOP_LOSS_PCT, SCAN_INTERVAL)

    require_env()

    # Print account info to confirm authentication
    acct = _req("GET", f"{TRADING_BASE}/v2/account", headers=HDR).json()
    logging.info("ACCOUNT: id=%s status=%s buying_power=%s",
                 acct.get("id"), acct.get("status"), acct.get("buying_power"))

    while True:
        try:
            clock = get_clock()
            is_open = bool(clock.get("is_open"))
            logging.info("MARKET: open=%s", is_open)

            if not is_open:
                time.sleep(SCAN_INTERVAL)
                continue

            for sym in symbols:
                try:
                    if has_position(sym):
                        logging.info("POSITION: %s already held -> skip", sym)
                        continue

                    bar = get_latest_bar(sym)
                    if not bar:
                        logging.info("DATA: no latest bar for %s", sym)
                        continue

                    today_open = float(bar.get("o", 0.0))
                    last_price = get_latest_trade_price(sym) or float(bar.get("c", 0.0))
                    if today_open <= 0 or last_price <= 0:
                        logging.info("DATA: bad prices for %s (o=%.4f last=%.4f)", sym, today_open, last_price)
                        continue

                    change = pct_change(today_open, last_price)
                    logging.info("SCAN: %s open=%.4f last=%.4f change=%.4f", sym, today_open, last_price, change)

                    if change >= BUY_THRESHOLD:
                        tp = last_price * (1 + TAKE_PROFIT_PCT)
                        sl = last_price * (1 - STOP_LOSS_PCT)
                        order = submit_bracket_market_buy(sym, ORDER_NOTIONAL, tp, sl)
                        logging.info("ORDER: BUY %s notional=%.2f @~%.2f TP=%.2f SL=%.2f id=%s",
                                     sym, ORDER_NOTIONAL, last_price, tp, sl, order.get("id"))
                        time.sleep(1)
                except Exception as e:
                    logging.exception("SYMBOL ERROR (%s): %s", sym, e)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logging.exception("LOOP ERROR: %s", e)
            time.sleep(3)

if __name__ == "__main__":
    try:
        scan_and_trade(SYMBOLS)
    except Exception as e:
        logging.exception("FATAL: %s", e)
        raise
