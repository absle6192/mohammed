# bot.py — Alpaca real-time + account test + probe

import os
import time
import json
import requests
from datetime import datetime

# ---------- Debug helpers ----------
def debug_print(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")

def log(msg: str) -> None:
    print(msg)

# ---------- Env / Config ----------
API_KEY    = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"  # لا تضف /v2 هنا

if not API_KEY or not API_SECRET:
    raise Exception("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY in environment")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}
JSON_HEADERS = {**HEADERS, "Content-Type": "application/json"}

# ---------- One-time account test ----------
def account_test() -> None:
    try:
        r = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=6)
        debug_print(f"Account test HTTP {r.status_code}")
        try:
            body = r.json()
        except Exception:
            body = {"text": r.text[:300]}
        # اطبع أهم الحقول فقط
        acc = {k: body.get(k) for k in ("id", "account_number", "status", "buying_power")}
        debug_print(f"Account summary: {acc}")
    except Exception as e:
        debug_print(f"Account test exception: {e}")

# ---------- Market status ----------
def is_market_open() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/v2/clock", headers=HEADERS, timeout=5)
        if r.status_code != 200:
            debug_print(f"Clock HTTP {r.status_code} | {r.text[:120]}")
            return True
        return bool(r.json().get("is_open", True))
    except Exception as e:
        debug_print(f"Clock exception: {e}")
        return True

# ---------- Data fetch (SIP → IEX fallback) ----------
def get_last_trade_price(symbol: str) -> float | None:
    def _fetch(feed: str):
        url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest?feed={feed}"
        r = requests.get(url, headers=HEADERS, timeout=6)
        try:
            body = r.json()
        except Exception:
            body = None
        return r.status_code, body

    # جرّب SIP أولاً
    code, data = _fetch("sip")
    if code == 200 and data and data.get("trade") and "p" in data["trade"]:
        price = float(data["trade"]["p"])
        debug_print(f"{symbol}: (Alpaca SIP) price = {price}")
        return price
    elif code == 403:
        debug_print(f"{symbol}: 403 على SIP — سنجرب IEX")

    # fallback إلى IEX
    code, data = _fetch("iex")
    if code == 200 and data and data.get("trade") and "p" in data["trade"]:
        price = float(data["trade"]["p"])
        debug_print(f"{symbol}: (Alpaca IEX) price = {price}")
        return price

    debug_print(f"{symbol}: فشل الجلب | HTTP={code} | body_keys={list((data or {}).keys())}")
    return None

# ---------- Order placement (bracket sample) ----------
def dollars_to_qty(dollars: float, price: float) -> int:
    if not price or price <= 0:
        return 0
    qty = int(dollars // price)
    return max(qty, 0)

def round2(x: float) -> float:
    return float(f"{x:.2f}")

def place_bracket_buy(symbol: str, price: float, qty: int) -> dict | None:
    if qty <= 0:
        debug_print(f"{symbol}: qty <= 0 — skip")
        return None
    tp = round2(price * 1.005)   # +0.5%
    sl = round2(price * 0.995)   # -0.5%
    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": tp},
        "stop_loss": {"stop_price": sl},
    }
    try:
        r = requests.post(f"{BASE_URL}/v2/orders", headers=JSON_HEADERS, data=json.dumps(payload), timeout=8)
        if r.status_code not in (200, 201):
            debug_print(f"{symbol}: order HTTP {r.status_code} | {r.text[:180]}")
            return None
        data = r.json()
        debug_print(f"{symbol}: 🟢 order placed id={data.get('id')} qty={qty}")
        return data
    except Exception as e:
        debug_print(f"{symbol}: order exception: {e}")
        return None

# ---------- Settings ----------
SYMBOLS = ["MSFT", "NVDA", "AAPL"]       # عدّل كما تريد
ENABLE_TRADING = True                    # اجعلها False للاختبار بدون أوامر
DOLLAR_PER_TRADE = float(os.getenv("DOLLAR_PER_TRADE", "1000"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "5"))

# ---------- Probe (optional) ----------
def probe(symbol: str = "AAPL") -> None:
    url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest?feed=sip"
    r = requests.get(url, headers=HEADERS, timeout=6)
    debug_print(f"Probe {symbol} SIP -> HTTP {r.status_code}")
    try:
        debug_print(f"Probe body: {r.json()}")
    except Exception:
        debug_print(f"Probe text: {r.text[:200]}")

# ---------- Main ----------
def main() -> None:
    debug_print("Bot starting…")
    account_test()          # يتأكد من صحة المفاتيح والخطة
    # probe("AAPL")        # شغّلها مرة واحدة لو حاب تشيك، ثم علّقها

    while True:
        market_open = is_market_open()
        for sym in SYMBOLS:
            price = get_last_trade_price(sym)

            if price is None:
                debug_print(f"{sym}: ⚠️ API returned None")
                debug_print(f"{sym}: لا توجد بيانات.")
                continue
            else:
                debug_print(f"{sym}: ✅ السعر الحالي = {price}")
            log(f"{sym}: آخر سعر = {price}")

            if market_open and ENABLE_TRADING:
                qty = dollars_to_qty(DOLLAR_PER_TRADE, price)
                if qty > 0:
                    place_bracket_buy(sym, price, qty)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
