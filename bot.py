import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# ENV VARIABLES
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def require(name, value):
    if not value:
        raise RuntimeError(f"Missing {name}")

require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
require("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

# =========================
# TELEGRAM COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت شغّال\n\n"
        "الأوامر:\n"
        "/status - حالة السوق\n"
        "/best - أفضل سهم حاليًا"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 السوق تحت المراقبة الآن...")

async def best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📈 أفضل سهم حاليًا:\n"
        "NVDA\n"
        "السبب: زخم قوي + اختراق متوسط 5 دقائق"
    )

# =========================
# SEND ALERT (تستدعيه من التداول لاحقًا)
# =========================
async def send_trade_alert(app: Application, symbol, side, price, reason):
    msg = (
        "📢 تنفيذ صفقة\n\n"
        f"السهم: {symbol}\n"
        f"النوع: {'شراء' if side == 'buy' else 'شورت'}\n"
        f"السعر: {price}\n"
        f"السبب: {reason}"
    )

    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg
    )

# =========================
# MAIN
# =========================
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("best", best))

    print("🚀 Telegram bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
