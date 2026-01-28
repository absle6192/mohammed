import os
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ================== ENV ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # رقمك مثل: 1682557412

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_CHAT_ID")
if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    raise RuntimeError("Missing Alpaca keys")

# ================== CONFIG ==================
WATCHLIST = ["TSLA", "NVDA", "AAPL", "CRWD", "AMZN", "AMD", "GOOGL", "MU"]
CHECK_ORDERS_EVERY_SEC = int(os.getenv("CHECK_ORDERS_EVERY_SEC", "20"))  # كل كم ثانية يشيّك على تنفيذ الأوامر

# ================== CLIENTS ==================
alpaca_trade = TradingClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY, paper=True)
alpaca_data = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)

# نخزن آخر أمر أرسلناه عشان ما نكرر الرسالة
LAST_NOTIFIED_ORDER_ID = None


# ================== HELPERS ==================
def _to_float(x):
    try:
        return float(x)
    except Exception:
        return None


def format_trade_message(symbol: str, side: str, qty, price, ts: str) -> str:
    side_ar = "شراء ✅" if side.lower() == "buy" else "شورت 🔻"
    return (
        f"📢 *تم تنفيذ صفقة*\n"
        f"• السهم: *{symbol}*\n"
        f"• النوع: *{side_ar}*\n"
        f"• الكمية: *{qty}*\n"
        f"• السعر: *{price}*\n"
        f"• الوقت: `{ts}`"
    )


async def send_to_chat(app: Application, text: str):
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN
    )


# ================== COMMANDS ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "هلا 👋\n"
        "أنا بوت التنبيهات.\n\n"
        "الأوامر:\n"
        "/status - حالة السوق\n"
        "/best - أفضل سهم الآن (شراء/شورت)\n"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clock = alpaca_trade.get_clock()
    is_open = "مفتوح ✅" if clock.is_open else "مغلق ⛔️"
    nxt_open = clock.next_open.isoformat() if clock.next_open else "—"
    nxt_close = clock.next_close.isoformat() if clock.next_close else "—"

    msg = (
        f"🕒 *حالة السوق*\n"
        f"• الآن: *{is_open}*\n"
        f"• الفتح القادم: `{nxt_open}`\n"
        f"• الإغلاق القادم: `{nxt_close}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    يحسب زخم آخر 5 دقائق على فريم 1 دقيقة:
    mom = (آخر إغلاق - إغلاق قبل 5 دقائق) / إغلاق قبل 5 دقائق
    إذا mom موجب => شراء
    إذا mom سالب => شورت
    ويختار الأعلى "بالقيمة المطلقة" (أقوى حركة).
    """
    try:
        end = datetime.now(timezone.utc)
        start = end.replace(minute=end.minute - 15)  # نافذة أكبر شوي للاحتياط

        req = StockBarsRequest(
            symbol_or_symbols=WATCHLIST,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed="iex"  # paper غالبًا يمشي
        )
        bars = alpaca_data.get_stock_bars(req).df

        if bars is None or len(bars) == 0:
            await update.message.reply_text("ما قدرت أجيب بيانات الآن. جرّب بعد دقيقة.")
            return

        best = None  # (score_abs, symbol, mom)
        details = []

        for sym in WATCHLIST:
            try:
                sym_df = bars.loc[sym]
                sym_df = sym_df.sort_index()
                closes = sym_df["close"].tail(6)  # آخر 6 دقائق
                if len(closes) < 6:
                    continue
                old = _to_float(closes.iloc[0])
                last = _to_float(closes.iloc[-1])
                if not old or not last or old == 0:
                    continue
                mom = (last - old) / old
                score = abs(mom)
                details.append((sym, mom, last))
                if best is None or score > best[0]:
                    best = (score, sym, mom, last)
            except Exception:
                continue

        if not best:
            await update.message.reply_text("ما فيه بيانات كفاية للحكم الآن.")
            return

        _, sym, mom, last_price = best
        side = "شراء ✅" if mom >= 0 else "شورت 🔻"
        mom_pct = round(mom * 100, 3)

        msg = (
            f"⭐️ *أفضل سهم الآن*\n"
            f"• السهم: *{sym}*\n"
            f"• التوصية: *{side}*\n"
            f"• الزخم (آخر 5 دقائق): *{mom_pct}%*\n"
            f"• آخر سعر (تقريبي): *{last_price}*\n\n"
            f"_تنبيه: هذه إشارة زخم بسيطة وليست نصيحة مالية._"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await update.message.reply_text(f"صار خطأ في /best:\n{e}")


# ================== ORDER MONITOR ==================
async def monitor_orders_job(context: ContextTypes.DEFAULT_TYPE):
    """
    يشيّك آخر أوامر منفذة، وإذا فيه أمر جديد منفذ يرسل رسالة.
    """
    global LAST_NOTIFIED_ORDER_ID

    try:
        orders = alpaca_trade.get_orders(limit=5)
        if not orders:
            return

        # ندور أحدث order "filled"
        latest_filled = None
        for o in orders:
            if getattr(o, "status", "") == "filled":
                latest_filled = o
                break

        if not latest_filled:
            return

        oid = getattr(latest_filled, "id", None)
        if not oid:
            return

        if LAST_NOTIFIED_ORDER_ID == oid:
            return  # نفس الأمر ما نكرر

        symbol = latest_filled.symbol
        side = latest_filled.side.value if hasattr(latest_filled.side, "value") else str(latest_filled.side)
        qty = getattr(latest_filled, "filled_qty", None) or getattr(latest_filled, "qty", None) or "?"
        price = getattr(latest_filled, "filled_avg_price", None) or "?"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        msg = format_trade_message(symbol, side, qty, price, ts)
        await send_to_chat(context.application, msg)

        LAST_NOTIFIED_ORDER_ID = oid

    except Exception as e:
        # ما نكثر رسائل أخطاء، بس نرسل واحدة مختصرة
        await send_to_chat(context.application, f"⚠️ خطأ في مراقبة الأوامر: `{e}`")


# ================== MAIN ==================
async def post_init(app: Application):
    # رسالة تشغيل
    await send_to_chat(app, "🤖 البوت اشتغل الآن وجاهز يرسل تنبيهات بعد تنفيذ الصفقات.")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("best", cmd_best))

    # Jobs
    app.job_queue.run_repeating(monitor_orders_job, interval=CHECK_ORDERS_EVERY_SEC, first=5)

    # Start polling
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
