import os
import asyncio
import datetime as dt
from typing import List, Dict, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Alpaca (alpaca-py)
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


# =========================
# ENV
# =========================
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL")  # مثال: https://paper-api.alpaca.markets
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# إعدادات بسيطة للاستراتيجية
WATCHLIST = os.getenv("WATCHLIST", "TSLA,NVDA,AAPL,AMZN,GOOGL,AMD,MU").split(",")
MOMENTUM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.002"))  # 0.2%
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "1000"))            # حجم الصفقة بالدولار
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "20"))                  # كل كم ثانية يفحص
ALLOW_SHORT = os.getenv("ALLOW_SHORT", "1") == "1"                   # 1 يسمح شورت
PAPER = ("paper" in (APCA_API_BASE_URL or "").lower())               # يستنتج ورقي من الرابط

# منع تكرار الصفقات بسرعة
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))

def require(name, value):
    if not value:
        raise RuntimeError(f"Missing {name}")

require("APCA_API_BASE_URL", APCA_API_BASE_URL)
require("APCA_API_KEY_ID", APCA_API_KEY_ID)
require("APCA_API_SECRET_KEY", APCA_API_SECRET_KEY)
require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
require("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

# Telegram chat id أحياناً لازم يكون int
try:
    TELEGRAM_CHAT_ID_INT = int(TELEGRAM_CHAT_ID)
except Exception:
    TELEGRAM_CHAT_ID_INT = TELEGRAM_CHAT_ID  # لو كانت @channel مثلاً

# =========================
# CLIENTS
# =========================
trading = TradingClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY, paper=PAPER)
data = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)

# =========================
# STATE
# =========================
_last_trade_at: Dict[str, dt.datetime] = {}
_last_signal: Dict[str, str] = {}  # "buy" / "short" / "none"


# =========================
# HELPERS
# =========================
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def in_cooldown(symbol: str) -> bool:
    t = _last_trade_at.get(symbol)
    if not t:
        return False
    return (now_utc() - t).total_seconds() < COOLDOWN_SECONDS

def usd_to_qty(price: float, usd: float) -> int:
    if price <= 0:
        return 0
    return max(1, int(usd / price))

async def tg_send(app: Application, text: str):
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID_INT, text=text)

def get_position_qty(symbol: str) -> int:
    # يرجع كمية المركز الحالية (موجب لونق / سالب شورت / 0 مافي)
    try:
        pos = trading.get_open_position(symbol)
        qty = int(float(pos.qty))
        return qty
    except Exception:
        return 0

def market_order(symbol: str, side: OrderSide, qty: int):
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    return trading.submit_order(order)

def fetch_last_5min_avg_and_last(symbol: str) -> Optional[tuple]:
    # نجيب آخر 6 شموع 1 دقيقة (تقريباً آخر 5 دقائق)
    end = now_utc()
    start = end - dt.timedelta(minutes=7)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        limit=10,
    )

    bars = data.get_stock_bars(req).data.get(symbol, [])
    if len(bars) < 3:
        return None

    # خذ آخر 5 شموع
    last_n = bars[-5:] if len(bars) >= 5 else bars
    closes = [b.close for b in last_n]
    avg = sum(closes) / len(closes)
    last = closes[-1]
    return avg, last


# =========================
# TELEGRAM COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ شات جبتي للأسهم شغّال\n\n"
        "الأوامر:\n"
        "/status - حالة البوت\n"
        "/best - أفضل إشارة الآن\n"
        "/watch - قائمة المراقبة"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 الحالة:\n"
        f"- WATCHLIST: {', '.join(WATCHLIST)}\n"
        f"- THRESHOLD: {MOMENTUM_THRESHOLD*100:.2f}%\n"
        f"- USD_PER_TRADE: {USD_PER_TRADE}\n"
        f"- LOOP_SECONDS: {LOOP_SECONDS}\n"
        f"- ALLOW_SHORT: {'نعم' if ALLOW_SHORT else 'لا'}\n"
    )

async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👀 قائمة المراقبة:\n" + "\n".join([f"- {s.strip()}" for s in WATCHLIST]))

async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # أفضل سهم = أكبر فرق بين السعر و متوسط 5 دقائق
    best_sym = None
    best_score = 0.0
    best_last = None
    best_avg = None

    for sym in WATCHLIST:
        sym = sym.strip().upper()
        res = fetch_last_5min_avg_and_last(sym)
        if not res:
            continue
        avg, last = res
        score = abs(last - avg) / avg if avg else 0.0
        if score > best_score:
            best_score = score
            best_sym = sym
            best_last = last
            best_avg = avg

    if not best_sym:
        await update.message.reply_text("ما قدرت أجيب بيانات كافية الآن. جرّب بعد دقيقة.")
        return

    direction = "شراء" if best_last > best_avg else "شورت"
    await update.message.reply_text(
        "🏆 أفضل حركة الآن:\n\n"
        f"السهم: {best_sym}\n"
        f"الاتجاه المتوقع: {direction}\n"
        f"السعر: {best_last:.2f}\n"
        f"متوسط 5 دقائق: {best_avg:.2f}\n"
        f"الفرق: {best_score*100:.2f}%"
    )


# =========================
# STRATEGY LOOP (runs inside PTB job queue)
# =========================
async def strategy_tick(context: ContextTypes.DEFAULT_TYPE):
    app = context.application

    for sym in WATCHLIST:
        symbol = sym.strip().upper()
        if not symbol:
            continue

        res = fetch_last_5min_avg_and_last(symbol)
        if not res:
            continue

        avg, last = res
        if avg <= 0:
            continue

        # إشارة
        up = last > avg * (1 + MOMENTUM_THRESHOLD)
        down = last < avg * (1 - MOMENTUM_THRESHOLD)

        # لا تكرر نفس الإشارة كل مرة
        prev_sig = _last_signal.get(symbol, "none")

        # تحقق من الكولداون
        if in_cooldown(symbol):
            continue

        pos_qty = get_position_qty(symbol)

        # BUY signal
        if up and prev_sig != "buy":
            qty = usd_to_qty(last, USD_PER_TRADE)

            # إذا عندك شورت مفتوح، اقفله أولاً
            if pos_qty < 0:
                close_qty = abs(pos_qty)
                market_order(symbol, OrderSide.BUY, close_qty)
                await tg_send(app, f"✅ تم تغطية الشورت على {symbol} | كمية: {close_qty} | السعر: {last:.2f}")

            # افتح لونق
            market_order(symbol, OrderSide.BUY, qty)
            _last_trade_at[symbol] = now_utc()
            _last_signal[symbol] = "buy"

            await tg_send(
                app,
                "📈 تنفيذ صفقة (شراء)\n\n"
                f"السهم: {symbol}\n"
                f"الدخول: {last:.2f}\n"
                f"الكمية: {qty}\n"
                f"السبب: زخم صاعد + اختراق متوسط 5 دقائق\n"
                f"متوسط 5 دقائق: {avg:.2f}"
            )

        # SHORT signal
        elif down and ALLOW_SHORT and prev_sig != "short":
            qty = usd_to_qty(last, USD_PER_TRADE)

            # إذا عندك لونق مفتوح، اقفله أولاً
            if pos_qty > 0:
                close_qty = pos_qty
                market_order(symbol, OrderSide.SELL, close_qty)
                await tg_send(app, f"✅ تم إغلاق الشراء على {symbol} | كمية: {close_qty} | السعر: {last:.2f}")

            # افتح شورت (SELL)
            market_order(symbol, OrderSide.SELL, qty)
            _last_trade_at[symbol] = now_utc()
            _last_signal[symbol] = "short"

            await tg_send(
                app,
                "📉 تنفيذ صفقة (شورت)\n\n"
                f"السهم: {symbol}\n"
                f"الدخول: {last:.2f}\n"
                f"الكمية: {qty}\n"
                f"السبب: زخم هابط + كسر متوسط 5 دقائق\n"
                f"متوسط 5 دقائق: {avg:.2f}"
            )

        else:
            # ما فيه إشارة جديدة
            pass


# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("watch", cmd_watch))

    # شغّل الاستراتيجية كل LOOP_SECONDS
    app.job_queue.run_repeating(strategy_tick, interval=LOOP_SECONDS, first=5)

    print("🚀 Bot is running (Telegram + Alpaca strategy)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
