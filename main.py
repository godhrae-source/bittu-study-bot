import os
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- 1. FREE RENDER WORKAROUND (Web Server) ---
# This keeps Render Free Tier active by opening a web port
web_app = Flask('')

@web_app.route('/')
def home():
    return "Bittu Study Bot is live!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# Run server in a background thread
Thread(target=run_web_server, daemon=True).start()

# --- 2. YOUR ORIGINAL BOT CODE ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Bittu Study Bot is running.\n\nSend me a screenshot."
    )


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.photo[-1].file_id

    await context.bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=file_id,
        caption="📚 Study Screenshot",
    )

    await update.message.reply_text("✅ Saved successfully.")


def main():
    # Fix event loop handling for newer Python runtime
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo))

    print("Bot started...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
w
