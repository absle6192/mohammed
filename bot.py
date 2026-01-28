import os
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Alpaca
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient


# =========================
# ENV (Render) — نفس أسمائك بالضبط
# =========================
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "").strip()
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "").strip()
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "").strip()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# إعدادات قابلة للتغيير من Render (اختياري)
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS", "TSLA,NVDA,AAPL,CRWD,AMZN,AMD,GOOGL,MU"
).split(",") if s.strip()]

POLL_SEC = int(os.getenv("POLL_SEC", "20"))                 # كل كم ثانية يفحص
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "5"))          # آخر كم دقيقة للزخم
MOM_THRESHOLD_PCT = float(os.getenv("MOM_THRESHOLD_PCT", "0.20"))  # % حد الإشارة
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "120"))        # لا يكرر نفس التنبيه بسرعة
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "1800"))     # كل 30 دقيقة يرسل نبضة (اختياري)


def require(name: str, val: str):
    if not val:
        raise RuntimeError(f"Missing {name}")


require("APCA_API_BASE_URL", APCA_API_BASE_URL)
require("APCA_API_KEY_ID", APCA_API_KEY_ID)
require("APCA_API_SECRET_KEY", APCA_API_SECRET_KEY)
require("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
require("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

CHAT_ID = int(TELEGRAM_CHAT_ID)

# =========================
# Clients
# =========================
data_client = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)
trade_client = TradingClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY, paper=True, url_override=APCA_API_BASE_URL)


# =========================
# Signal model
# =========================
@dataclass
class BestSignal:
    symbol: str
    side: str            # "BUY" or "SHORT" or "WAIT"
    mom_pct: float
    price: float
    reason: str
    ts: float


STATE: Dict[str, object] = {
    "last_best_key": None,      # (symbol, side)
    "last_sent_ts": 0.0,
    "last_heartbeat_ts": 0.0,
    "last_best": None,          # BestSignal
}


# =========================
# Helpers
# =========================
def now_sa_str() -> str:
    # Saudi time = UTC+3
    return datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")

def pct_change(old: float, new: float) -> float:
    if old <= 0:
        return 0.0
    return (new - old) / old * 100.0

def decide_side(mom_pct: float, threshold_pct: float) -> str:
    if mom_pct >= threshold_pct:
        return "BUY"
    if mom_pct <= -threshold_pct:
        return "SHORT"
    return "WAIT"

def format_best_ar(best: BestSignal) -> str:
    if best.side == "BUY":
        icon = "📈"
        side_ar = "شراء (Long)"
    elif best.side == "SHORT":
        icon = "📉"
        side_ar = "شورت (Short)"
    else:
        icon = "⏸️"
        side_ar = "انتظار"

    return (
        f"{icon} <b>أفضل فرصة الآن</b>\n"
        f"• السهم: <b>{best.symbol}</b>\n"
        f"• القرار: <b>{side_ar}</b>\n"
        f"• السعر: <b>{best.price:.2f}</b>\n"
        f"• الزخم ({LOOKBACK_MIN}د): <b>{best.mom_pct:+.3f}%</b>\n"
        f"• السبب: {best.reason}\n"
        f"• الوقت: <code>{now_sa_str()}</code>"
    )

async def tg_send(app: Application, text: str):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

def get_market_status_ar() -> str:
    try:
        clock = trade_client.get_clock()
        if clock.is_open:
            return "🟢 السوق الأمريكي مفتوح"
        return "🔴 السوق الأمريكي مغلق"
    except Exception:
        return "⚠️ تعذر جلب حالة السوق"

async def fetch_momentum(symbol: str) -> Optional[Tuple[float, float]]:
    """
    يرجع (mom_pct, last_price) لآخر LOOKBACK_MIN دقائق.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=LOOKBACK_MIN + 2)

    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        limit=200,
    )
    bars = data_client.get_stock_bars(req).data.get(symbol, [])
    if not bars or len(bars) < (LOOKBACK_MIN + 1):
        return None

    # نأخذ أول بار من النافذة + آخر بار
    # نستخدم close كمقياس بسيط
    old_price = float(bars[0].close)
    last_price = float(bars[-1].close)
    mom_pct = pct_change(old_price, last_price)
    return mom_pct, last_price

async def compute_best() -> Optional[BestSignal]:
    results: List[BestSignal] = []

    for sym in SYMBOLS:
        try:
            r = await fetch_momentum(sym)
            if not r:
                continue
            mom_pct, last_price = r
            side = decide_side(mom_pct, MOM_THRESHOLD_PCT)

            # سبب عربي بسيط
            if side == "BUY":
                reason = f"زخم صاعد خلال آخر {LOOKBACK_MIN} دقائق"
            elif side == "SHORT":
                reason = f"زخم هابط خلال آخر {LOOKBACK_MIN} دقائق"
            else:
                reason = "ما فيه أفضلية واضحة"

            results.append(BestSignal(
                symbol=sym,
                side=side,
                mom_pct=mom_pct,
                price=last_price,
                reason=reason,
                ts=time.time()
            ))
        except Exception:
            continue

    if not results:
        return None

    # اختر الأقوى بالزخم المطلق
    best = sorted(results, key=lambda x: abs(x.mom_pct), reverse=True)[0]
    return best

def should_notify(best: BestSignal) -> bool:
    now_ts = time.time()
    last_sent = float(STATE["last_sent_ts"])
    last_key = STATE["last_best_key"]
    heartbeat_ts = float(STATE["last_heartbeat_ts"])

    key = (best.symbol, best.side)

    # نرسل فقط عند BUY/SHORT، أو heartbeat (اختياري)
    is_action = best.side in ("BUY", "SHORT")

    if last_key is None and is_action:
        return True

    if is_action and key != last_key and (now_ts - last_sent) >= COOLDOWN_SEC:
        return True

    # نبضة كل HEARTBEAT_SEC حتى لو WAIT (للتأكد انه شغال)
    if (now_ts - heartbeat_ts) >= HEARTBEAT_SEC:
        return True

    return False

def mark_notified(best: BestSignal):
    STATE["last_best_key"] = (best.symbol, best.side)
    STATE["last_sent_ts"] = time.time()
    STATE["last_heartbeat_ts"] = time.time()
    STATE["last_best"] = best


# =========================
# Telegram Commands
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت شغّال\n\n"
        "الأوامر:\n"
        "/status - حالة السوق\n"
        "/best - أفضل سهم الآن (شراء/شورت)\n",
        parse_mode=ParseMode.HTML
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"{get_market_status_ar()}\n"
        f"📌 المتابعة: {', '.join(SYMBOLS)}\n"
        f"⏱️ فحص كل: {POLL_SEC} ثانية\n"
        f"⚡ حد الإشارة: {MOM_THRESHOLD_PCT:.2f}% (آخر {LOOKBACK_MIN} دقائق)\n"
        f"🕒 {now_sa_str()} (السعودية)"
    )
    await update.message.reply_text(msg)

async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    best = await compute_best()
    if not best:
        await update.message.reply_text("⛔️ ما قدرت أجيب بيانات كفاية الآن. جرّب بعد دقيقة.")
        return
    await update.message.reply_text(format_best_ar(best), parse_mode=ParseMode.HTML)


# =========================
# Background monitor loop
# =========================
async def monitor_loop(app: Application):
    # رسالة تشغيل
    await tg_send(app, "✅ <b>البوت اشتغل بنجاح</b>\n🕒 <code>" + now_sa_str() + "</code>\n📡 بدأ يراقب الأسهم ويرسل إشارات (شراء/شورت).")

    while True:
        try:
            best = await compute_best()
            if best:
                if should_notify(best):
                    # لو Heartbeat و best WAIT، نرسل نبضة مختصرة بدل تنبيه
                    if best.side == "WAIT" and (time.time() - float(STATE["last_heartbeat_ts"])) >= HEARTBEAT_SEC:
                        await tg_send(app, "💓 البوت شغال\n🕒 <code>" + now_sa_str() + "</code>")
                        STATE["last_heartbeat_ts"] = time.time()
                    else:
                        await tg_send(app, format_best_ar(best))
                        mark_notified(best)
            await asyncio.sleep(POLL_SEC)

        except Exception as e:
            # لا نطيح البوت بسبب خطأ
            try:
                await tg_send(app, f"⚠️ خطأ مؤقت في المراقبة: <code>{type(e).__name__}</code>")
            except Exception:
                pass
            await asyncio.sleep(10)


async def post_init(app: Application):
    # شغل المراقبة بالخلفية
    app.create_task(monitor_loop(app))


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("best", cmd_best))

    print("🚀 Bot running (polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
