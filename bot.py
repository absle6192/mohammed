import os
import time
import json
import logging
from typing import List, Dict
import requests

# ----------------------------
# إعدادات اللوج
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

# ----------------------------
# قراءة المتغيرات من البيئة
# ----------------------------
ALPACA_API_BASE_URL = os.getenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL_BASE       = os.getenv("DATA_URL_BASE",       "https://data.alpaca.markets")
API_KEY             = os.getenv("APCA_API_KEY_ID",     "").strip()
API_SECRET          = os.getenv("APCA_API_SECRET_KEY", "").strip()

# مهم: الافتراضي iex – لا نستخدم sip
DATA_FEED           = os.getenv("APCA_API_DATA_FEED", "iex").strip().lower()
if DATA_FEED not in ("iex", "sip"):
    DATA_FEED = "iex"

DOLLAR_AMOUNT       = float(os.getenv("DOLLAR_AMOUNT", "200"))
ENABLE_TRADING      = os.getenv("ENABLE_TRADING", "true").strip().lower() == "true"
POLL_SECONDS        = int(os.getenv("POLL_SECONDS", "5"))
SYMBOLS_RAW         = os.getenv("SYMBOLS", "AAPL,MSFT,AMZN,GOOGL,NVDA")
SYMBOLS             = [s.strip().upper() for s in SYMBOLS_RAW.split(",") if s.strip()]

# تحقق مبكر من المفاتيح
if not API_KEY or not API_SECRET:
    raise RuntimeError("مفاتيح Alpaca ناقصة: تأكد من ضبط APCA_API_KEY_ID و APCA_API_SECRET_KEY في Render.")

# رؤوس الطلبات
HEADERS_TRADING = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

HEADERS_DATA = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}

# ----------------------------
# دوال مساعدة
# ----------------------------
def chunk_symbols(symbols: List[str], size: int = 50) -> List[List[str]]:
    """نقسم الرموز على دفعات (واجهات ألباكا تسمح بعدة رموز معاً)."""
    out = []
    cur = []
    for s in symbols:
        cur.append(s)
        if len(cur) >= size:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out

def fetch_latest_quotes(symbols: List[str]) -> Dict[str, dict]:
    """
    نجلب آخر عرض/طلب (quote) لكل رمز من Data API v2
    endpoint: /v2/stocks/quotes/latest?symbols=...&feed=iex
    """
    results: Dict[str, dict] = {}
    for batch in chunk_symbols(symbols, 50):
        url = f"{DATA_URL_BASE}/v2/stocks/quotes/latest"
        params = {
            "symbols": ",".join(batch),
            "feed": DATA_FEED,   # <<<<< هنا يضمن IEX
        }
        try:
            r = requests.get(url, headers=HEADERS_DATA, params=params, timeout=10)
            if r.status_code == 403:
                log.error("403 Forbidden من Data API (feed=%s). تأكد أن APCA_API_DATA_FEED=iex وأن اشتراكك يسمح.", DATA_FEED)
                continue
            r.raise_for_status()
            data = r.json().get("quotes", {})
            # شكل الإرجاع: {"AAPL": {"symbol":"AAPL","quote":{...}}, ...}
            for sym, item in data.items():
                results[sym] = item.get("quote") or {}
        except Exception as e:
            log.exception("فشل الجلب لدفعة %s: %s", batch, e)
    return results

def fetch_positions() -> Dict[str, float]:
    """نقرأ المراكز الحالية (عدد الأسهم لكل رمز)."""
    url = f"{ALPACA_API_BASE_URL}/v2/positions"
    try:
        r = requests.get(url, headers=HEADERS_TRADING, timeout=10)
        if r.status_code == 403:
            log.error("403 Forbidden من Trading API. تأكد من المفاتيح والبيئة (Paper/Live).")
            return {}
        r.raise_for_status()
        pos = {}
        for p in r.json():
            pos[p["symbol"].upper()] = float(p.get("qty", 0))
        return pos
    except Exception as e:
        log.exception("فشل قراءة المراكز: %s", e)
        return {}

def place_market_order(symbol: str, notional_usd: float, side: str = "buy"):
    """أمر Market بالقيمة (notional)."""
    url = f"{ALPACA_API_BASE_URL}/v2/orders"
    payload = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "notional": round(notional_usd, 2),
    }
    try:
        r = requests.post(url, headers=HEADERS_TRADING, data=json.dumps(payload), timeout=10)
        if r.status_code == 403:
            log.error("403 Forbidden عند إرسال أمر %s %s: تحقق من الصلاحيات/الحساب.", side, symbol)
            log.error("Body: %s", r.text)
            return None
        r.raise_for_status()
        order = r.json()
        log.info("تم إرسال أمر %s %s بقيمة $%.2f | id=%s", side.upper(), symbol, notional_usd, order.get("id"))
        return order
    except Exception as e:
        log.exception("فشل إرسال أمر %s %s: %s", side, symbol, e)
        return None

# ----------------------------
# استراتيجية بسيطة تجريبية
# ----------------------------
def should_buy(quote: dict) -> bool:
    """
    مثال بسيط: إذا فيه Quote صالح (ask/bid) ننفّذ شراء تجريبي.
    عدّل الشرط لاحقاً بما يناسبك.
    """
    if not quote:
        return False
    # إذا يوجد سعر طلب (ask) موجب
    ask = quote.get("ap")  # ask price
    return isinstance(ask, (int, float)) and ask > 0

def main_loop():
    log.info("بدء البوت ✅ | feed=%s | trading=%s | symbols=%s",
             DATA_FEED, ENABLE_TRADING, ",".join(SYMBOLS))

    while True:
        try:
            quotes = fetch_latest_quotes(SYMBOLS)

            if not quotes:
                log.warning("لا توجد بيانات.")
            else:
                for sym in SYMBOLS:
                    q = quotes.get(sym) or {}
                    ap = q.get("ap")  # ask price
                    bp = q.get("bp")  # bid price
                    ts = q.get("t")
                    log.info("%s: bid=%s | ask=%s | t=%s", sym, bp, ap, ts)

                    if ENABLE_TRADING and should_buy(q):
                        place_market_order(sym, DOLLAR_AMOUNT, side="buy")

        except Exception as e:
            log.exception("خطأ في الحلقة الرئيسية: %s", e)

        time.sleep(POLL_SECONDS)

# ----------------------------
# التشغيل
# ----------------------------
if __name__ == "__main__":
    main_loop()
