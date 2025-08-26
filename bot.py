# bot.py
# bot.py
import os
import time
import requests
from datetime import datetime, UTC

# ========== اختبار API ==========
headers = {
    "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
    "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
}
try:
    r = requests.get(
        "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest",
        headers=headers,
        timeout=10
    )
    print("ALPACA_TEST", r.status_code, r.text[:200])
except Exception as e:
    print("ALPACA_TEST_ERROR", str(e))
# ================================
import os
import time
import requests
from datetime import datetime, UTC

# ====================== الإعدادات من متغيرات البيئة ======================
# نمط يدوي (SYMBOLS) أو تلقائي (AUTO_SELECT + UNIVERSE + TOP_K)
AUTO_SELECT = os.getenv("AUTO_SELECT", "false").lower() == "true"
UNIVERSE = [s.strip().upper() for s in os.getenv("UNIVERSE", "").split(",") if s.strip()]
TOP_K = int(os.getenv("TOP_K", "5"))

# لو كان يدوي:
SYMBOLS_MANUAL = [s.strip().upper() for s in os.getenv("SYMBOLS", "AMD,TSLA,AAPL").split(",") if s.strip()]

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
ENABLE_TRADING = os.getenv("ENABLE_TRADING", "true").lower() == "true"
DOLLAR_PER_TRADE = float(os.getenv("DOLLAR_PER_TRADE", "50"))

ENTRY_UP_PCT = float(os.getenv("ENTRY_UP_PCT", "0.6"))
TP_PCT = float(os.getenv("TP_PCT", "1.2"))
SL_PCT = float(os.getenv("SL_PCT", "0.6"))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "300"))

# مفاتيح Alpaca
ALPACA_KEY    = os.getenv("ALPACA_KEY_ID", "") or os.getenv("APCA_API_KEY_ID", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "") or os.getenv("APCA_API_SECRET_KEY", "")

# ====================== عناوين API ======================
ALPACA_DATA_BASE = "https://data.alpaca.markets/v2/stocks"
TRADING_BASE = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

def log(msg: str):
    dt = datetime.now(UTC)
    print(f"[{dt.isoformat()}Z] {msg}", flush=True)

# ====================== دوال مساعدة ======================
def is_market_open() -> bool:
    try:
        r = requests.get(f"{TRADING_BASE}/clock", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            return bool(r.json().get("is_open", False))
    except Exception:
        pass
    return False

def get_position_qty(symbol: str) -> float:
    """يرجع كمية المركز المفتوح في السهم (0 إذا لا يوجد)."""
    try:
        r = requests.get(f"{TRADING_BASE}/positions/{symbol}", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            qty = data.get("qty")
            if qty is not None:
                return float(qty)
    except Exception:
        pass
    return 0.0

def get_last_trade_price(symbol: str):
    try:
        url = f"{ALPACA_DATA_BASE}/{symbol}/trades/latest"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        trade = r.json().get("trade")
        if not trade:
            return None
        return float(trade.get("p"))
    except Exception:
        return None

def place_bracket_buy(symbol: str, price: float, qty: float):
    """
    أمر شراء Market مع Bracket (TP/SL). يدعم الكميات الكسرية (Fractional) إذا كانت مفعّلة في حسابك.
    """
    qty_rounded = float(f"{qty:.3f}")  # 3 منازل عشرية تكفي للكميات الكسرية
    if qty_rounded <= 0:
        return None

    tp = round(price * (1 + TP_PCT / 100), 2)
    sl_stop = round(price * (1 - SL_PCT / 100), 2)

    body = {
        "symbol": symbol,
        "qty": qty_rounded,
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": tp},
        "stop_loss": {"stop_price": sl_stop}
    }
    r = requests.post(f"{TRADING_BASE}/orders", headers=HEADERS, json=body, timeout=10)
    if r.status_code in (200, 201):
        log(f"تم إرسال أمر BUY (Bracket) لـ {symbol} - qty={qty_rounded} | TP={tp} | SL={sl_stop}")
        return r.json()
    else:
        log(f"فشل أمر BUY لـ {symbol}: {r.status_code} | {r.text[:200]}")
        return None

def dollars_to_qty(dollars: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return dollars / price  # كمية كسرية إذا لزم

# آخر قراءة سعر + آخر تنفيذ (للتبريد)
last_price: dict[str, float] = {}
last_exec_time: dict[str, float] = {}

def change_pct(curr: float, prev: float | None) -> float:
    if prev is None or prev <= 0:
        return 0.0
    return (curr / prev - 1.0) * 100.0

# ====================== اختيار تلقائي للأسهم ======================
def pick_top_symbols(universe: list[str], top_k: int) -> list[str]:
    """
    يختار أفضل أسهم لحظيًا بناءً على نسبة الارتفاع مقارنة بالقراءة السابقة في last_price.
    لا يحدث last_price هنا؛ التحديث يتم في الحلقة الرئيسية بعد المعالجة.
    """
    candidates: list[tuple[str, float]] = []  # (symbol, change%)
    for sym in universe:
        p = get_last_trade_price(sym)
        if p is None:
            log(f"{sym}: لا توجد بيانات.")
            continue
        prev = last_price.get(sym)
        chg = change_pct(p, prev)
        if prev is not None:
            log(f"{sym}: آخر سعر = {p} | تغيّر منذ القراءة السابقة = {chg:.2f}%")
            candidates.append((sym, chg))
        else:
            log(f"{sym}: آخر سعر = {p} (نحتاج قراءة سابقة للمقارنة)")

    candidates.sort(key=lambda t: t[1], reverse=True)
    if not candidates:
        return []
    return [sym for sym, _ in candidates[:max(1, top_k)]]

# ====================== شروط الدخول ======================
def should_enter_long(symbol: str, price: float) -> bool:
    prev = last_price.get(symbol)
    if prev is None:
        return False
    chg = change_pct(price, prev)
    if chg >= ENTRY_UP_PCT:
        t_prev = last_exec_time.get(symbol, 0)
        if (time.time() - t_prev) < COOLDOWN_SEC:
            return False
        if get_position_qty(symbol) > 0:
            return False
        return True
    return False

# ====================== الحلقة الرئيسية ======================
def main():
    mode = "تلقائي (UNIVERSE)" if AUTO_SELECT else "يدوي (SYMBOLS)"
    watch_list = UNIVERSE if AUTO_SELECT else SYMBOLS_MANUAL
    log(f"بدء تشغيل البوت — النمط: {mode}")
    log(f"سلة المراقبة: {', '.join(watch_list)}")
    log(f"إعدادات: تحديث كل {POLL_SECONDS}s | دخول عند +{ENTRY_UP_PCT}% | TP={TP_PCT}% | SL={SL_PCT}%")
    if not ENABLE_TRADING:
        log("ملاحظة: ENABLE_TRADING=false (لن يتم إرسال أوامر).")

    while True:
        market_open = is_market_open()

        if AUTO_SELECT:
            # اختر أفضل الأسهم لهذه الدورة
            selected = pick_top_symbols(UNIVERSE, TOP_K)
            if selected:
                log(f"اختيار تلقائي — أفضل {min(TOP_K, len(selected))}: {', '.join(selected)}")
            else:
                log("اختيار تلقائي — لا يوجد مرشح حتى الآن (نحتاج قراءتين على الأقل/بيانات).")
        else:
            selected = SYMBOLS_MANUAL

        # نفذ المنطق على القائمة المختارة
        for sym in selected:
            price = get_last_trade_price(sym)
            if price is None:
                log(f"{sym}: لا توجد بيانات.")
                continue

            log(f"{sym}: آخر سعر = {price}")

            if market_open and ENABLE_TRADING and should_enter_long(sym, price):
                qty = dollars_to_qty(DOLLAR_PER_TRADE, price)
                if qty > 0:
                    res = place_bracket_buy(sym, price, qty)
                    if res is not None:
                        last_exec_time[sym] = time.time()

            # حدّث القراءة السابقة في النهاية
            last_price[sym] = price

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
