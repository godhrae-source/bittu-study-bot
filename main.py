import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image

import google.generativeai as genai
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

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
            summary TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Helper function to sanitize text for ReportLab standard canvas
def safe_str(text: str) -> str:
    if not text:
        return ""
    return "".join(c if ord(c) < 128 else "" for c in text).strip() or "Study Note"

# ----------------- AI Summary Helper -----------------
async def summarize_image_with_ai(image_bytes: bytes) -> str:
    if not GEMINI_API_KEY:
        return "AI summary omitted (Set GEMINI_API_KEY in Render to enable)."

    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        img = Image.open(BytesIO(image_bytes))
        prompt = (
            "Analyze this study screenshot or newspaper clipping. "
            "Provide a concise summary in 2 bullet points."
        )
        response = await asyncio.to_thread(model.generate_content, [prompt, img])
        return response.text.strip() if response.text else "Summary unavailable."
    except Exception as e:
        logger.error(f"Gemini AI error: {e}")
        return "Summary could not be generated."

# ----------------- Flask Web Server for Render -----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Bittu Study Bot with AI Summary is Live!"

async def run_flask():
    port = int(os.environ.get("PORT", 10000))
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", port, app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

# ----------------- Telegram Bot Handlers -----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📚 *Bittu Study Notes Bot (HD PDF + AI Summary)*\n\n"
        "📸 **How to save notes:**\n"
        "Send any screenshot, and reply with `#topic` when asked.\n\n"
        "📄 **Get Compiled HD PDFs:**\n"
        "• `/daily_pdf` - Today's notes + Summary\n"
        "• `/weekly_pdf` - Last 7 days + Summary\n"
        "• `/monthly_pdf` - Last 30 days + Summary\n"
        "• `/topic_pdf #topic` - Filter by specific topic"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    context.user_data['pending_photo'] = photo_file_id
    
    await update.message.reply_text(
        "📌 **Screenshot received!**\nPlease reply with the **#topic** (e.g., `#shiksha`, `#chemistry`):",
        parse_mode="Markdown"
    )
    return WAITING_FOR_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"):
        topic = f"#{topic}"
        
    file_id = context.user_data.get('pending_photo')
    if not file_id:
        await update.message.reply_text("❌ Session expired. Please resend the photo.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("🔍 Processing screenshot & generating AI summary...")

    # Download image & generate summary
    tg_file = await context.bot.get_file(file_id)
    img_bytes = await tg_file.download_as_bytearray()
    summary_text = await summarize_image_with_ai(img_bytes)

    # Save to SQLite Database
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO screenshots (file_id, topic, summary) VALUES (?, ?, ?)", (file_id, topic, summary_text))
    conn.commit()
    conn.close()

    # Forward to Channel
    if CHANNEL_ID:
        try:
            caption = f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n🤖 **Summary:**\n{summary_text[:800]}"
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to post to channel: {e}")

    await status_msg.edit_text(f"✅ **Saved under {topic}!**\n\n🤖 **AI Summary:**\n{summary_text}", parse_mode="Markdown")
    context.user_data.pop('pending_photo', None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_photo', None)
    await update.message.reply_text("Action canceled.")
    return ConversationHandler.END

# ----------------- PDF Generator Engine -----------------

async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, query_sql: str, params: tuple, title: str):
    await update.message.reply_text("⏳ Compiling HD PDF & generating summary page... Please wait.")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await update.message.reply_text("⚠️ No screenshots found for this timeframe or topic.")
        return

    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_w, page_h = A4

    # --- PAGE 1: Summary Index Page ---
    c.setFillColor(colors.HexColor("#1A2B4C"))
    c.rect(0, page_h - 90, page_w, 90, fill=True, stroke=False)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(25, page_h - 40, f"Topic PDF Report")
    c.setFont("Helvetica", 10)
    c.drawString(25, page_h - 65, f"Generated: {datetime.now().strftime('%Y-%m-%d (%A)')}")

    # Index List
    y = page_h - 120
    c.setFillColor(colors.HexColor("#2C3E50"))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(25, y, f"Total Screenshots Compiled: {len(records)}")
    y -= 30

    for idx, r in enumerate(records, start=1):
        _, topic_tag, summary_txt, ts_str = r
        c.setFont("Helvetica-Bold", 10)
        c.drawString(25, y, f"[{idx}] Topic: {safe_str(topic_tag)} | Date: {ts_str[:10]}")
        y -= 15

        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor("#444444"))
        
        clean_sum = safe_str(summary_txt or "Summary recorded").replace("\n", " ")
        if len(clean_sum) > 100:
            clean_sum = clean_sum[:97] + "..."
            
        c.drawString(35, y, f"Note: {clean_sum}")
        y -= 25
        c.setFillColor(colors.HexColor("#2C3E50"))

        if y < 60:
            c.showPage()
            y = page_h - 50

    c.showPage()

    # --- SCREENSHOT PAGES ---
    for idx, record in enumerate(records, start=1):
        file_id, topic_tag, summary_txt, ts_str = record

        # Header Banner
        c.setFillColor(colors.HexColor("#2C3E50"))
        c.rect(0, page_h - 45, page_w, 45, fill=True, stroke=False)

        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20, page_h - 28, f"Topic: {safe_str(topic_tag)}")
        c.drawRightString(page_w - 20, page_h - 28, f"Date: {ts_str[:10]}")

        # Image Download & Placement
        tg_file = await context.bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()

        img = Image.open(BytesIO(img_bytes))
        temp_img_path = f"temp_{idx}.jpg"
        img.save(temp_img_path, quality=95)

        max_w = page_w - 40
        max_h = page_h - 70

        orig_w, orig_h = img.size
        aspect = orig_w / float(orig_h)

        if max_w / max_h < aspect:
            draw_w = max_w
            draw_h = max_w / aspect
        else:
            draw_h = max_h
            draw_w = max_h * aspect

        x = (page_w - draw_w) / 2
        y_img = (page_h - 45 - draw_h) / 2

        c.drawImage(temp_img_path, x, y_img, width=draw_w, height=draw_h, preserveAspectRatio=True)

        c.setFillColor(colors.HexColor("#7F8C8D"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 20, 15, f"Page {idx + 1} of {len(records) + 1}")

        c.showPage()
        if os.path.exists(temp_img_path):
            os.remove(temp_img_path)

    c.save()
    pdf_buffer.seek(0)

    clean_filename = f"Study_Notes_{datetime.now().strftime('%Y%m%d')}.pdf"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=pdf_buffer,
        filename=clean_filename,
        caption=f"📄 **{title}** Ready!\nIncludes {len(records)} screenshot(s) + Index Page."
    )

# PDF Command Handlers
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT file_id, topic, summary, timestamp FROM screenshots WHERE timestamp LIKE ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (today,), "Daily Study Notes")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT file_id, topic, summary, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (week_ago,), "Weekly Study Notes")

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT file_id, topic, summary, timestamp FROM screenshots WHERE timestamp >= ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Study Notes")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Specify topic! Example: `/topic_pdf shiksa`", parse_mode="Markdown")
        return
    
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT file_id, topic, summary, timestamp FROM screenshots WHERE LOWER(topic) LIKE ? ORDER BY id ASC"
    await generate_and_send_pdf(update, context, sql, (f"%{user_topic.lower()}%",), f"Topic PDF - #{user_topic}")

# ----------------- Main Execution Block -----------------

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

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
    
    # Drops pending updates from old instances to fix Conflict error
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram Bot Polling Started!")

    # Starts Flask web server for Render keep-alive
    await run_flask()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
