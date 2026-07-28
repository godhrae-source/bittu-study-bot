import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors

from flask import Flask
from telegram import Update
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

# Database File
DB_FILE = "study_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
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

def safe_str(text: str) -> str:
    if not text:
        return ""
    return "".join(c if ord(c) < 128 else "" for c in text).strip() or "Study Note"

# ----------------- Flask Web Server for Render -----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bittu Study Bot is Live!"

async def run_flask():
    port = int(os.environ.get("PORT", 10000))
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", port, app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

# ----------------- Telegram Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📚 *Bittu Study Notes Bot*\n\n"
        "📸 **How to save notes:**\n"
        "Send 1 or multiple screenshots, and reply with `#topic` when asked.\n\n"
        "📄 **Get Compiled HD PDFs:**\n"
        "• `/daily_pdf` - Today's notes\n"
        "• `/weekly_pdf` - Last 7 days\n"
        "• `/monthly_pdf` - Last 30 days\n"
        "• `/topic_pdf #topic` - Filter by specific topic"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    
    # Maintain a list of photos sent in a batch/album
    if 'pending_photos' not in context.user_data:
        context.user_data['pending_photos'] = []
        
    context.user_data['pending_photos'].append(photo_file_id)

    # Only send prompt message once for the entire batch
    if not context.user_data.get('asked_topic'):
        context.user_data['asked_topic'] = True
        await update.message.reply_text(
            "📌 **Screenshot(s) received!**\nPlease reply with the **#topic** (e.g., `#21`, `#chemistry`):",
            parse_mode="Markdown"
        )
    return WAITING_FOR_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"):
        topic = f"#{topic}"
        
    file_ids = context.user_data.get('pending_photos', [])
    if not file_ids:
        await update.message.reply_text("❌ Session expired. Please resend the photo(s).")
        return ConversationHandler.END

    status_msg = await update.message.reply_text(f"💾 Saving {len(file_ids)} screenshot(s)...")

    # Save all accumulated images to Database
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for f_id in file_ids:
        cursor.execute("INSERT INTO screenshots (file_id, topic) VALUES (?, ?)", (f_id, topic))
        if CHANNEL_ID:
            try:
                caption = f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=f_id, caption=caption, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to post to channel: {e}")
    conn.commit()
    conn.close()

    await status_msg.edit_text(f"✅ **Saved {len(file_ids)} screenshot(s) under {topic}!**", parse_mode="Markdown")
    
    # Reset context state
    context.user_data.pop('pending_photos', None)
    context.user_data.pop('asked_topic', None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_photos', None)
    context.user_data.pop('asked_topic', None)
    await update.message.reply_text("Action canceled.")
    return ConversationHandler.END
# ----------------- Fast PDF Generator Engine -----------------

async def fetch_image(bot, file_id, idx):
    """Helper to download and optimize images concurrently."""
    try:
        tg_file = await bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()
        
        # Load and convert image safely
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        
        # Fast resize for PDF canvas (Max width 1200px keeps print sharp & fast)
        img.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
        
        temp_path = f"temp_{idx}.jpg"
        img.save(temp_path, "JPEG", quality=85, optimize=True)
        return idx, temp_path
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None

async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, query_sql: str, params: tuple, title: str):
    await update.message.reply_text("⏳ Compiling PDF fast... Please wait.")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await update.message.reply_text("⚠️ No screenshots found for this timeframe or topic.")
        return

    # 1. Download all images concurrently in parallel (Super Fast!)
    download_tasks = [
        fetch_image(context.bot, record[1], idx) 
        for idx, record in enumerate(records, start=1)
    ]
    downloaded_images = await asyncio.gather(*download_tasks)
    
    # Sort images back in correct order
    downloaded_images.sort(key=lambda x: x[0])

    # 2. Build PDF Document
    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_w, page_h = A4
    total_count = len(records)

    for idx, temp_path in downloaded_images:
        if not temp_path or not os.path.exists(temp_path):
            continue

        record = records[idx - 1]
        topic_tag, ts_str = record[2], record[3]

        # Header Banner
        c.setFillColor(colors.HexColor("#2C3E50"))
        c.rect(0, page_h - 45, page_w, 45, fill=True, stroke=False)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20, page_h - 28, f"Topic: {safe_str(topic_tag)}")
        c.drawRightString(page_w - 20, page_h - 28, f"Date: {ts_str[:10]}")

        # Image Dimensions calculation
        img = Image.open(temp_path)
        orig_w, orig_h = img.size
        aspect = orig_w / float(orig_h)

        max_w = page_w - 40
        max_h = page_h - 70

        if max_w / max_h < aspect:
            draw_w = max_w
            draw_h = max_w / aspect
        else:
            draw_h = max_h
            draw_w = max_h * aspect

        x = (page_w - draw_w) / 2
        y_img = (page_h - 45 - draw_h) / 2

        c.drawImage(temp_path, x, y_img, width=draw_w, height=draw_h)

        # Footer
        c.setFillColor(colors.HexColor("#7F8C8D"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 20, 15, f"Page {idx} of {total_count}")

        c.showPage()
        
        # Cleanup temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)

    c.save()
    pdf_buffer.seek(0)

    clean_filename = f"Study_Notes_{datetime.now().strftime('%Y%m%d')}.pdf"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=pdf_buffer,
        filename=clean_filename,
        caption=f"📄 **{title}** Ready!\nTotal {total_count} page(s) compiled."
    )
# PDF Command Handlers
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT id, file_id, topic, timestamp FROM screenshots WHERE timestamp LIKE ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (today,), "Daily Study Notes")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT id, file_id, topic, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (week_ago,), "Weekly Study Notes")

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT id, file_id, topic, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Study Notes")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Specify topic! Example: `/topic_pdf 21`", parse_mode="Markdown")
        return
    
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT id, file_id, topic, timestamp FROM screenshots WHERE LOWER(topic) LIKE ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (f"%{user_topic.lower()}%",), f"Topic PDF - #{user_topic}")

# ----------------- Main Execution Block -----------------

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receive_photo)],
        states={
            WAITING_FOR_TOPIC: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)
            ],
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
    logger.info("Telegram Bot Polling Started!")

    await run_flask()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
