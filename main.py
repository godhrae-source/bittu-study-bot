import os
import asyncio
import logging
import sqlite3
import hashlib
import time
import re
import threading
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Image as RLImage, PageBreak, Flowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.utils import ImageReader

from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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
CHANNEL_ID = os.environ.get("CHANNEL_ID")  # SET THIS IN RENDER ENV VARIABLES
PORT = int(os.environ.get("PORT", 10000))

# Conversation States
WAITING_FOR_TOPIC = 1
WAITING_FOR_DATE = 2

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
    return "".join(c if ord(c) < 128 or (0x0A80 <= ord(c) <= 0x0AFF) else "" for c in text).strip() or "Study Note"

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

def get_available_dates():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT DATE(timestamp) FROM content_store ORDER BY DATE(timestamp) DESC")
    dates = [row[0] for row in cursor.fetchall()]
    conn.close()
    return dates

# ==================== FLASK SERVER (SEPARATE THREAD) ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bittu Study Bot is Live!"

@app.route('/health')
@app.route('/ping')
def health_check():
    return jsonify({'status': 'alive', 'timestamp': datetime.now().isoformat()})

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

def run_flask():
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", PORT, app)
    logger.info(f"🌐 Flask server starting on port {PORT}...")
    server.serve_forever()

# ==================== IMAGE HANDLING ====================
async def fetch_image_fast(bot, file_id, idx):
    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
        temp_path = f"temp_{idx}_{int(time.time())}.jpg"
        img.save(temp_path, "JPEG", quality=90, optimize=True)
        return (idx, temp_path, img.size)
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return (idx, None, None)

# ==================== SUPER HD PDF GENERATOR ====================
def generate_smart_pdf(records, downloaded_images, title, summary_text, record_ids):
    pdf_buffer = BytesIO()
    margin = 5
    page_width, page_height = A4
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    y_position = page_height - margin
    current_page = 1
    total_records = len(records)
    
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(colors.HexColor('#1A237E'))
    c.drawCentredString(page_width/2, y_position - 10, title[:50])
    c.setFont("Helvetica", 10)
    c.setFillColor(colors.HexColor('#455A64'))
    c.drawCentredString(page_width/2, y_position - 25, f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | Total: {total_records} items")
    y_position -= 40
    
    if summary_text:
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(colors.HexColor('#1A5276'))
        c.drawString(margin + 5, y_position - 10, "📝 Summary:")
        c.setFont("Helvetica", 9)
        c.setFillColor(colors.HexColor('#2C3E50'))
        summary_lines = []
        words = summary_text.split()
        line = ""
        for word in words:
            if len(line + " " + word) < 80:
                line += " " + word if line else word
            else:
                summary_lines.append(line)
                line = word
        if line: summary_lines.append(line)
        for i, line in enumerate(summary_lines[:3]):
            c.drawString(margin + 10, y_position - 25 - (i * 12), line[:80])
        y_position -= 30 + (len(summary_lines[:3]) * 12)
    
    c.setStrokeColor(colors.HexColor('#BDC3C7'))
    c.setLineWidth(0.5)
    c.line(margin, y_position - 5, page_width - margin, y_position - 5)
    y_position -= 20
    
    for idx, rec in enumerate(records):
        if y_position < 50:
            c.showPage()
            current_page += 1
            y_position = page_height - margin
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.HexColor('#7F8C8D'))
            c.drawCentredString(page_width/2, y_position - 5, f"Page {current_page}")
            y_position -= 15
        
        topic_text = f"📌 {rec['topic']} | {rec['timestamp'][:16]}"
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(colors.HexColor('#2C3E50'))
        c.setFillColor(colors.HexColor('#ECF0F1'))
        c.rect(margin, y_position - 15, page_width - 2*margin, 18, fill=True, stroke=False)
        c.setFillColor(colors.HexColor('#2C3E50'))
        c.drawString(margin + 10, y_position - 10, topic_text[:70])
        y_position -= 22
        
        if rec['type'] == 'photo' and idx in downloaded_images:
            img_path = downloaded_images[idx]
            if img_path and os.path.exists(img_path):
                try:
                    img_reader = ImageReader(img_path)
                    orig_w, orig_h = img_reader.getSize()
                    avail_w = page_width - (2 * margin)
                    avail_h = y_position - margin
                    scale_w = (avail_w - 4) / orig_w
                    scale_h = (avail_h - 4) / orig_h
                    scale = min(scale_w, scale_h)
                    final_w = orig_w * scale
                    final_h = orig_h * scale
                    border_width = 1.5
                    c.setStrokeColor(colors.HexColor('#2C3E50'))
                    c.setLineWidth(border_width)
                    x_pos = (page_width - final_w) / 2
                    c.rect(x_pos - border_width, y_position - final_h - border_width, final_w + 2*border_width, final_h + 2*border_width)
                    c.drawImage(img_reader, x_pos, y_position - final_h, width=final_w, height=final_h, mask='auto')
                    y_position -= (final_h + 10)
                except Exception as e:
                    logger.error(f"Error adding image {idx}: {e}")
                    c.setFont("Helvetica", 8)
                    c.setFillColor(colors.HexColor('#E74C3C'))
                    c.drawString(margin + 10, y_position - 10, "⚠️ Image error")
                    y_position -= 20
            else:
                c.setFont("Helvetica", 8)
                c.setFillColor(colors.HexColor('#E74C3C'))
                c.drawString(margin + 10, y_position - 10, "⚠️ Image not found")
                y_position -= 20
        elif rec['type'] == 'text':
            text_content = safe_str(rec['content'])
            if len(text_content) > 200: text_content = text_content[:200] + "..."
            c.setFont("Helvetica", 9)
            c.setFillColor(colors.HexColor('#2C3E50'))
            lines = []
            words = text_content.split()
            line = ""
            for word in words:
                if len(line + " " + word) < 80:
                    line += " " + word if line else word
                else:
                    lines.append(line)
                    line = word
            if line: lines.append(line)
            for i, line in enumerate(lines[:10]):
                c.drawString(margin + 10, y_position - 12 - (i * 12), line[:80])
                y_position -= 12
            y_position -= 10
        
        c.setStrokeColor(colors.HexColor('#BDC3C7'))
        c.setLineWidth(0.5)
        c.line(margin, y_position - 5, page_width - margin, y_position - 5)
        y_position -= 12
    
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor('#7F8C8D'))
    c.drawCentredString(page_width/2, 20, f"Generated by Bittu Study Bot | {total_records} entries")
    c.save()
    pdf_buffer.seek(0)
    for path in downloaded_images.values():
        if path and os.path.exists(path):
            try: os.remove(path)
            except: pass
    return pdf_buffer

# ==================== PDF GENERATION (CRASH-PROOF & CHANNEL SAVER) ====================
async def generate_and_send_pdf_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                              query_sql: str, params: tuple, title: str):
    chat_id = update.effective_chat.id
    status_msg = None
    
    try:
        if update.message:
            status_msg = await update.message.reply_text("⏳ PDF બનાવી રહ્યા છીએ...")
        elif update.callback_query and update.callback_query.message:
            try:
                status_msg = await update.callback_query.message.edit_text("⏳ PDF બનાવી રહ્યા છીએ...")
            except:
                status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ PDF બનાવી રહ્યા છીએ...")
        else:
            status_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ PDF બનાવી રહ્યા છીએ...")
    except Exception as e:
        logger.error(f"Failed to send initial status: {e}")
        return

    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query_sql, params)
        records = cursor.fetchall()
        conn.close()

        if not records:
            if status_msg: await status_msg.edit_text("⚠️ કોઈ રેકોર્ડ મળ્યા નથી.")
            return

        # --- 🚀 AUTO-SPLIT LOGIC (Prevents RAM Crashes) ---
        if len(records) > 8:
            if status_msg: await status_msg.edit_text(f"📊 {len(records)} items found. Auto-splitting into smaller PDFs to prevent crashes...")
            
            total_batches = (len(records) + 7) // 8
            batch_number = 1
            
            for i in range(0, len(records), 8):
                batch = records[i:i+8]
                batch_title = f"{title} (Batch {batch_number} of {total_batches})"
                
                await generate_batch_pdf(context, batch, batch_title, chat_id)
                
                if i + 8 < len(records):
                    await asyncio.sleep(15) # Give server time to clear RAM
                batch_number += 1
            
            if status_msg: 
                try: await status_msg.delete()
                except: pass
            return
        # --- END OF AUTO-SPLIT LOGIC ---

        # If less than 8 items, generate normally
        await generate_batch_pdf(context, records, title, chat_id)
        
        if status_msg:
            try: await status_msg.delete()
            except: pass
        
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        if status_msg:
            try: await status_msg.edit_text(f"❌ એરર: {str(e)[:100]}")
            except: pass


# ==================== HELPER BATCH GENERATOR (SENDS TO CHANNEL) ====================
async def generate_batch_pdf(context: ContextTypes.DEFAULT_TYPE, records, title: str, chat_id: int):
    """Generates PDF and saves it to Private Channel instead of keeping it in RAM"""
    try:
        summary = ""
        text_items = [rec for rec in records if rec['type'] == 'text']
        if text_items:
            all_text = ' '.join([rec['content'][:100] for rec in text_items[:3]])
            if all_text: summary = f"{all_text[:150]}..."

        photo_records = [(idx, r) for idx, r in enumerate(records) if r['type'] == 'photo']
        downloaded_images = {}
        
        if photo_records:
            download_tasks = [fetch_image_fast(context.bot, rec['content'], idx) for idx, rec in photo_records]
            try:
                results = await asyncio.wait_for(asyncio.gather(*download_tasks), timeout=40)
                for result in results:
                    idx, path, size = result
                    if path: downloaded_images[idx] = path
            except asyncio.TimeoutError:
                pass

        record_ids = [rec['id'] for rec in records]
        loop = asyncio.get_event_loop()
        
        # Generate without timeout (Ignores Render's 60s kill signal)
        pdf_buffer = await loop.run_in_executor(
            executor, generate_smart_pdf, records, downloaded_images, title, summary, record_ids
        )
        
        # --- SEND TO PRIVATE CHANNEL ---
        filename = f"{title.replace(' ', '_')}_{int(time.time())}.pdf"
        
        if CHANNEL_ID:
            await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=BytesIO(pdf_buffer.getvalue()),
                filename=filename,
                caption=f"📄 **{title}**\nGenerated for user: {chat_id}"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ **PDF Saved to Channel!**\n\nCheck your Private Channel to download it.\nFilename: `{filename}`",
                parse_mode="Markdown"
            )
        else:
            # Fallback if no channel
            await context.bot.send_document(
                chat_id=chat_id,
                document=BytesIO(pdf_buffer.getvalue()),
                filename=filename,
                caption=f"📄 **{title}** Ready!"
            )
        
        # Clean up temp images
        for path in downloaded_images.values():
            if path and os.path.exists(path):
                try: os.remove(path)
                except: pass
                
    except Exception as e:
        logger.error(f"Batch PDF error: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Batch error: {str(e)[:50]}")

# ==================== KEYBOARDS & START COMMAND ====================
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📅 Daily PDF"), KeyboardButton("📊 Weekly PDF")],
        [KeyboardButton("📈 Monthly PDF"), KeyboardButton("🏷️ Topic PDF")],
        [KeyboardButton("📅 By Date"), KeyboardButton("📅 Date Range")],
        [KeyboardButton("📁 My Data"), KeyboardButton("📋 All Topics")],
        [KeyboardButton("⚡ Instant Report"), KeyboardButton("🔍 Search")],
        [KeyboardButton("📊 Stats"), KeyboardButton("🗑️ Clear Cache")],
        [KeyboardButton("❓ Help"), KeyboardButton("📚 Menu")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📅 Daily PDF", callback_data="menu_daily"), InlineKeyboardButton("📊 Weekly PDF", callback_data="menu_weekly")],
        [InlineKeyboardButton("📈 Monthly PDF", callback_data="menu_monthly"), InlineKeyboardButton("🏷️ Topic PDF", callback_data="menu_topic")],
        [InlineKeyboardButton("📅 By Date", callback_data="menu_date"), InlineKeyboardButton("📅 Date Range", callback_data="menu_daterange")],
        [InlineKeyboardButton("📁 My Data", callback_data="menu_my_data"), InlineKeyboardButton("📋 All Topics", callback_data="menu_all_topics")],
        [InlineKeyboardButton("⚡ Instant Report", callback_data="menu_instant"), InlineKeyboardButton("🔍 Search", callback_data="menu_search")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats"), InlineKeyboardButton("🗑️ Clear Cache", callback_data="menu_clear_cache")],
        [InlineKeyboardButton("❓ Help", callback_data="menu_help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        "📚 *Bittu Study Notes Bot*\n\n"
        "📸 **Save Content:**\n"
        "Send screenshots or text, then reply with `#topic`.\n\n"
        "📊 **Generate PDFs:**\n"
        "Use buttons below or type commands:\n"
        "/daily_pdf, /weekly_pdf, /monthly_pdf, /topic_pdf\n"
        "/date_pdf, /daterange\n\n"
        "✨ **PDF Features:**\n"
        "• 🔍 Maximum image zoom\n"
        "• 📐 Minimal borders (5px)\n"
        "• 📄 Multiple images per page\n"
        "• 🚫 No wasted space\n\n"
        "👇 *Use menu below or type commands:*"
    )
    
    # Safe Markdown fallback
    try:
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
            await update.message.reply_text("💡 *Quick Actions:* Use the buttons below or type commands like /start", parse_mode="Markdown", reply_markup=get_main_keyboard())
        elif update.callback_query:
            await update.callback_query.message.edit_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
            await update.callback_query.message.reply_text("💡 *Quick Actions:* Use the buttons below or type commands like /start", parse_mode="Markdown", reply_markup=get_main_keyboard())
    except Exception:
        # Plain text fallback if Markdown fails
        if update.message:
            await update.message.reply_text(msg, reply_markup=reply_markup)
            await update.message.reply_text("Quick Actions: Use the buttons below", reply_markup=get_main_keyboard())
        elif update.callback_query:
            await update.callback_query.message.edit_text(msg, reply_markup=reply_markup)
            await update.callback_query.message.reply_text("Quick Actions: Use the buttons below", reply_markup=get_main_keyboard())

async def handle_type_area_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📅 Daily PDF": await daily_pdf(update, context)
    elif text == "📊 Weekly PDF": await weekly_pdf(update, context)
    elif text == "📈 Monthly PDF": await monthly_pdf(update, context)
    elif text == "🏷️ Topic PDF": await topic_pdf(update, context)
    elif text == "📅 By Date": await date_pdf_command(update, context)
    elif text == "📅 Date Range": await date_range_pdf_command(update, context)
    elif text == "📁 My Data": await my_data(update, context)
    elif text == "📋 All Topics": await all_topics(update, context)
    elif text == "⚡ Instant Report": await instant_report(update, context)
    elif text == "🔍 Search": await search_notes(update, context)
    elif text == "📊 Stats": await stats_command(update, context)
    elif text == "🗑️ Clear Cache": await clear_cache(update, context)
    elif text == "❓ Help": await help_command(update, context)
    elif text == "📚 Menu": await start(update, context)

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    if 'pending_items' not in context.user_data: context.user_data['pending_items'] = []
    context.user_data['pending_items'].append(('photo', photo_file_id))
    if not context.user_data.get('asked_topic'):
        context.user_data['asked_topic'] = True
        await update.message.reply_text("📌 **Content received!**\nReply with **#topic** (e.g., `#chemistry`):", parse_mode="Markdown")
    return WAITING_FOR_TOPIC

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_content = update.message.text.strip()
    if text_content.startswith("/"): return
    if text_content in ["📅 Daily PDF", "📊 Weekly PDF", "📈 Monthly PDF", "🏷️ Topic PDF", "📅 By Date", "📅 Date Range", "📁 My Data", "📋 All Topics", "⚡ Instant Report", "🔍 Search", "📊 Stats", "🗑️ Clear Cache", "❓ Help", "📚 Menu"]:
        await handle_type_area_buttons(update, context); return
    if 'pending_items' not in context.user_data: context.user_data['pending_items'] = []
    context.user_data['pending_items'].append(('text', text_content))
    if not context.user_data.get('asked_topic'):
        context.user_data['asked_topic'] = True
        await update.message.reply_text("📝 **Text received!**\nReply with **#topic** (e.g., `#chemistry`):", parse_mode="Markdown")
    return WAITING_FOR_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"): topic = f"#{topic}"
    items = context.user_data.get('pending_items', [])
    if not items:
        await update.message.reply_text("❌ Session expired. Please resend.")
        return ConversationHandler.END
    status_msg = await update.message.reply_text(f"💾 {len(items)} item(s) સેવ થઈ રહ્યા છે...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for item_type, content in items:
        cursor.execute("INSERT INTO content_store (type, content, topic) VALUES (?, ?, ?)", (item_type, content, topic))
        if item_type == 'photo' and CHANNEL_ID:
            try:
                caption = f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=content, caption=caption, parse_mode="Markdown")
            except Exception as e: logger.error(f"Failed to post to channel: {e}")
    conn.commit(); conn.close()
    await status_msg.edit_text(f"✅ **{len(items)} item(s) under {topic} સેવ થયા!**", parse_mode="Markdown")
    context.user_data.pop('pending_items', None); context.user_data.pop('asked_topic', None)
    await update.message.reply_text("💡 *Quick Actions:* Use the buttons below or type /start for menu", parse_mode="Markdown", reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_items', None); context.user_data.pop('asked_topic', None)
    await update.message.reply_text("❌ Action canceled.")
    return ConversationHandler.END

# ==================== PDF COMMANDS ====================
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (today, f"%{topic.lower()}%"), f"Daily Notes - #{topic}")
    else:
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (today,), "Daily Notes (All)")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    heading = f"Weekly Report ({datetime.now().strftime('%Y-%m-%d')})"
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (week_ago, f"%{topic.lower()}%"), f"{heading} - #{topic}")
    else:
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (week_ago,), heading)

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (month_ago, f"%{topic.lower()}%"), f"Monthly Notes - #{topic}")
    else:
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (month_ago,), "Monthly Notes (All)")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        topics = get_all_topics()
        if topics:
            keyboard = [[InlineKeyboardButton(f"📌 {topic}", callback_data=f"topic_select_{topic}")] for topic in topics[:15]]
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
            await update.message.reply_text("🏷️ *Select a topic:*\n\nOr use `/topic_pdf topic_name`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        else: await update.message.reply_text("⚠️ No topics found. Add some data first!")
        return
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf_with_summary(update, context, sql, (f"%{user_topic.lower()}%",), f"Topic PDF - #{user_topic}")

async def date_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        dates = get_available_dates()
        if dates:
            keyboard = [[InlineKeyboardButton(f"📅 {date}", callback_data=f"date_select_{date}")] for date in dates[:15]]
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="menu_back")])
            await update.message.reply_text("📅 *Select a date:*\n\nOr use `/date_pdf YYYY-MM-DD`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        else: await update.message.reply_text("⚠️ No dates found. Add some data first!")
        return
    date_str = context.args[0].strip()
    try: datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError: await update.message.reply_text("⚠️ Invalid date format. Use: `/date_pdf 2026-07-29`"); return
    date_query = date_str + "%"
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf_with_summary(update, context, sql, (date_query,), f"Study Notes - {date_str}")

async def date_range_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("📅 *Date Range PDF*\n\nUsage: `/daterange YYYY-MM-DD YYYY-MM-DD`\nExample: `/daterange 2026-07-01 2026-07-29`"); return
    start_date = context.args[0].strip(); end_date = context.args[1].strip()
    try: datetime.strptime(start_date, "%Y-%m-%d"); datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError: await update.message.reply_text("⚠️ Invalid date format. Use: `/daterange 2026-07-01 2026-07-29`"); return
    if start_date > end_date: await update.message.reply_text("⚠️ Start date must be before end date!"); return
    start_query = start_date + " 00:00:00"; end_query = end_date + " 23:59:59"
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC"
    await generate_and_send_pdf_with_summary(update, context, sql, (start_query, end_query), f"Study Notes - {start_date} to {end_date}")

async def clear_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pdf_cache.clear(); await update.message.reply_text("🗑️ Cache cleared successfully!")

async def all_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = get_all_topics()
    if not topics: await update.message.reply_text("No topics found."); return
    msg = "📋 *All Topics:*\n\n"
    for topic in topics:
        conn = sqlite3.connect(DB_FILE); cursor = conn.cursor(); cursor.execute("SELECT COUNT(*) FROM content_store WHERE topic = ?", (topic,)); count = cursor.fetchone()[0]; conn.close()
        msg += f"• `{topic}` ({count} items)\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT id, type, topic, timestamp FROM content_store ORDER BY timestamp DESC LIMIT 10"); records = cursor.fetchall(); conn.close()
    if not records: await update.message.reply_text("No data found."); return
    msg = "📁 *Your Recent Entries:*\n\n"
    for rec in records: msg += f"• #{rec[0]} | {rec[1]} | {rec[2]} | {rec[3][:16]}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def instant_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf_with_summary(update
