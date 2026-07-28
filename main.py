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

# ==================== FAST IMAGE HANDLING ====================
async def fetch_image_fast(bot, file_id, idx):
    """Faster image download with compression"""
    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
        
        # Quick resize for speed
        if max(img.size) > 1200:
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        
        temp_path = f"temp_{idx}_{int(time.time())}.jpg"
        img.save(temp_path, "JPEG", quality=85, optimize=True)
        return idx, temp_path
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None

# ==================== FAST PDF GENERATOR ====================
def generate_pdf_fast(records, downloaded_images, title):
    """Generate PDF quickly with good quality"""
    pdf_buffer = BytesIO()
    
    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                           leftMargin=30, rightMargin=30,
                           topMargin=50, bottomMargin=50)
    
    story = []
    styles = getSampleStyleSheet()
    
    # Simple styles for speed
    header_style = ParagraphStyle(
        'CustomHeader',
        parent=styles['Heading1'],
        fontSize=14,
        textColor=colors.HexColor('#2C3E50'),
        alignment=TA_CENTER,
        spaceAfter=15
    )
    
    topic_style = ParagraphStyle(
        'TopicStyle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#34495E'),
        leftIndent=10,
        spaceAfter=8,
        backColor=colors.HexColor('#ECF0F1')
    )
    
    text_style = ParagraphStyle(
        'TextStyle',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=5,
        leftIndent=10,
        rightIndent=10
    )
    
    # Header
    story.append(Paragraph(f"📚 {title}", header_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", 
                          styles['Italic']))
    story.append(Spacer(1, 15))
    
    for idx, rec in enumerate(records):
        # Topic
        topic_text = f"<b>Topic:</b> {rec['topic']} | <b>Date:</b> {rec['timestamp'][:16]}"
        story.append(Paragraph(topic_text, topic_style))
        story.append(Spacer(1, 5))
        
        if rec['type'] == 'photo' and idx in downloaded_images:
            img_path = downloaded_images[idx]
            try:
                # Quick image sizing
                img = Image.open(img_path)
                img_width, img_height = img.size
                
                page_width = A4[0] - 60
                page_height = A4[1] - 120
                
                # Calculate best fit
                ratio = min(page_width / img_width, page_height / img_height)
                width = img_width * ratio * 0.9
                height = img_height * ratio * 0.9
                
                story.append(Spacer(1, 8))
                story.append(RLImage(img_path, width=width, height=height))
                story.append(Spacer(1, 8))
                
            except Exception as e:
                logger.error(f"Error adding image {idx}: {e}")
                story.append(Paragraph("⚠️ Image error", text_style))
            
        elif rec['type'] == 'text':
            text_content = safe_str(rec['content'])
            # Simple text formatting
            if len(text_content) > 500:
                text_content = text_content[:500] + "..."
            story.append(Paragraph(text_content, text_style))
        
        story.append(Spacer(1, 8))
        story.append(Paragraph("<hr/>", styles['Normal']))
        story.append(Spacer(1, 8))
    
    # Footer
    story.append(Spacer(1, 15))
    story.append(Paragraph(f"<i>Generated by Bittu Study Bot - {len(records)} entries</i>", 
                          styles['Italic']))
    
    # Build PDF
    doc.build(story)
    pdf_buffer.seek(0)
    
    # Cleanup
    for path in downloaded_images.values():
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
    
    return pdf_buffer

# ==================== PDF GENERATION WITH PROGRESS ====================
async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                query_sql: str, params: tuple, title: str):
    """Generate and send PDF with instant download"""
    chat_id = update.effective_chat.id
    
    # Get the message to edit
    if update.message:
        status_msg = await update.message.reply_text("⏳ Generating PDF...")
    elif update.callback_query:
        status_msg = await update.callback_query.message.reply_text("⏳ Generating PDF...")
    else:
        return
    
    try:
        # Check cache first
        cache_key = get_cache_key(query_sql, params, title)
        if cache_key in pdf_cache:
            cached_time, cached_pdf = pdf_cache[cache_key]
            if time.time() - cached_time < CACHE_EXPIRY:
                await status_msg.edit_text("⚡ Sending cached PDF...")
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=BytesIO(cached_pdf),
                    filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                    caption=f"📄 **{title}** Ready!"
                )
                await status_msg.delete()
                return

        # Query database
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query_sql, params)
        records = cursor.fetchall()
        conn.close()

        if not records:
            await status_msg.edit_text("⚠️ No records found.")
            return

        await status_msg.edit_text(f"📊 Processing {len(records)} items...")

        # Download images in parallel
        photo_records = [(idx, r) for idx, r in enumerate(records) if r['type'] == 'photo']
        downloaded_images = {}
        
        if photo_records:
            await status_msg.edit_text(f"📸 Downloading {len(photo_records)} images...")
            download_tasks = []
            for idx, rec in photo_records:
                download_tasks.append(fetch_image_fast(context.bot, rec['content'], idx))
            
            results = await asyncio.gather(*download_tasks)
            downloaded_images = {idx: path for idx, path in results if path}

        # Generate PDF
        await status_msg.edit_text("📄 Creating PDF...")
        loop = asyncio.get_event_loop()
        pdf_buffer = await loop.run_in_executor(
            executor,
            generate_pdf_fast,
            records,
            downloaded_images,
            title
        )

        # Cache
        pdf_data = pdf_buffer.getvalue()
        pdf_cache[cache_key] = (time.time(), pdf_data)

        # Send PDF immediately
        await status_msg.edit_text("📤 Sending PDF...")
        await context.bot.send_document(
            chat_id=chat_id,
            document=BytesIO(pdf_data),
            filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
            caption=f"📄 **{title}** Ready! ({len(records)} items)"
        )
        
        # Delete status message
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:100]}")

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
         InlineKeyboardButton("🔍 Search", callback_data="menu_search")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
         InlineKeyboardButton("🗑️ Clear Cache", callback_data="menu_clear_cache")],
        [InlineKeyboardButton("🤖 AI Help", callback_data="menu_ai"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        "📚 *Bittu Study Notes Bot*\n\n"
        "📸 **Save Content:**\n"
        "Send screenshots or text, then reply with `#topic`.\n\n"
        "📊 **Generate PDFs:**\n"
        "Use buttons below or commands:\n"
        "/daily_pdf, /weekly_pdf, /monthly_pdf, /topic_pdf\n\n"
        "👇 *Use menu:*"
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
            keyboard = []
            for topic in topics[:15]:
                keyboard.append([InlineKeyboardButton(f"📌 {topic}", callback_data=f"topic_select_{topic}")])
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🏷️ *Select a topic:*\n\nOr use `/topic_pdf topic_name`",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("⚠️ No topics found. Add some data first!")
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

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    cursor.execute("""
        SELECT topic, COUNT(*) as count 
        FROM content_store 
        GROUP BY topic 
        ORDER BY count DESC 
        LIMIT 5
    """)
    top_topics = cursor.fetchall()
    
    conn.close()
    
    msg = f"📊 *Study Statistics*\n\n"
    msg += f"📚 Total Notes: {total}\n"
    msg += f"🏷️ Topics: {topics}\n"
    msg += f"📅 Today: {today_count} entries\n"
    msg += f"📆 This Week: {week_count} entries\n\n"
    
    if top_topics:
        msg += "*Top Topics:*\n"
        for topic, count in top_topics:
            bar = "▰" * min(count, 10) + "▱" * max(0, 10 - min(count, 10))
            msg += f"• {topic}: {bar} {count}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def search_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔍 *Search Notes*\n\n"
            "Usage: `/search your query`\n"
            "Example: `/search physics`\n"
            "Or: `/search topic:chemistry`"
        )
        return
    
    query = ' '.join(context.args).lower()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    if 'topic:' in query:
        topic = query.split('topic:')[1].strip()
        cursor.execute("SELECT content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? LIMIT 10", (f'%{topic}%',))
    else:
        cursor.execute("SELECT content, topic, timestamp FROM content_store WHERE LOWER(content) LIKE ? OR LOWER(topic) LIKE ? LIMIT 10", (f'%{query}%', f'%{query}%'))
    
    results = cursor.fetchall()
    conn.close()
    
    if not results:
        await update.message.reply_text(f"❌ No results found for '{query}'")
        return
    
    msg = f"🔍 *Results ({len(results)}) for '{query}':*\n\n"
    for content, topic, timestamp in results[:5]:
        preview = content[:150] + "..." if len(content) > 150 else content
        msg += f"📌 *{topic}* ({timestamp[:16]})\n{preview}\n\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        help_text = """
🤖 *AI Assistant Help*

*Commands:*
• /ai topics - List all topics
• /ai count - Total notes count
• /ai today - Today's notes count
• /ai search [text] - Search notes

*Examples:*
• /ai topics
• /ai count
• /ai today
• /ai search chemistry
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")
        return
    
    query = ' '.join(context.args).lower()
    
    if query == 'topics':
        topics = get_all_topics()
        if topics:
            msg = "📋 *All Topics:*\n\n"
            for topic in topics:
                msg += f"• {topic}\n"
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text("No topics found.")
        
    elif query == 'count':
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM content_store")
        count = cursor.fetchone()[0]
        conn.close()
        await update.message.reply_text(f"📚 Total notes: {count}")
        
    elif query == 'today':
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM content_store WHERE date(timestamp) = ?", (today,))
        count = cursor.fetchone()[0]
        conn.close()
        await update.message.reply_text(f"📅 Today's notes: {count}")
        
    elif query.startswith('search'):
        search_text = query.replace('search', '').strip()
        if search_text:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT content, topic FROM content_store WHERE LOWER(content) LIKE ? LIMIT 5", (f'%{search_text}%',))
            results = cursor.fetchall()
            conn.close()
            
            if results:
                msg = f"🔍 *Results for '{search_text}':*\n\n"
                for content, topic in results:
                    preview = content[:100] + "..." if len(content) > 100 else content
                    msg += f"📌 {topic}\n{preview}\n\n"
                await update.message.reply_text(msg, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"No results found for '{search_text}'")
        else:
            await update.message.reply_text("Usage: /ai search [text]")
    else:
        await update.message.reply_text("Unknown command. Use /ai for help.")

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

*🔍 Search & Stats:*
• /search [query] - Search notes
• /stats - Study statistics
• /alltopics - Show all topics
• /mydata - Show recent entries

*🤖 AI Assistant:*
• /ai topics - List topics
• /ai count - Total notes
• /ai today - Today's notes
• /ai search [text] - Search

*📌 Other:*
• /start - Main menu
• /cancel - Cancel operation
• /clearcache - Clear PDF cache
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
    
    if data.startswith("topic_select_"):
        topic = data.replace("topic_select_", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (f"%{topic.lower()}%",),
                                    f"Topic PDF - {topic}")
        return
    
    if data == "menu_daily":
        await daily_pdf(update, context)
    elif data == "menu_weekly":
        await weekly_pdf(update, context)
    elif data == "menu_monthly":
        await monthly_pdf(update, context)
    elif data == "menu_topic":
        topics = get_all_topics()
        if topics:
            keyboard = []
            for topic in topics[:15]:
                keyboard.append([InlineKeyboardButton(f"📌 {topic}", callback_data=f"topic_select_{topic}")])
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(
                "🏷️ *Select a topic:*",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await query.message.edit_text("⚠️ No topics found. Add some data first!")
    elif data == "menu_my_data":
        await my_data(update, context)
    elif data == "menu_all_topics":
        await all_topics(update, context)
    elif data == "menu_instant":
        await instant_report(update, context)
    elif data == "menu_search":
        await query.message.edit_text(
            "🔍 *Search Notes*\n\n"
            "Use `/search your query`\n"
            "Example: `/search physics`"
        )
    elif data == "menu_stats":
        await stats_command(update, context)
    elif data == "menu_clear_cache":
        pdf_cache.clear()
        await query.message.edit_text("🗑️ Cache cleared successfully!")
    elif data == "menu_ai":
        await ai_command(update, context)
    elif data == "menu_help":
        await help_command(update, context)
    elif data == "menu_back":
        await start(update, context)

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
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("daily_pdf", daily_pdf))
    application.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    application.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    application.add_handler(CommandHandler("topic_pdf", topic_pdf))
    application.add_handler(CommandHandler("clearcache", clear_cache))
    application.add_handler(CommandHandler("alltopics", all_topics))
    application.add_handler(CommandHandler("mydata", my_data))
    application.add_handler(CommandHandler("instant", instant_report))
    application.add_handler(CommandHandler("search", search_notes))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(conv_handler)
    
    # Start bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("🚀 Bot Started Successfully!")
    
    # Start Flask
    await run_flask()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
