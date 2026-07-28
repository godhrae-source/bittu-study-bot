import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
import reportlab
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")

# Conversation States
WAITING_FOR_TOPIC = 1

# Database Initialization
def init_db():
    conn = sqlite3.connect("study_data.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS screenshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------- Flask Web Server for Render -----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bittu Study Bot with PDF Generator is Active!"

async def run_flask():
    port = int(os.environ.get("PORT", 10000))
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", port, app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

# ----------------- Telegram Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📚 *Welcome to Bittu Study Notes Bot!*\n\n"
        "📸 **How to save notes:**\n"
        "Send any screenshot, and I will ask you for the `#topic` before saving.\n\n"
        "📄 **Get Compiled PDFs:**\n"
        "• `/daily_pdf` - PDF of today's notes\n"
        "• `/weekly_pdf` - PDF of the last 7 days\n"
        "• `/monthly_pdf` - PDF of the last 30 days\n"
        "• `/topic_pdf #topic` - PDF filtered by specific topic"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# Step 1: User sends photo -> Bot receives and asks for topic
async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    context.user_data['pending_photo'] = photo_file_id
    
    await update.message.reply_text(
        "📌 **Screenshot received!**\nPlease reply with the **#topic** (e.g., `#chemistry`, `#pedagogy`, `#maths`):",
        parse_mode="Markdown"
    )
    return WAITING_FOR_TOPIC

# Step 2: User provides topic -> Bot saves to database & posts to channel
async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"):
        topic = f"#{topic}"
        
    file_id = context.user_data.get('pending_photo')
    
    if not file_id:
        await update.message.reply_text("❌ Session expired. Please send the screenshot again.")
        return ConversationHandler.END

    # Save to SQLite Database
    conn = sqlite3.connect("study_data.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO screenshots (file_id, topic) VALUES (?, ?)", (file_id, topic))
    conn.commit()
    conn.close()

    # Forward to Channel
    if CHANNEL_ID:
        try:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=file_id,
                caption=f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to post to channel: {e}")

    await update.message.reply_text(f"✅ Saved under topic **{topic}**!", parse_mode="Markdown")
    context.user_data.pop('pending_photo', None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_photo', None)
    await update.message.reply_text("Action canceled.")
    return ConversationHandler.END

# ----------------- PDF Generator Engine -----------------

async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, query_sql: str, params: tuple, title: str):
    await update.message.reply_text("⏳ Generating PDF document... Please wait.")

    conn = sqlite3.connect("study_data.db")
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await update.message.reply_text("⚠️ No screenshots found for this timeframe/topic.")
        return

    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=letter)
    width, height = letter

    for record in records:
        file_id, topic, timestamp = record
        
        # Download image from Telegram
        tg_file = await context.bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()
        
        img = Image.open(BytesIO(img_bytes))
        img_temp_path = f"temp_{file_id}.jpg"
        img.save(img_temp_path)

        # Draw Header info on PDF Page
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, height - 40, f"Topic: {topic}")
        c.setFont("Helvetica", 10)
        c.drawString(40, height - 55, f"Date: {timestamp}")

        # Draw Image onto PDF Page
        c.drawImage(img_temp_path, 40, 40, width=width - 80, height=height - 110, preserveAspectRatio=True)
        c.showPage()

        # Clean up temporary file
        if os.path.exists(img_temp_path):
            os.remove(img_temp_path)

    c.save()
    pdf_buffer.seek(0)
    pdf_buffer.name = f"{title.replace(' ', '_')}.pdf"

    # Send PDF document back to user
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=pdf_buffer,
        caption=f"📄 **{title}** ready!"
    )

# PDF Command Handlers
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE timestamp LIKE ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (today,), "Daily Study PDF")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (week_ago,), "Weekly Study PDF")

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Study PDF")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Please specify a topic! Example: `/topic_pdf #chemistry`", parse_mode="Markdown")
        return
    
    topic = context.args[0].strip()
    if not topic.startswith("#"):
        topic = f"#{topic}"

    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE topic = ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (topic,), f"Topic PDF - {topic}")

# ----------------- Main Function -----------------

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Setup Conversation Handler for Photo -> Topic workflow
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receive_photo)],
        states={
            WAITING_FOR_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("daily_pdf", daily_pdf))
    application.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    application.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    application.add_handler(CommandHandler("topic_pdf", topic_pdf))
    application.add_handler(conv_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    await run_flask()

if __name__ == "__main__":
    asyncio.run(main())
