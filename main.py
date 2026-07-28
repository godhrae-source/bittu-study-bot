import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Bittu Study Bot is running.\n\nSend me a screenshot."
    )


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1].file_id

    await context.bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=photo,
        caption="📚 Study Screenshot"
    )

    await update.message.reply_text("✅ Saved to your study channel.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo))

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
