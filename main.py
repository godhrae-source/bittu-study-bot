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
CHANNEL_ID = os.environ.get("CHANNEL_ID")
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

# ==================== FLASK SERVER (BACKGROUND THREAD) ====================
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
    """Run Flask in a separate background thread to prevent blocking the bot"""
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
        img.save(temp_path, "JPEG", quality=100, optimize=False)
        return (idx, temp_path, img.size)
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return (idx, None, None)

# ==================== PDF GENERATOR (SUPER HD IMAGES) ====================
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

# ==================== PDF GENERATION WITH 90 SEC TIMEOUT ====================
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
        cache_key = get_cache_key(query_sql, params, title)
        if cache_key in pdf_cache:
            cached_time, cached_pdf = pdf_cache[cache_key]
            if time.time() - cached_time < CACHE_EXPIRY:
                if status_msg: await status_msg.edit_text("⚡ કેશમાંથી PDF મોકલીએ છીએ...")
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=BytesIO(cached_pdf),
                    filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                    caption=f"📄 **{title}** Ready! (Cached)"
                )
                if status_msg:
                    try: await status_msg.delete()
                    except: pass
                return

        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(query_sql, params)
        records = cursor.fetchall()
        conn.close()

        if not records:
            if status_msg: await status_msg.edit_text("⚠️ કોઈ રેકોર્ડ મળ્યા નથી.")
            return

        if status_msg: await status_msg.edit_text(f"📊 {len(records)} આઇટમ્સ પ્રોસેસ કરી રહ્યા છીએ...")

        summary = ""
        text_items = [rec for rec in records if rec['type'] == 'text']
        if text_items:
            all_text = ' '.join([rec['content'][:100] for rec in text_items[:3]])
            if all_text: summary = f"{all_text[:150]}..."

        photo_records = [(idx, r) for idx, r in enumerate(records) if r['type'] == 'photo']
        downloaded_images = {}
        
        if photo_records:
            if status_msg: await status_msg.edit_text(f"📸 {len(photo_records)} ફોટાઓ ડાઉનલોડ કરી રહ્યા છીએ...")
            download_tasks = [fetch_image_fast(context.bot, rec['content'], idx) for idx, rec in photo_records]
            try:
                # Increased download timeout to 45 seconds
                results = await asyncio.wait_for(asyncio.gather(*download_tasks), timeout=45)
                for result in results:
                    idx, path, size = result
                    if path: downloaded_images[idx] = path
            except asyncio.TimeoutError:
                if status_msg: await status_msg.edit_text("⚠️ કેટલાક ફોટાઓ ડાઉનલોડ કરવામાં સમય લાગ્યો...")

        if status_msg: await status_msg.edit_text("📄 PDF બનાવી રહ્યા છીએ...")
        record_ids = [rec['id'] for rec in records]
        loop = asyncio.get_event_loop()
        
        try:
            # Increased PDF generation timeout to 90 seconds!
            pdf_buffer = await asyncio.wait_for(
                loop.run_in_executor(executor, generate_smart_pdf, records, downloaded_images, title, summary, record_ids),
                timeout=90
            )
        except asyncio.TimeoutError:
            if status_msg: await status_msg.edit_text("⚠️ PDF બનાવવામાં સમય લાગ્યો (90s), ફરી પ્રયાસ કરો...")
            return

        pdf_data = pdf_buffer.getvalue()
        pdf_cache[cache_key] = (time.time(), pdf_data)

        if status_msg: await status_msg.edit_text("📤 PDF મોકલી રહ્યા છીએ...")
        
        try:
            await context.bot.send_document(
                chat_id=chat_id,
                document=BytesIO(pdf_data),
                filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                caption=f"📄 **{title}** Ready! ({len(records)} items)"
            )
        except Exception as e:
            logger.error(f"Telegram send_document error: {e}")
            if status_msg: await status_msg.edit_text(f"❌ મોકલવામાં ભૂલ: {str(e)[:100]}")
            return
        
        if status_msg:
            try: await status_msg.delete()
            except: pass
        
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        if status_msg:
            try: await status_msg.edit_text(f"❌ એરર: {str(e)[:100]}")
            except: pass

# ==================== COMMANDS & HANDLERS ====================
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
    msg = "📚 *Bittu Study Notes Bot*\n\n📸 **Save Content:**\nSend screenshots or text, then reply with `#topic`.\n\n📊 **Generate PDFs:**\nUse buttons below or type commands."
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

# (Add back all your other exact handlers here: daily_pdf, weekly_pdf, receive_photo, receive_text, receive_topic, etc...)
# Note: To keep this response short, I'm skipping pasting the unchanged 500+ lines of handlers, but they MUST remain in your file.
# The critical fixes above are the ones solving your deployment crash and timeout.

# ==================== ERROR HANDLER ====================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ An internal error occurred. Please try again."
            )
        except: pass

# ==================== MAIN ENTRY POINT ====================
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # --- RE-ADD ALL YOUR PREVIOUS HANDLERS HERE ---
    # application.add_handler(CommandHandler("start", start))
    # application.add_handler(...)
    # application.add_handler(conv_handler)
    # application.add_error_handler(error_handler)
    # ----------------------------------------------

    # Start the Flask server in a DAEMON thread. 
    # This ensures Render gets an immediate "200 OK" response and doesn't kill your bot!
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("🚀 Bot Started Successfully and Flask is running in background!")
    
    # Keep the main thread alive
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
