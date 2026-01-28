import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

if not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_CHAT_ID")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ البوت شغّال\n\n"
        "/status - حالة السوق\n"
        "/best - أفضل سهم"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 السوق تحت المراقبة")

async def best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 NVDA")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("best", best))

    print("🚀 Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
