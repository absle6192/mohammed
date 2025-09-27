import os, time, threading, requests, logging
from dataclasses import dataclass
from typing import Dict, Optional
from alpaca_trade_api.rest import REST
from alpaca_trade_api.stream import Stream

log = logging.getLogger("combo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==== Env ====
APCA_API_KEY_ID     = os.getenv("APCA_API_KEY_ID","")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY","")
APCA_API_BASE_URL   = os.getenv("APCA_API_BASE_URL","https://paper-api.alpaca.markets").rstrip("/")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID","")

EARLY_SIGNALS   = os.getenv("EARLY_SIGNALS","1") == "1"
IMB_UP          = float(os.getenv("IMBALANCE_UP","1.5"))
IMB_DN          = float(os.getenv("IMBALANCE_DN","0.67"))
MAX_SPREAD_USD  = float(os.getenv("MAX_SPREAD_USD","0.03"))
MOMENTUM_TH     = float(os.getenv("MOMENTUM_TH","0.0005"))
HOLD_SEC        = float(os.getenv("HOLD_SEC","2"))
COOLDOWN_SEC    = float(os.getenv("COOLDOWN_SEC","45"))
REFRESH_SEC     = float(os.getenv("REFRESH_SEC","1.0"))

WATCH_REFRESH_SEC = float(os.getenv("WATCH_REFRESH_SEC","1.0"))
MIN_MOVE_USD      = float(os.getenv("MIN_MOVE_USD","0.05"))
MAX_SILENCE_SEC   = float(os.getenv("MAX_SILENCE_SEC","60"))

# عدّل قائمتك (أسهمك الثمانية)
WATCH = ["AAPL","NVDA","TSLA","MSFT","AMZN","META","GOOGL","AMD"]

# ==== helpers ====
def tg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=8
        )
    except: pass

api = REST(APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL)

def latest_trade(symbol) -> Optional[float]:
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price)
    except Exception as e:
        log.warning(f"latest_trade {symbol}: {e}")
        return None

def latest_quote(symbol):
    try:
        q = api.get_latest_quote(symbol)
        # يحتاج بيانات تكتيكية مفعّلة
        return float(q.bid_price), float(q.ask_price), float(q.bid_size or 0), float(q.ask_size or 0)
    except Exception as e:
        log.warning(f"latest_quote {symbol}: {e}")
        return None

# ==== Early signals (order-flow) ====
last_ok   : Dict[str, float] = {s: 0 for s in WATCH}
last_sent : Dict[str, float] = {s: 0 for s in WATCH}
last_px   : Dict[str, float] = {}

def momentum_small(symbol, horizon_sec=2.0):
    """زخم بسيط: فرق السعر النسبي خلال أفق قصير."""
    now = time.time()
    p_now = latest_trade(symbol)
    if p_now is None: return 0.0
    p_prev = last_px.get(symbol, p_now)
    last_px[symbol] = p_now
    # لو ما مر وقت كفاية، استخدم فرق بسيط
    if p_prev == 0: return 0.0
    return (p_now / p_prev) - 1.0

def early_signals_loop():
    tg("✅ إشارات مبكّرة: شغّالة")
    while EARLY_SIGNALS:
        now = time.time()
        for s in WATCH:
            q = latest_quote(s)
            if not q: continue
            bid, ask, bsz, asz = q
            spread = max(0.0, ask - bid)
            if spread == 0: continue

            imb = (bsz / asz) if asz > 0 else 999.0  # تفوق طلب
            mom = momentum_small(s)

            good_up = (imb >= IMB_UP) and (spread <= MAX_SPREAD_USD) and (mom >= MOMENTUM_TH)
            good_dn = (imb <= IMB_DN) and (spread <= MAX_SPREAD_USD) and (mom <= -MOMENTUM_TH)

            # ثبات الشرط HOLD_SEC
            prev_ok = last_ok.get(s, 0)
            if good_up or good_dn:
                if prev_ok == 0:
                    last_ok[s] = now
                elif now - prev_ok >= HOLD_SEC:
                    # Cooldown
                    if now - last_sent.get(s, 0) >= COOLDOWN_SEC:
                        p = latest_trade(s) or bid
                        if good_up:
                            tg(f"📈 إشارة مبكّرة — {s}\nالطلب أقوى ({imb:.2f}×) | السبريد ${spread:.02f}\nزخم إيجابي بسيط\nالسعر الآن: ${p:.2f}")
                        else:
                            tg(f"📉 إشارة مبكّرة — {s}\nالعرض أقوى ({(1/imb if imb>0 else 0):.2f}× تقريبًا) | السبريد ${spread:.02f}\nزخم سلبي بسيط\nالسعر الآن: ${p:.2f}")
                        last_sent[s] = now
                        last_ok[s]   = 0
            else:
                # لو كان فيه OK قبل قليل ثم اختفى بسرعة ممكن ترسل إلغاء (اختياري)
                if prev_ok != 0 and (now - prev_ok) <= HOLD_SEC:
                    tg(f"⚠️ إلغاء الإشارة — {s}\nاختفى تفوق الطلب/العرض قبل التأكيد")
                last_ok[s] = 0

        time.sleep(REFRESH_SEC)

# ==== Price-follow بعد الشراء ====
@dataclass
class Track:
    entry: float
    qty: float
    running: bool = True
    last_px: Optional[float] = None
    last_ts: float = 0.0

tracks: Dict[str, Track] = {}

def fmt2(x): return f"{x:.2f}"

def follow_loop(symbol: str):
    tr = tracks[symbol]
    tg(f"👀 متابعة {symbol}\nسعر الدخول: ${fmt2(tr.entry)} | كمية: {tr.qty}")
    while tr.running:
        px = latest_trade(symbol)
        if px is None:
            time.sleep(WATCH_REFRESH_SEC); continue
        now = time.time()
        moved = (tr.last_px is None) or (abs(px - tr.last_px) >= MIN_MOVE_USD)
        silent = (now - tr.last_ts) >= MAX_SILENCE_SEC
        if moved or silent:
            diff = px - tr.entry
            arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "→")
            sign = "+" if diff > 0 else ""
            tg(f"{symbol} ${fmt2(px)}  {arrow} {sign}{fmt2(diff)}$  (من دخول ${fmt2(tr.entry)})")
            tr.last_px = px
            tr.last_ts = now
        time.sleep(WATCH_REFRESH_SEC)

def start_track(sym: str, entry: float, qty: float):
    if sym in tracks:
        t = tracks[sym]
        new_qty = t.qty + qty
        new_entry = (t.entry*t.qty + entry*qty)/new_qty if new_qty>0 else entry
        t.entry, t.qty = new_entry, new_qty
        tg(f"🔁 تحديث متابعة {sym}\nمتوسط دخول جديد: ${fmt2(t.entry)} | كمية: {t.qty}")
        return
    tracks[sym] = Track(entry=entry, qty=qty)
    threading.Thread(target=follow_loop, args=(sym,), daemon=True).start()

def stop_track_if_closed(sym: str):
    try:
        _ = api.get_position(sym)  # لو ما فيه مركز سترمي استثناء
    except Exception:
        if sym in tracks and tracks[sym].running:
            tracks[sym].running = False
            tg(f"🛑 إيقاف متابعة {sym} (المركز أغلق).")

# ==== Stream trade_updates ====
stream = Stream(APCA_API_KEY_ID, APCA_API_SECRET_KEY, base_url=APCA_API_BASE_URL)

@stream.on("trade_updates")
async def on_trade_update(data):
    try:
        ev     = data.event
        order  = data.order
        side   = order.get("side")
        status = order.get("status")
        sym    = order.get("symbol")
        avg_p  = float(order.get("filled_avg_price") or 0.0)
        qty    = float(order.get("filled_qty") or 0.0)

        if ev == "fill" and status == "filled":
            if side == "buy":
                start_track(sym, avg_p, qty)
                tg(f"🟢 تم شراء {sym}\nسعر الدخول: ${fmt2(avg_p)} | كمية: {qty}")
            elif side == "sell":
                stop_track_if_closed(sym)
    except Exception as e:
        log.warning(f"on_trade_update error: {e}")

# ==== main ====
if __name__ == "__main__":
    tg("✅ البوت الثاني بدأ: إشارات مبكّرة + متابعة السعر بعد الشراء.")
    if EARLY_SIGNALS:
        threading.Thread(target=early_signals_loop, daemon=True).start()
    stream.run()
