import os
import time
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # مثال: 1682557412

ALPACA_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET")

# إعدادات تشغيل
SYMBOLS = os.getenv("SYMBOLS", "TSLA,NVDA,AAPL,CRWD,AMZN,AMD,GOOGL,MU").split(",")
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "20"))          # كل كم ثانية يسوي دورة
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "5"))           # آخر كم دقيقة لحساب الزخم
AUTO_TRADE = os.getenv("AUTO_TRADE", "0") == "1"             # 0 = إشارات فقط / 1 = ينفذ
TRADE_QTY = float(os.getenv("TRADE_QTY", "1"))               # كمية الصفقة (أسهم)
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.001"))           # حد أدنى للزخم عشان يطلع إشارة

# Paper / Live
PAPER = os.getenv("ALPACA_PAPER", "1") == "1"


def _require_env(name: str, val: Optional[str]):
    if not val:
        raise RuntimeError(f"Missing {name}")


_require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN)
_require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
_require_env("ALPACA_API_KEY", ALPACA_KEY)
_require_env("ALPACA_API_SECRET", ALPACA_SECRET)

TELEGRAM_CHAT_ID_INT = int(TELEGRAM_CHAT_ID)


# =========================
# CLIENTS
# =========================
# alpaca-py uses different endpoints internally; for trading client we pass paper flag.
trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=PAPER)
data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)


# =========================
# MODEL / SIGNAL
# =========================
@dataclass
class Signal:
    symbol: str
    side: str  # "buy" or "short"
    score: float
    last_price: float
    reason: str


def calc_momentum_score(prices: List[float]) -> float:
    """
    زخم بسيط: نسبة التغير من أول السعر لآخر السعر.
    """
    if not prices or len(prices) < 2:
        return 0.0
    first = prices[0]
    last = prices[-1]
    if first <= 0:
        return 0.0
    return (last - first) / first


def pick_side(score: float) -> str:
    return "buy" if score > 0 else "short"


def format_signal_ar(sig: Signal) -> str:
    side_ar = "شراء" if sig.side == "buy" else "شورت"
    return (
        "📢 إشارة جديدة\n"
        f"السهم: {sig.symbol}\n"
        f"النوع: {side_ar}\n"
        f"السعر الحالي: {sig.last_price:.2f}\n"
        f"الزخم: {sig.score*100:.3f}%\n"
        f"السبب: {sig.reason}\n"
        f"{'✅ تنفيذ آلي مفعّل' if AUTO_TRADE else 'ℹ️ إشارات فقط (بدون تنفيذ)'}"
    )


# =========================
# ALPACA HELPERS (sync -> async)
# =========================
def fetch_last_minutes_prices(symbol: str, lookback_min: int) -> List[float]:
    """
    يجلب Bars دقيقة (1Min) لآخر lookback_min دقائق.
    """
    # نرجع 10 دقائق احتياط (لتفادي فجوات)
    end = int(time.time())
    start = end - (lookback_min + 5) * 60

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        limit=100,
    )
    bars = data_client.get_stock_bars(req)
    df = bars.df

    if df is None or df.empty:
        return []

    # df multi-index: (symbol, timestamp)
    try:
        sym_df = df.xs(symbol)
    except Exception:
        return []

    closes = sym_df["close"].tail(lookback_min).tolist()
    return [float(x) for x in closes if x is not None]


def place_market_order(symbol: str, side: str, qty: float) -> str:
    """
    ينفذ Market order (شراء أو شورت).
    """
    alp_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=alp_side,
        time_in_force=TimeInForce.DAY,
    )
    submitted = trading_client.submit_order(order_data=order)
    return str(submitted.id)


async def async_fetch_prices(symbol: str, lookback_min: int) -> List[float]:
    return await asyncio.to_thread(fetch_last_minutes_prices, symbol, lookback_min)


async def async_place_order(symbol: str, side: str, qty: float) -> str:
    return await asyncio.to_thread(place_market_order, symbol, side, qty)


# =========================
# TELEGRAM COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت شغّال\n\n"
        "الأوامر:\n"
        "/status - حالة البوت\n"
        "/best - أفضل سهم الآن\n"
        "/autoon - تشغيل التنفيذ الآلي (حذر)\n"
        "/autooff - إيقاف التنفيذ الآلي\n"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    auto = "مفعّل ✅" if context.application.bot_data.get("AUTO_TRADE", AUTO_TRADE) else "مقفول ⛔️"
    last = context.application.bot_data.get("LAST_SIGNAL")
    last_txt = f"{last.symbol} ({'شراء' if last.side=='buy' else 'شورت'})" if last else "لا يوجد"
    await update.message.reply_text(
        "📊 حالة البوت\n"
        f"- متابعة: {', '.join(SYMBOLS)}\n"
        f"- الدورة كل: {INTERVAL_SEC} ثانية\n"
        f"- نافذة التحليل: {LOOKBACK_MIN} دقائق\n"
        f"- التنفيذ الآلي: {auto}\n"
        f"- آخر إشارة: {last_txt}"
    )


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sig = await compute_best_signal()
    if not sig:
        await update.message.reply_text("ما قدرت أطلع أفضل سهم الآن (بيانات غير كافية).")
        return
    await update.message.reply_text(format_signal_ar(sig))


async def cmd_autoon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["AUTO_TRADE"] = True
    await update.message.reply_text("✅ تم تفعيل التنفيذ الآلي (Auto Trade).")


async def cmd_autooff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["AUTO_TRADE"] = False
    await update.message.reply_text("⛔️ تم إيقاف التنفيذ الآلي. (إشارات فقط)")


# =========================
# CORE LOGIC
# =========================
async def compute_best_signal() -> Optional[Signal]:
    best: Optional[Signal] = None

    for sym in SYMBOLS:
        prices = await async_fetch_prices(sym, LOOKBACK_MIN)
        if len(prices) < 2:
            continue

        score = calc_momentum_score(prices)
        last_price = prices[-1]

        # تجاهل الزخم الضعيف جدًا
        if abs(score) < MIN_SCORE:
            continue

        side = pick_side(score)
        reason = f"زخم آخر {LOOKBACK_MIN} دقائق"

        sig = Signal(symbol=sym, side=side, score=score, last_price=last_price, reason=reason)
        if best is None or abs(sig.score) > abs(best.score):
            best = sig

    return best


async def send_to_telegram(app: Application, text: str):
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID_INT, text=text)


async def monitor_loop(app: Application):
    """
    حلقة مراقبة مستمرة:
    - تحسب أفضل سهم
    - ترسل إشعار عند تغير الإشارة/تجاوز حد الزخم
    - (اختياري) تنفذ صفقة ثم ترسل إشعار تنفيذ
    """
    last_key: Optional[Tuple[str, str]] = None

    while True:
        try:
            sig = await compute_best_signal()
            if sig:
                app.bot_data["LAST_SIGNAL"] = sig

                key = (sig.symbol, sig.side)
                if key != last_key:
                    last_key = key
                    await send_to_telegram(app, format_signal_ar(sig))

                    auto = app.bot_data.get("AUTO_TRADE", AUTO_TRADE)
                    if auto:
                        # تنفيذ الصفقة
                        order_id = await async_place_order(sig.symbol, sig.side, TRADE_QTY)
                        side_ar = "شراء" if sig.side == "buy" else "شورت"
                        await send_to_telegram(
                            app,
                            "✅ تم تنفيذ صفقة\n"
                            f"السهم: {sig.symbol}\n"
                            f"النوع: {side_ar}\n"
                            f"الكمية: {TRADE_QTY}\n"
                            f"Order ID: {order_id}"
                        )

        except Exception as e:
            # لا نوقف البوت بسبب خطأ
            await send_to_telegram(app, f"⚠️ خطأ في المراقبة: {type(e).__name__}: {e}")

        await asyncio.sleep(INTERVAL_SEC)


async def on_startup(app: Application):
    # حفظ حالة التنفيذ الافتراضية
    app.bot_data["AUTO_TRADE"] = AUTO_TRADE
    # تشغيل حلقة المراقبة في الخلفية
    app.create_task(monitor_loop(app))


# =========================
# RUN
# =========================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("autoon", cmd_autoon))
    app.add_handler(CommandHandler("autooff", cmd_autooff))

    print("🚀 Bot running (polling)...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
