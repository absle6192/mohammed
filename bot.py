# bot.py — Alpaca (SIP) realtime + checks + sample buy

import os, time, json, requests
from datetime import datetime

# ---------- Debug ----------
def dbg(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")

# ---------- ENV ----------
API_KEY    = os.getenv("APCA_API_KEY_ID", "")
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "")
BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"
DATA_FEED  = os.getenv("APCA_API_DATA_FEED", "sip").lower().strip()  # اجعلها sip

if not API_KEY or not API_SECRET:
    raise RuntimeError("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}
JSON_HEADERS = {**HEADERS, "Content-Type": "application/json"}

# ---------- Quick checks ----------
def account_check():
    r = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=8)
    dbg(f"Account HTTP {r.status_code}")
    try:
        j = r.json()
        dbg(f"Account: status={j.get('status')} buying_power={j.get('buying_power')}")
    except Exception:
        dbg(f"Account text: {r.text[:200]}")

def entitlements_check():
    r = requests.get(f"{DATA_URL}/v1beta1/entitlements", headers=HEADERS, timeout=8)
    dbg(f"Entitlements HTTP {r.status_code}")
    try:
        e = r.json()
    except Exception:
        e = {"text": r.text[:200]}
    dbg(f"Entitlements: {json.dumps(e, ensure_ascii=False)[:400]}")

def is_market_open() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/v2/clock", headers=HEADERS, timeout=6)
        if r.status_code != 200:
            dbg(f"Clock HTTP {r.status_code}")
            return True
        return bool(r.json().get("is_open", True))
    except Exception as e:
        dbg(f"Clock exception: {e}")
        return True

# ---------- Data (SIP -> optional fallback) ----------
def get_last_trade_price(symbol: str) -> float | None:
    # حاول بالـ feed من المتغير (المفروض sip)
    feeds = [DATA_FEED]
    # لو مو sip نضيف sip كنسخة احتياط
    if DATA_FEED != "sip":
        feeds.append("sip")

    for feed in feeds:
        url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest?feed={feed}"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            try:
                p = float(r.json()["trade"]["p"])
                dbg(f"{symbol}: ({feed.upper()}) price = {p}")
                return p
            except Exception:
                dbg(f"{symbol}: ({feed}) 200 بدون trade.p")
                return None
        elif r.status_code == 403:
            dbg(f"{symbol}: ({feed}) 403 Forbidden")
            continue
        else:
            dbg(f"{symbol}: ({feed}) HTTP={r.status_code} | {r.text[:120]}")
            continue
    return None

# ---------- Simple order ----------
def dollars_to_qty(dollars: float, price: float) -> int:
    if price <= 0: return 0
    return max(int(dollars // price), 0)

def r2(x: float) -> float:
    return float(f"{x:.2f}")

def place_market_buy(symbol: str, qty: int) -> dict | None:
    if qty <= 0:
        dbg(f"{symbol}: qty=0 skip order")
        return None
    payload = {
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/v2/orders", headers=JSON_HEADERS, data=json.dumps(payload), timeout=10)
    if r.status_code not in (200, 201):
        dbg(f"{symbol}: order HTTP {r.status_code} | {r.text[:200]}")
        return None
    j = r.json()
    dbg(f"{symbol}: 🟢 order placed id={j.get('id')} qty={qty}")
    return j

# ---------- Settings ----------
SYMBOLS          = [s.strip() for s in os.getenv("SYMBOLS", "AAPL,MSFT,NVDA").split(",") if s.strip()]
ENABLE_TRADING   = os.getenv("ENABLE_TRADING", "true").lower() == "true"
DOLLAR_PER_TRADE = float(os.getenv("DOLLAR_PER_TRADE", "200"))
POLL_SECONDS     = int(os.getenv("POLL_SECONDS", "5"))

# ---------- Main ----------
def main():
    dbg(f"Using KEY_ID={API_KEY[:4]}...{API_KEY[-4:]} BASE_URL={BASE_URL} FEED={DATA_FEED}")
    account_check()
    entitlements_check()

    while True:
        mkt = is_market_open()
        for sym in SYMBOLS:
            price = get_last_trade_price(sym)
            if price is None:
                dbg(f"{sym}: لا توجد بيانات.")
                continue
            dbg(f"{sym}: ✅ السعر الحالي = {price}")
            if mkt and ENABLE_TRADING:
                qty = dollars_to_qty(DOLLAR_PER_TRADE, price)
                place_market_buy(sym, qty)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
