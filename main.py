import os
import asyncio
import logging
import sqlite3
import hashlib
import time
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.utils import ImageReader

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ==================== CONFIGURATION ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
PORT = int(os.environ.get("PORT", 10000))

# Conversation States
WAITING_FOR_TOPIC = 1

DB_FILE = "study_data.db"
CACHE_EXPIRY = 3600
MAX_WORKERS = 4

# Global executor
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
pdf_cache = {}

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS content_store (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            topic TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_topic ON content_store(topic)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON content_store(timestamp)")
    conn.commit()
    conn.close()

init_db()

def safe_str(text: str) -> str:
    if not text:
        return ""
    return "".join(c if ord(c) < 128 else "" for c in text).strip() or "Study Note"

def get_cache_key(query_sql: str, params: tuple, title: str) -> str:
    key_str = f"{query_sql}_{params}_{title}"
    return hashlib.md5(key_str.encode()).hexdigest()

def get_all_topics():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT topic FROM content_store ORDER BY topic")
    topics = [row[0] for row in cursor.fetchall()]
    conn.close()
    return topics

# ==================== FLASK SERVER ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bittu Study Bot is Live!"

@app.route('/api/stats')
def api_stats():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM content_store")
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT topic) FROM content_store")
    topics = cursor.fetchone()[0]
    
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM content_store WHERE date(timestamp) = ?", (today,))
    today_count = cursor.fetchone()[0]
    
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM content_store WHERE date(timestamp) >= ?", (week_ago,))
    week_count = cursor.fetchone()[0]
    
    conn.close()
    
    return jsonify({
        'total': total,
        'topics': topics,
        'today': today_count,
        'week': week_count
    })

async def run_flask():
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", PORT, app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

# ==================== IMAGE HANDLING ====================
async def fetch_image_optimized(bot, file_id, idx):
    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
        img.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
        temp_path = f"temp_{idx}_{int(time.time())}.jpg"
        img.save(temp_path, "JPEG", quality=85, optimize=True)
        return idx, temp_path
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None

# ==================== PDF GENERATOR ====================
def generate_pdf_sync(records, downloaded_images, title, page_num=1):
    pdf_buffer = BytesIO()
    
    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4, 
                           leftMargin=30, rightMargin=30,
                           topMargin=50, bottomMargin=30)
    story = []
    styles = getSampleStyleSheet()
    
    header_style = ParagraphStyle(
        'CustomHeader',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=colors.HexColor('#2C3E50'),
        alignment=TA_CENTER,
        spaceAfter=20
    )
    
    topic_style = ParagraphStyle(
        'TopicStyle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#34495E'),
        leftIndent=10,
        spaceAfter=10,
        backColor=colors.HexColor('#ECF0F1')
    )
    
    text_style = ParagraphStyle(
        'TextStyle',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=8,
        leftIndent=10,
        rightIndent=10
    )
    
    story.append(Paragraph(f"📚 {title}", header_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", 
                          styles['Italic']))
    story.append(Spacer(1, 20))
    
    for idx, rec in enumerate(records):
        topic_text = f"<b>Topic:</b> {rec['topic']} | <b>Date:</b> {rec['timestamp'][:16]}"
        story.append(Paragraph(topic_text, topic_style))
        
        if rec['type'] == 'photo' and idx in downloaded_images:
            img_path = downloaded_images[idx]
            try:
                img = ImageReader(img_path)
                img_width, img_height = img.getSize()
                
                max_width = 500
                max_height = 400
                
                aspect = img_width / float(img_height)
                if max_width / max_height < aspect:
                    width = max_width
                    height = max_width / aspect
                else:
                    height = max_height
                    width = max_height * aspect
                
                story.append(Spacer(1, 10))
                story.append(RLImage(img_path, width=width, height=height))
                story.append(Spacer(1, 10))
            except Exception as e:
                logger.error(f"Error adding image {idx}: {e}")
                story.append(Paragraph("⚠️ Image could not be loaded", text_style))
            
        elif rec['type'] == 'text':
            text_content = safe_str(rec['content'])
            paragraphs = text_content.split('\n')
            for para in paragraphs:
                if para.strip():
                    story.append(Paragraph(para, text_style))
                    story.append(Spacer(1, 5))
        
        story.append(Spacer(1, 15))
        story.append(Paragraph("<hr/>", styles['Normal']))
        story.append(Spacer(1, 10))
    
    story.append(Spacer(1, 20))
    story.append(Paragraph(f"<i>Page {page_num} - Generated by Bittu Study Bot</i>", 
                          styles['Italic']))
    
    doc.build(story)
    pdf_buffer.seek(0)
    
    for path in downloaded_images.values():
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
    
    return pdf_buffer

async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                query_sql: str, params: tuple, title: str):
    chat_id = update.effective_chat.id
    message_to_edit = update.message if update.message else update.callback_query.message
    
    cache_key = get_cache_key(query_sql, params, title)
    if cache_key in pdf_cache:
        cached_time, cached_pdf = pdf_cache[cache_key]
        if time.time() - cached_time < CACHE_EXPIRY:
            await message_to_edit.reply_text("⚡ Loading from cache...")
            await context.bot.send_document(
                chat_id=chat_id,
                document=BytesIO(cached_pdf),
                filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                caption=f"📄 **{title}** Ready! (Cached)"
            )
            return

    status_msg = await message_to_edit.reply_text("⏳ Generating your PDF...")

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await status_msg.edit_text("⚠️ No records found for this selection.")
        return

    photo_records = [(idx, r) for idx, r in enumerate(records) if r['type'] == 'photo']
    download_tasks = []
    for idx, rec in photo_records:
        download_tasks.append(fetch_image_optimized(context.bot, rec['content'], idx))
    
    downloaded_images = {}
    if download_tasks:
        results = await asyncio.gather(*download_tasks)
        downloaded_images = {idx: path for idx, path in results if path}

    loop = asyncio.get_event_loop()
    pdf_buffer = await loop.run_in_executor(
        executor,
        generate_pdf_sync,
        records,
        downloaded_images,
        title,
        1
    )

    pdf_data = pdf_buffer.getvalue()
    pdf_cache[cache_key] = (time.time(), pdf_data)

    await status_msg.edit_text("📤 Uploading your PDF...")
    await context.bot.send_document(
        chat_id=chat_id,
        document=BytesIO(pdf_data),
        filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
        caption=f"📄 **{title}** Ready! ({len(records)} items)"
    )
    await status_msg.delete()

# ==================== BOT HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📅 Daily PDF", callback_data="menu_daily"),
         InlineKeyboardButton("📊 Weekly PDF", callback_data="menu_weekly")],
        [InlineKeyboardButton("📈 Monthly PDF", callback_data="menu_monthly"),
         InlineKeyboardButton("🏷️ Topic PDF", callback_data="menu_topic")],
        [InlineKeyboardButton("📁 My Data", callback_data="menu_my_data"),
         InlineKeyboardButton("📋 All Topics", callback_data="menu_all_topics")],
        [InlineKeyboardButton("⚡ Instant Report", callback_data="menu_instant"),
         InlineKeyboardButton("🗑️ Clear Cache", callback_data="menu_clear_cache")],
        [InlineKeyboardButton("❓ Help", callback_data="menu_help")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        "📚 *Bittu Study Notes Bot*\n\n"
        "📸 **Save Content:**\n"
        "Send screenshots or text, then reply with `#topic`.\n\n"
        "📊 **Generate PDFs:**\n"
        "Use the buttons below or commands:\n"
        "/daily_pdf, /weekly_pdf, /monthly_pdf, /topic_pdf\n\n"
        "👇 *Use the menu below:*"
    )
    
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    if 'pending_items' not in context.user_data:
        context.user_data['pending_items'] = []
    
    context.user_data['pending_items'].append(('photo', photo_file_id))
    
    if not context.user_data.get('asked_topic'):
        context.user_data['asked_topic'] = True
        await update.message.reply_text(
            "📌 **Content received!**\nReply with **#topic** (e.g., `#chemistry`):",
            parse_mode="Markdown"
        )
    return WAITING_FOR_TOPIC

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_content = update.message.text.strip()
    if text_content.startswith("/"):
        return
    
    if 'pending_items' not in context.user_data:
        context.user_data['pending_items'] = []
    
    context.user_data['pending_items'].append(('text', text_content))
    
    if not context.user_data.get('asked_topic'):
        context.user_data['asked_topic'] = True
        await update.message.reply_text(
            "📝 **Text received!**\nReply with **#topic** (e.g., `#chemistry`):",
            parse_mode="Markdown"
        )
    return WAITING_FOR_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"):
        topic = f"#{topic}"
    
    items = context.user_data.get('pending_items', [])
    if not items:
        await update.message.reply_text("❌ Session expired. Please resend.")
        return ConversationHandler.END
    
    status_msg = await update.message.reply_text(f"💾 Saving {len(items)} item(s)...")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for item_type, content in items:
        cursor.execute("INSERT INTO content_store (type, content, topic) VALUES (?, ?, ?)",
                      (item_type, content, topic))
        if item_type == 'photo' and CHANNEL_ID:
            try:
                caption = f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=content,
                                            caption=caption, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to post to channel: {e}")
    conn.commit()
    conn.close()
    
    await status_msg.edit_text(f"✅ **Saved {len(items)} item(s) under {topic}!**",
                              parse_mode="Markdown")
    
    context.user_data.pop('pending_items', None)
    context.user_data.pop('asked_topic', None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_items', None)
    context.user_data.pop('asked_topic', None)
    await update.message.reply_text("❌ Action canceled.")
    return ConversationHandler.END

# ==================== COMMAND HANDLERS ====================
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        await generate_and_send_pdf(update, context, sql, (today, f"%{topic.lower()}%"),
                                    f"Daily Notes - #{topic}")
    else:
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (today,), "Daily Notes (All)")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    heading = f"Weekly Report ({start_date} to {end_date})"
    
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago, f"%{topic.lower()}%"),
                                    f"{heading} - #{topic}")
    else:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago,), heading)

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago, f"%{topic.lower()}%"),
                                    f"Monthly Notes - #{topic}")
    else:
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Notes (All)")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        topics = get_all_topics()
        if topics:
            msg = "🏷️ *Available Topics:*\n\n"
            for topic in topics[:10]:
                msg += f"• `{topic}`\n"
            if len(topics) > 10:
                msg += f"\n... and {len(topics) - 10} more"
            await update.message.reply_text(msg + "\n\nUsage: `/topic_pdf topic_name`",
                                          parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ No topics found. Usage: `/topic_pdf topic_name`",
                                          parse_mode="Markdown")
        return
    
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf(update, context, sql, (f"%{user_topic.lower()}%",),
                                f"Topic PDF - #{user_topic}")

async def clear_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_cache.clear()
    await update.message.reply_text("🗑️ Cache cleared successfully!")

async def all_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = get_all_topics()
    if not topics:
        await update.message.reply_text("No topics found.")
        return
    
    msg = "📋 *All Topics:*\n\n"
    for topic in topics:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM content_store WHERE topic = ?", (topic,))
        count = cursor.fetchone()[0]
        conn.close()
        msg += f"• `{topic}` ({count} items)\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, type, topic, timestamp 
        FROM content_store 
        ORDER BY timestamp DESC 
        LIMIT 10
    """)
    records = cursor.fetchall()
    conn.close()
    
    if not records:
        await update.message.reply_text("No data found.")
        return
    
    msg = "📁 *Your Recent Entries:*\n\n"
    for rec in records:
        msg += f"• #{rec[0]} | {rec[1]} | {rec[2]} | {rec[3][:16]}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def instant_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf(update, context, sql, (today,), "📊 Instant Report - Today")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📚 *Bittu Study Bot Help*

*📸 Save Content:*
• Send photo → Reply with #topic
• Send text → Reply with #topic

*📊 Generate PDFs:*
• /daily_pdf - Today's notes
• /weekly_pdf - Last 7 days
• /monthly_pdf - Last 30 days
• /topic_pdf [topic] - Topic wise

*📌 Commands:*
• /start - Main menu
• /cancel - Cancel operation
• /clearcache - Clear PDF cache
• /alltopics - Show all topics
• /mydata - Show recent entries
• /instant - Today's instant report

*💡 Tips:*
• Use #topic for organization
• Choose from menu for quick actions
• Cache makes PDFs faster!
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ==================== BUTTON HANDLERS ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "menu_daily":
        await daily_pdf(update, context)
    elif data == "menu_weekly":
        await weekly_pdf(update, context)
    elif data == "menu_monthly":
        await monthly_pdf(update, context)
    elif data == "menu_topic":
        await topic_pdf(update, context)
    elif data == "menu_my_data":
        await my_data(update, context)
    elif data == "menu_all_topics":
        await all_topics(update, context)
    elif data == "menu_instant":
        await instant_report(update, context)
    elif data == "menu_clear_cache":
        pdf_cache.clear()
        await query.message.edit_text("🗑️ Cache cleared successfully!")
    elif data == "menu_help":
        await help_command(update, context)

# ==================== MAIN ====================
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, receive_photo),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
        ],
        states={
            WAITING_FOR_TOPIC: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Core commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("daily_pdf", daily_pdf))
    application.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    application.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    application.add_handler(CommandHandler("topic_pdf", topic_pdf))
    application.add_handler(CommandHandler("clearcache", clear_cache))
    application.add_handler(CommandHandler("alltopics", all_topics))
    application.add_handler(CommandHandler("mydata", my_data))
    application.add_handler(CommandHandler("instant", instant_report))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    
    # Callback handler
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(conv_handler)
    
    # Start bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram Bot Started Successfully!")
    
    # Start Flask
    await run_flask()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
