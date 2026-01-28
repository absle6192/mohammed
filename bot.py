import os
import time
import math
import asyncio
from datetime import datetime, timedelta, timezone

import pytz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


# =========================
# ENV
# =========================
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "").strip()
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "").strip()
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# إعدادات البوت
WATCHLIST = os.getenv("WATCHLIST", "TSLA,NVDA,AAPL,AMZN,AMD,MU,GOOGL").strip()
SYMBOLS = [s.strip().upper() for s in WATCHLIST.split(",") if s.strip()]

# تحليل 5 دقائق (زخم)
MOMENTUM_MIN_PCT = float(os.getenv("MOMENTUM_MIN_PCT", "0.15"))  # %0.15 افتراضي
TRADE_NOTIONAL_USD = float(os.getenv("TRADE_NOTIONAL_USD", "1000"))  # قيمة الصفقة بالدولار
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "30"))  # كل كم ثانية يعيد التحليل

TZ_RIYADH = pytz.timezone("Asia/Riyadh")


def require(name: str, value: str):
    if not value:
        raise RuntimeError(f"Missing {name}")


require("APCA_API_BASE_URL", APCA_API_BASE_URL)
require("APCA_API_KEY_ID", APCA_API_KEY_ID)
require("APCA_API_SECRET_KEY", APCA_API_SECRET_KEY)
require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
require("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

IS_PAPER = "paper" in APCA_API_BASE_URL.lower()

# عملاء Alpaca
data_client = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)
trading_client = TradingClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY, paper=IS_PAPER)


# =========================
# HELPERS
# =========================
def now_riyadh_str() -> str:
    return datetime.now(TZ_RIYADH).strftime("%Y-%m-%d %I:%M:%S %p")

def fmt_pct(x: float) -> str:
    return f"{x:.2f}%"

def is_market_open_simple() -> bool:
    """
    فحص بسيط: يعتمد على ساعات السوق الأمريكية تقريبًا.
    (ممكن توسّعه لاحقًا بـ Calendar API من Alpaca)
    """
    # نيويورك
    ny = pytz.timezone("America/New_York")
    now_ny = datetime.now(ny)
    # السبت/الأحد إغلاق
    if now_ny.weekday() >= 5:
        return False
    # 9:30 إلى 16:00
    t = now_ny.time()
    return (t.hour > 9 or (t.hour == 9 and t.minute >= 30)) and (t.hour < 16)

async def tg_send(app: Application, text: str):
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.MARKDOWN
    )

async def send_trade_alert(app: Application, symbol: str, side: str, price: float, reason: str, mom_pct: float):
    side_ar = "شراء" if side == "buy" else "شورت"
    msg = (
        f"📢 *تنفيذ صفقة*\n\n"
        f"• الوقت: {now_riyadh_str()}\n"
        f"• السهم: *{symbol}*\n"
        f"• النوع: *{side_ar}*\n"
        f"• السعر التقريبي: *{price:.2f}*\n"
        f"• الزخم (5د): *{fmt_pct(mom_pct)}*\n"
        f"• السبب: {reason}\n"
    )
    await tg_send(app, msg)

async def send_info(app: Application, text: str):
    msg = f"ℹ️ {text}\n\n🕒 {now_riyadh_str()}"
    await tg_send(app, msg)

async def send_error(app: Application, text: str):
    msg = f"⚠️ *خطأ*\n{text}\n\n🕒 {now_riyadh_str()}"
    await tg_send(app, msg)


def get_positions_symbols() -> set:
    try:
        positions = trading_client.get_all_positions()
        return {p.symbol.upper() for p in positions}
    except Exception:
        return set()


def latest_5m_momentum(symbol: str) -> tuple[float, float, str]:
    """
    يرجع:
    - momentum_pct: نسبة الزخم خلال 5 دقائق
    - last_price: آخر إغلاق
    - note: سبب/ملاحظة
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=6)  # هامش

    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        limit=10
    )
    bars = data_client.get_stock_bars(req)

    df = bars.df
    if df is None or df.empty:
        return 0.0, 0.0, "لا توجد بيانات"

    # df multi-index (symbol, timestamp)
    try:
        sdf = df.xs(symbol)
    except Exception:
        # أحيانًا يكون df بدون multiindex حسب النسخة/الرد
        sdf = df

    sdf = sdf.sort_index()
    if len(sdf) < 5:
        last_close = float(sdf["close"].iloc[-1])
        first_open = float(sdf["open"].iloc[0])
        mom = ((last_close - first_open) / first_open) * 100.0 if first_open else 0.0
        return mom, last_close, "بيانات أقل من 5 دقائق"

    last_close = float(sdf["close"].iloc[-1])
    first_open = float(sdf["open"].iloc[-5])  # قبل 5 دقائق تقريبًا
    mom = ((last_close - first_open) / first_open) * 100.0 if first_open else 0.0
    return mom, last_close, "زخم 5 دقائق"


def place_market_order(symbol: str, side: str, notional: float):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    req = MarketOrderRequest(
        symbol=symbol,
        notional=notional,
        side=order_side,
        time_in_force=TimeInForce.DAY
    )
    return trading_client.submit_order(req)


# =========================
# TELEGRAM COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت شغّال\n\n"
        "الأوامر:\n"
        "/status - حالة السوق\n"
        "/best - أفضل سهم الآن (حسب زخم 5 دقائق)\n"
        "/positions - مراكزي المفتوحة\n"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_ = is_market_open_simple()
    mode = "تجريبي (Paper)" if IS_PAPER else "لايف (Live)"
    await update.message.reply_text(
        f"📊 الحالة:\n"
        f"• السوق: {'مفتوح' if open_ else 'مغلق/خارج الدوام'}\n"
        f"• الوضع: {mode}\n"
        f"• قائمة المراقبة: {', '.join(SYMBOLS)}\n"
        f"• زخم أدنى للتداول: {MOMENTUM_MIN_PCT:.2f}%\n"
        f"• قيمة الصفقة: ${TRADE_NOTIONAL_USD:.0f}\n"
        f"• تكرار التحليل: كل {LOOP_SECONDS} ثانية\n"
    )

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        positions = trading_client.get_all_positions()
        if not positions:
            await update.message.reply_text("✅ ما عندك مراكز مفتوحة الآن.")
            return

        lines = []
        for p in positions:
            # p.qty ممكن يكون سترنق
            qty = getattr(p, "qty", "")
            side = "شراء" if float(qty) > 0 else "شورت"
            lines.append(f"• {p.symbol} | {side} | الكمية: {qty} | P/L: {getattr(p, 'unrealized_pl', '')}")

        await update.message.reply_text("📌 المراكز المفتوحة:\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذر جلب المراكز: {e}")

async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    best_sym = None
    best_abs = 0.0
    best_mom = 0.0
    best_price = 0.0
    best_note = ""

    for s in SYMBOLS:
        try:
            mom, last_price, note = latest_5m_momentum(s)
            if abs(mom) > best_abs:
                best_abs = abs(mom)
                best_mom = mom
                best_price = last_price
                best_sym = s
                best_note = note
        except Exception:
            continue

    if not best_sym:
        await update.message.reply_text("ما قدرت أطلع أفضل سهم الآن (بيانات غير كافية).")
        return

    direction = "شراء ✅" if best_mom > 0 else "شورت ✅"
    await update.message.reply_text(
        f"🏆 أفضل سهم الآن:\n"
        f"• السهم: {best_sym}\n"
        f"• الاتجاه: {direction}\n"
        f"• الزخم (5د): {fmt_pct(best_mom)}\n"
        f"• السعر: {best_price:.2f}\n"
        f"• ملاحظة: {best_note}"
    )


# =========================
# TRADING LOOP
# =========================
async def trading_loop(app: Application):
    await send_info(app, "تم تشغيل حلقة التداول والتحليل ✅")

    while True:
        try:
            # (اختياري) لا تتداول خارج الدوام — تقدر تشيل الشرط لو تبي
            if not is_market_open_simple():
                await asyncio.sleep(LOOP_SECONDS)
                continue

            open_positions = get_positions_symbols()

            # اختر الأفضل حسب زخم 5 دقائق
            best_sym = None
            best_abs = 0.0
            best_mom = 0.0
            best_price = 0.0
            best_note = ""

            for s in SYMBOLS:
                try:
                    mom, last_price, note = latest_5m_momentum(s)
                    if abs(mom) > best_abs:
                        best_abs = abs(mom)
                        best_mom = mom
                        best_price = last_price
                        best_sym = s
                        best_note = note
                except Exception:
                    continue

            if not best_sym:
                await asyncio.sleep(LOOP_SECONDS)
                continue

            # شرط الزخم
            if abs(best_mom) < MOMENTUM_MIN_PCT:
                await asyncio.sleep(LOOP_SECONDS)
                continue

            # لا تدخل إذا عندك مركز مفتوح على نفس السهم
            if best_sym in open_positions:
                await asyncio.sleep(LOOP_SECONDS)
                continue

            side = "buy" if best_mom > 0 else "sell"  # sell = شورت
            reason = f"{best_note} + تجاوز حد الزخم {MOMENTUM_MIN_PCT:.2f}%"

            # تنفيذ
            order = place_market_order(best_sym, side, TRADE_NOTIONAL_USD)

            # تنبيه تيليجرام
            await send_trade_alert(
                app=app,
                symbol=best_sym,
                side=("buy" if side == "buy" else "sell"),
                price=best_price,
                reason=reason,
                mom_pct=best_mom
            )

            # تهدئة بسيطة بعد تنفيذ صفقة
            await asyncio.sleep(max(LOOP_SECONDS, 45))

        except Exception as e:
            await send_error(app, f"{type(e).__name__}: {e}")
            await asyncio.sleep(LOOP_SECONDS)


# =========================
# MAIN
# =========================
async def post_init(app: Application):
    # شغّل حلقة التداول داخل نفس Event Loop الخاص بالتيليجرام
    app.create_task(trading_loop(app))

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("positions", cmd_positions))

    print("🚀 Bot is running...")
    # run_polling هنا صحيح مع v20.7 (بدون Updater قديم)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
