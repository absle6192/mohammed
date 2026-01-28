import os
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pytz
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -----------------------
# Logging
# -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

# -----------------------
# Env
# -----------------------
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "").strip()
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "").strip()
APCA_BASE_URL = os.getenv("APCA_BASE_URL", "https://paper-api.alpaca.markets").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # keep as string for telegram

WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST", "TSLA,NVDA,AAPL,AMZN,AMD,GOOGL,MU,CRWD"
).split(",") if s.strip()]

POLL_SEC = int(os.getenv("POLL_SEC", "15"))
MOMENTUM_MIN = int(os.getenv("MOMENTUM_MIN", "5"))
MOMENTUM_THRESHOLD_PCT = float(os.getenv("MOMENTUM_THRESHOLD_PCT", "0.20"))  # %
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "120"))

AUTO_TRADE = os.getenv("AUTO_TRADE", "0").strip() == "1"
USD_PER_TRADE = float(os.getenv("USD_PER_TRADE", "200"))

SA_TZ = pytz.timezone("Asia/Riyadh")

# -----------------------
# Clients
# -----------------------
if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    log.warning("⚠️ Missing Alpaca keys. Set APCA_API_KEY_ID & APCA_API_SECRET_KEY in Render env.")

data_client = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)
trade_client = TradingClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY, paper=True, url_override=APCA_BASE_URL)

# -----------------------
# State
# -----------------------
@dataclass
class Signal:
    symbol: str
    side: str            # "BUY" or "SHORT"
    price: float
    mom_pct: float
    sma: float
    reason: str
    ts: float

latest_best: Optional[Signal] = None
latest_snapshot: Dict[str, Dict] = {}
last_alert_at: Dict[str, float] = {}
last_trade_at: Dict[str, float] = {}

# -----------------------
# Helpers
# -----------------------
def now_sa_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def format_signal_ar(sig: Signal) -> str:
    side_ar = "شراء" if sig.side == "BUY" else "شورت"
    arrow = "📈" if sig.side == "BUY" else "📉"
    return (
        f"{arrow} <b>إشارة {side_ar}</b>\n"
        f"🏷️ السهم: <b>{sig.symbol}</b>\n"
        f"💵 السعر: <b>{sig.price:.2f}</b>\n"
        f"⚡ الزخم ({MOMENTUM_MIN}د): <b>{sig.mom_pct:.2f}%</b>\n"
        f"📊 SMA({MOMENTUM_MIN}د): <b>{sig.sma:.2f}</b>\n"
        f"🧠 السبب: {sig.reason}\n"
        f"🕒 {now_sa_str()} (السعودية)"
    )

async def tg_send_text(app: Application, text: str) -> bool:
    """Send Telegram message; never crash the bot."""
    if not TELEGRAM_CHAT_ID:
        log.warning("⚠️ Missing TELEGRAM_CHAT_ID")
        return False
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        log.warning(f"⚠️ Telegram send failed: {e}")
        return False

def get_market_status() -> Tuple[str, str]:
    """Return (status_ar, details)"""
    try:
        clock = trade_client.get_clock()
        if clock.is_open:
            return "🟢 السوق مفتوح", f"يغلق بعد: {clock.next_close}"
        else:
            return "🔴 السوق مغلق", f"يفتح عند: {clock.next_open}"
    except Exception as e:
        return "⚠️ تعذر جلب حالة السوق", str(e)

def simple_sma(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)

def compute_signal_for_symbol(symbol: str) -> Optional[Signal]:
    """
    Pull recent 1-min bars, compute momentum over MOMENTUM_MIN and SMA over MOMENTUM_MIN.
    Generate BUY if momentum > threshold AND price > SMA.
    Generate SHORT if momentum < -threshold AND price < SMA.
    """
    try:
        # pull last ~20 minutes for safety
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Minute,
            limit=max(20, MOMENTUM_MIN + 5),
        )
        bars = data_client.get_stock_bars(req).data.get(symbol, [])
        if not bars or len(bars) < MOMENTUM_MIN + 2:
            return None

        closes = [safe_float(b.close) for b in bars]
        last_price = closes[-1]
        past_price = closes[-(MOMENTUM_MIN + 1)]

        if past_price <= 0:
            return None

        mom_pct = ((last_price - past_price) / past_price) * 100.0
        sma = simple_sma(closes[-MOMENTUM_MIN:])

        # keep snapshot for /best /status
        latest_snapshot[symbol] = {
            "price": last_price,
            "mom_pct": mom_pct,
            "sma": sma,
            "ts": time.time(),
        }

        if mom_pct >= MOMENTUM_THRESHOLD_PCT and last_price > sma:
            reason = f"زخم صاعد + السعر فوق متوسط {MOMENTUM_MIN} دقائق"
            return Signal(symbol=symbol, side="BUY", price=last_price, mom_pct=mom_pct, sma=sma, reason=reason, ts=time.time())

        if mom_pct <= -MOMENTUM_THRESHOLD_PCT and last_price < sma:
            reason = f"زخم هابط + السعر تحت متوسط {MOMENTUM_MIN} دقائق"
            return Signal(symbol=symbol, side="SHORT", price=last_price, mom_pct=mom_pct, sma=sma, reason=reason, ts=time.time())

        return None
    except Exception as e:
        log.warning(f"{symbol} analysis error: {e}")
        return None

def should_alert(symbol: str) -> bool:
    last = last_alert_at.get(symbol, 0.0)
    return (time.time() - last) >= COOLDOWN_SEC

def mark_alert(symbol: str):
    last_alert_at[symbol] = time.time()

def can_trade(symbol: str) -> bool:
    last = last_trade_at.get(symbol, 0.0)
    return (time.time() - last) >= max(COOLDOWN_SEC, 60)

def mark_trade(symbol: str):
    last_trade_at[symbol] = time.time()

def place_trade(signal: Signal) -> Tuple[bool, str]:
    """
    Optional: market order by USD notional.
    BUY => buy notional, SHORT => sell notional.
    """
    try:
        side = OrderSide.BUY if signal.side == "BUY" else OrderSide.SELL

        order_req = MarketOrderRequest(
            symbol=signal.symbol,
            notional=USD_PER_TRADE,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = trade_client.submit_order(order_req)
        return True, f"✅ تم إرسال أمر {signal.symbol} ({signal.side}) بقيمة ${USD_PER_TRADE:.0f} | id={order.id}"
    except APIError as e:
        return False, f"❌ فشل تنفيذ الصفقة: {e}"
    except Exception as e:
        return False, f"❌ خطأ غير متوقع بالصفقة: {e}"

# -----------------------
# Telegram commands
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>هلا! أنا بوت إشارات الأسهم</b>\n"
        f"📌 أراقب: {', '.join(WATCHLIST)}\n"
        f"⏱️ كل {POLL_SEC} ثانية\n"
        f"⚡ زخم {MOMENTUM_MIN} دقائق | حد الإشارة: {MOMENTUM_THRESHOLD_PCT:.2f}%\n\n"
        "الأوامر:\n"
        "/status - حالة السوق\n"
        "/best - أفضل سهم الآن"
    )
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_ar, details = get_market_status()
    await update.message.reply_text(f"{status_ar}\nℹ️ {details}", parse_mode="HTML")

async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global latest_best
    if not latest_best:
        await update.message.reply_text("ما عندي أفضل سهم الآن (مافي إشارة قوية حاليًا).", parse_mode="HTML")
        return
    await update.message.reply_text(format_signal_ar(latest_best), parse_mode="HTML")

# -----------------------
# Main loop
# -----------------------
async def analysis_loop(app: Application):
    global latest_best

    # Startup ping
    await tg_send_text(app, "✅ <b>البوت اشتغل بنجاح</b>\n🕒 " + now_sa_str())

    while True:
        try:
            signals: List[Signal] = []
            for sym in WATCHLIST:
                sig = compute_signal_for_symbol(sym)
                if sig:
                    signals.append(sig)

            # choose best by absolute momentum
            best = None
            if signals:
                best = sorted(signals, key=lambda s: abs(s.mom_pct), reverse=True)[0]

            # send signal if new / cooldown
            if best:
                # if different symbol OR momentum changed a lot, allow alert
                changed = (latest_best is None) or (best.symbol != latest_best.symbol) or (abs(best.mom_pct - latest_best.mom_pct) >= 0.15)

                if changed and should_alert(best.symbol):
                    latest_best = best
                    await tg_send_text(app, format_signal_ar(best))
                    mark_alert(best.symbol)

                    # optional auto trade
                    if AUTO_TRADE and can_trade(best.symbol):
                        ok, trade_msg = place_trade(best)
                        await tg_send_text(app, trade_msg)
                        if ok:
                            mark_trade(best.symbol)

            await asyncio.sleep(POLL_SEC)

        except Exception as e:
            log.exception(f"Loop error: {e}")
            # don't crash, just wait
            await asyncio.sleep(5)

async def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (set it in Render env).")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("best", cmd_best))

    # start telegram polling + analysis loop
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # run analysis loop in background
    asyncio.create_task(analysis_loop(app))

    log.info("✅ Telegram polling started. Bot is running.")
    await app.updater.idle()

    await app.updater.stop()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
