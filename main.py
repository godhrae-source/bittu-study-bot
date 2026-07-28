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
    return "Bittu Study Bot HD PDF Generator is Active!"

async def run_flask():
    port = int(os.environ.get("PORT", 10000))
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", port, app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

# ----------------- Telegram Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📚 *Bittu Study Notes HD PDF Bot*\n\n"
        "📸 **How to save notes:**\n"
        "Send any screenshot, and I will ask you for the `#topic` before saving.\n\n"
        "📄 **Get HD Compiled PDFs with Summary:**\n"
        "• `/daily_pdf` - HD PDF of today's notes\n"
        "• `/weekly_pdf` - HD PDF of the last 7 days\n"
        "• `/monthly_pdf` - HD PDF of the last 30 days\n"
        "• `/topic_pdf #topic` - HD PDF filtered by specific topic"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# Step 1: User sends photo -> Bot receives and asks for topic
async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    context.user_data['pending_photo'] = photo_file_id
    
    await update.message.reply_text(
        "📌 **Screenshot received!**\nPlease reply with the **#topic** (e.g., `#શિક્ષણ`, `#chemistry`, `#pedagogy`):",
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

# ----------------- Advanced HD PDF Generator Engine -----------------

async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, query_sql: str, params: tuple, title: str):
    await update.message.reply_text("⏳ Generating HD PDF document with summary... Please wait.")

    conn = sqlite3.connect("study_data.db")
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await update.message.reply_text("⚠️ No screenshots found for this timeframe/topic.")
        return

    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_w, page_h = A4

    # ---------------- PAGE 1: Summary & Index Page ----------------
    c.setFillColor(colors.HexColor("#1A2B4C"))  # Dark Blue Banner
    c.rect(0, page_h - 100, page_w, 100, fill=True, stroke=False)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(30, page_h - 45, title)
    c.setFont("Helvetica", 12)
    c.drawString(30, page_h - 70, f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S (%A)')}")

    # Summary Section Content
    c.setFillColor(colors.HexColor("#333333"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, page_h - 130, "📌 Study Material Summary")

    c.setFont("Helvetica", 11)
    c.drawString(40, page_h - 155, f"• Total Screenshots Included: {len(records)} page(s)")

    # Extract unique topics
    topics = list(set([r[1] for r in records]))
    c.drawString(40, page_h - 175, f"• Topics Covered: {', '.join(topics)}")

    # Index Table Header
    c.setFont("Helvetica-Bold", 12)
    c.drawString(30, page_h - 220, "Index Log:")
    
    c.setFont("Helvetica-Bold", 10)
    c.drawString(30, page_h - 240, "#")
    c.drawString(60, page_h - 240, "Date & Day")
    c.drawString(240, page_h - 240, "Topic Tag")
    c.drawString(450, page_h - 240, "Page")
    
    c.setLineWidth(0.5)
    c.setStrokeColor(colors.gray)
    c.line(30, page_h - 245, page_w - 30, page_h - 245)

    y_pos = page_h - 265
    c.setFont("Helvetica", 10)
    
    for idx, record in enumerate(records, start=1):
        _, topic_tag, ts_str = record
        try:
            dt_obj = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            formatted_date = dt_obj.strftime('%Y-%m-%d (%a)')
        except Exception:
            formatted_date = ts_str

        c.drawString(30, y_pos, str(idx))
        c.drawString(60, y_pos, formatted_date)
        c.drawString(240, y_pos, topic_tag)
        c.drawString(450, y_pos, f"Page {idx + 1}")
        y_pos -= 20

        if y_pos < 50:
            c.showPage()
            y_pos = page_h - 50

    c.showPage()  # Move to Page 2 for actual screenshot pages

    # ---------------- SCREENSHOT PAGES ----------------
    for idx, record in enumerate(records, start=1):
        file_id, topic_tag, ts_str = record

        try:
            dt_obj = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            formatted_date = dt_obj.strftime('%Y-%m-%d (%A)')
        except Exception:
            formatted_date = ts_str

        # Draw Header Banner on every page
        c.setFillColor(colors.HexColor("#2C3E50"))
        c.rect(0, page_h - 50, page_w, 50, fill=True, stroke=False)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(20, page_h - 30, f"Topic: {topic_tag}")
        c.setFont("Helvetica", 10)
        c.drawRightString(page_w - 20, page_h - 30, f"Date: {formatted_date}")

        # Download high quality image
        tg_file = await context.bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()

        img = Image.open(BytesIO(img_bytes))
        img_temp_path = f"temp_{idx}_{file_id}.jpg"
        img.save(img_temp_path, quality=95)

        # Calculate fit dimensions without cropping or altering aspect ratio
        max_width = page_w - 40   # 20pt margin left/right
        max_height = page_h - 80  # Space under header

        orig_w, orig_h = img.size
        aspect = orig_w / float(orig_h)

        if max_width / max_height < aspect:
            draw_w = max_width
            draw_h = max_width / aspect
        else:
            draw_h = max_height
            draw_w = max_height * aspect

        x_pos = (page_w - draw_w) / 2
        y_pos = (page_h - 50 - draw_h) / 2

        # Draw image
        c.drawImage(img_temp_path, x_pos, y_pos, width=draw_w, height=draw_h, preserveAspectRatio=True)

        # Footer page number
        c.setFillColor(colors.HexColor("#7F8C8D"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 20, 15, f"Page {idx + 1} of {len(records) + 1}")

        c.showPage()

        # Clean temp image file
        if os.path.exists(img_temp_path):
            os.remove(img_temp_path)

    c.save()
    pdf_buffer.seek(0)
    pdf_file_name = f"{title.replace(' ', '_')}.pdf"

    # Send HD PDF to Telegram
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=pdf_buffer,
        filename=pdf_file_name,
        caption=f"📄 **{title}**\nIncludes {len(records)} page(s) + Summary."
    )

# PDF Command Handlers
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE timestamp LIKE ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (today,), "Daily Study Notes")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (week_ago,), "Weekly Study Notes")

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT file_id, topic, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Study Notes")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Please specify a topic! Example: `/topic_pdf #શિક્ષણ`", parse_mode="Markdown")
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
