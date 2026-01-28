import os
import asyncio
from datetime import datetime, timezone

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# ----------------------------
# Config
# ----------------------------
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "TSLA,NVDA,AAPL,CRWD,AMZN,AMD,GOOGL,MU").split(",") if s.strip()]
CHECK_EVERY_SEC = int(os.getenv("CHECK_EVERY_SEC", "15"))
MOM_THRESHOLD = float(os.getenv("MOM_THRESHOLD", "0.15"))

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "")
APCA_BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_CHAT_ID")
if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    raise RuntimeError("Missing Alpaca API keys (APCA_API_KEY_ID / APCA_API_SECRET_KEY)")


# Alpaca data client (keys only – base url not needed for data client)
data_client = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)

# State
last_signal_by_symbol = {}  # symbol -> ("LONG"/"SHORT"/"WAIT", last_price, reason)
last_best = None


def classify_signal(bars_6):
    """
    bars_6: list of 6 close prices (old->new)
    Returns: (signal, last_price, reason, mom, sma5)
    """
    closes = [b.close for b in bars_6]
    last_price = float(closes[-1])
    sma5 = sum(float(x) for x in closes[-5:]) / 5.0
    mom = float(closes[-1]) - float(closes[0])  # ~5 دقائق

    # قواعد بسيطة
    if mom >= MOM_THRESHOLD and last_price >= sma5:
        return "LONG", last_price, "زخم إيجابي + فوق متوسط 5 دقائق", mom, sma5
    if mom <= -MOM_THRESHOLD and last_price <= sma5:
        return "SHORT", last_price, "زخم سلبي + تحت متوسط 5 دقائق", mom, sma5
    return "WAIT", last_price, "ما فيه أفضلية واضحة الآن", mom, sma5


async def fetch_symbol_signal(symbol: str):
    # نطلب آخر 6 دقائق (1-min bars)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        limit=6
    )
    bars = data_client.get_stock_bars(req).data.get(symbol, [])
    if len(bars) < 6:
        return None

    signal, last_price, reason, mom, sma5 = classify_signal(bars)
    return {
        "symbol": symbol,
        "signal": signal,
        "price": last_price,
        "reason": reason,
        "mom": mom,
        "sma5": sma5
    }


def score(sig: dict):
    # نرتّب الأفضل حسب قوة الزخم
    # LONG الأعلى mom، SHORT الأكثر سلبية
    if sig["signal"] == "LONG":
        return abs(sig["mom"])
    if sig["signal"] == "SHORT":
        return abs(sig["mom"])
    return 0.0


def format_signal_msg(sig: dict):
    s = sig["signal"]
    emoji = "📈" if s == "LONG" else ("📉" if s == "SHORT" else "⏸️")
    label = "شراء (Long)" if s == "LONG" else ("شورت (Short)" if s == "SHORT" else "انتظار")

    return (
        f"{emoji} إشارة {label}\n"
        f"السهم: {sig['symbol']}\n"
        f"السعر: {sig['price']:.2f}\n"
        f"السبب: {sig['reason']}\n"
        f"الزخم(≈5د): {sig['mom']:.3f}\n"
        f"متوسط 5د: {sig['sma5']:.2f}\n"
    )


async def send_telegram(app: Application, text: str):
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)


async def scan_and_notify(app: Application):
    global last_best

    signals = []
    for sym in SYMBOLS:
        try:
            sig = await asyncio.to_thread(fetch_symbol_signal, sym)
            # asyncio.to_thread يرجع coroutine؟ لا: fetch_symbol_signal async.
        except TypeError:
            # لأن fetch_symbol_signal async، نناديها مباشرة:
            sig = await fetch_symbol_signal(sym)
        except Exception:
            sig = None

        if not sig:
            continue

        signals.append(sig)

        prev = last_signal_by_symbol.get(sym)
        now_tuple = (sig["signal"], round(sig["price"], 2), sig["reason"])
        if prev != now_tuple and sig["signal"] != "WAIT":
            # نرسل فقط عند LONG/SHORT وتغيّر الحالة
            await send_telegram(app, format_signal_msg(sig))
        last_signal_by_symbol[sym] = now_tuple

    if not signals:
        return

    # اختيار أفضل سهم الآن (الأقوى زخمًا)
    best = max(signals, key=score)
    best_key = (best["symbol"], best["signal"], round(best["price"], 2))
    if best["signal"] != "WAIT" and best_key != last_best:
        last_best = best_key
        await send_telegram(app, "⭐️ أفضل فرصة الآن:\n" + format_signal_msg(best))


# ----------------------------
# Telegram commands
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ شات جبتي للأسهم جاهز.\n"
        "الأوامر:\n"
        "/status - حالة البوت\n"
        "/best - أفضل سهم الآن"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🟢 البوت شغال\n"
        f"الأسهم: {', '.join(SYMBOLS)}\n"
        f"فحص كل: {CHECK_EVERY_SEC} ثانية\n"
        f"عتبة الزخم: {MOM_THRESHOLD}\n"
        f"الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # نجيب أفضل سهم لحظيًا عند الطلب
    signals = []
    for sym in SYMBOLS:
        sig = await fetch_symbol_signal(sym)
        if sig:
            signals.append(sig)

    if not signals:
        await update.message.reply_text("⛔️ ما قدرت أجيب بيانات الآن.")
        return

    best = max(signals, key=score)
    await update.message.reply_text("⭐️ أفضل سهم الآن:\n" + format_signal_msg(best))


async def periodic_job(app: Application):
    while True:
        try:
            await scan_and_notify(app)
        except Exception:
            # لا نطيح البوت بسبب خطأ مؤقت
            pass
        await asyncio.sleep(CHECK_EVERY_SEC)


async def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("best", cmd_best))

    # تشغيل البوت + المهمة الدورية
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # أرسل رسالة تشغيل
    await send_telegram(application, "✅ تم تشغيل شات جبتي للأسهم (إشارات فقط).")

    await periodic_job(application)


if __name__ == "__main__":
    asyncio.run(main())
