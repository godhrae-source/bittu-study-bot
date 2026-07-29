import os
import asyncio
import logging
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image

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

WAITING_FOR_TOPIC = 1
DB_FILE = "study_data.db"

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
    conn.commit()
    conn.close()

init_db()

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

# ==================== FLASK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bittu Study Bot is Live!"

@app.route('/health')
def health_check():
    return jsonify({'status': 'alive', 'timestamp': datetime.now().isoformat()})

def run_flask():
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", PORT, app)
    server.serve_forever()

# ==================== FAST IMAGE DOWNLOAD ====================
async def fetch_single_image(bot, file_id):
    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
        temp_path = f"temp_{int(time.time())}.jpg"
        img.save(temp_path, "JPEG", quality=80, optimize=True)
        return temp_path
    except Exception as e:
        logger.error(f"Image error: {e}")
        return None

# ==================== ULTRA-FAST SINGLE-THREAD PDF ====================
def generate_fast_pdf(records, downloaded_images, title):
    pdf_buffer = BytesIO()
    margin = 10
    page_width, page_height = A4
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    y_position = page_height - margin
    
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(page_width/2, y_position - 10, title[:40])
    c.setFont("Helvetica", 9)
    c.drawCentredString(page_width/2, y_position - 25, f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    y_position -= 40
    
    for idx, rec in enumerate(records):
        if y_position < 50:
            c.showPage()
            y_position = page_height - margin
        
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin, y_position, f"📌 {rec['topic']}")
        y_position -= 18
        
        if rec['type'] == 'photo' and idx in downloaded_images:
            img_path = downloaded_images[idx]
            if img_path and os.path.exists(img_path):
                try:
                    img_reader = ImageReader(img_path)
                    orig_w, orig_h = img_reader.getSize()
                    avail_w = page_width - (2 * margin)
                    avail_h = y_position - margin
                    scale = min(avail_w / orig_w, avail_h / orig_h, 0.7)
                    final_w = orig_w * scale
                    final_h = orig_h * scale
                    x_pos = (page_width - final_w) / 2
                    c.drawImage(img_reader, x_pos, y_position - final_h, width=final_w, height=final_h, mask='auto')
                    y_position -= (final_h + 15)
                except:
                    y_position -= 20
            else:
                y_position -= 20
                
        elif rec['type'] == 'text':
            c.setFont("Helvetica", 9)
            text_content = rec['content'][:150] + "..." if len(rec['content']) > 150 else rec['content']
            c.drawString(margin, y_position - 10, text_content)
            y_position -= 30
        
        y_position -= 15
    
    c.save()
    pdf_buffer.seek(0)
    return pdf_buffer

# ==================== CRASH-PROOF GENERATOR (WORKS FOR HUGE PDFS) ====================
async def generate_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                            query_sql: str, params: tuple, title: str):
    chat_id = update.effective_chat.id
    
    try:
        # STEP 1: Get records safely
        conn = sqlite3.connect(DB_FILE, timeout=5)
        cursor = conn.cursor()
        cursor.execute(query_sql, params)
        records = cursor.fetchall()
        conn.close()

        if not records:
            await update.message.reply_text("⚠️ No records found.")
            return

        # STEP 2: Determine if this is a huge file and warn the user
        is_large_pdf = len(records) > 10
        
        if is_large_pdf:
            status_msg = await update.message.reply_text(
                f"⚠️ *{len(records)} items detected!*\n\n"
                f"This will take **up to 2 minutes**.\n"
                f"Please **do not press anything** until I reply!\n"
                f"I am working in the background...",
                parse_mode="Markdown"
            )
        else:
            status_msg = await update.message.reply_text(f"⏳ Generating `{title}`... Please wait 20 seconds.")

        # STEP 3: Download images ONE BY ONE (Saves RAM)
        downloaded_images = {}
        for idx, rec in enumerate(records):
            if rec[1] == 'photo':
                path = await fetch_single_image(context.bot, rec[2])
                if path:
                    downloaded_images[idx] = path

        # STEP 4: Generate PDF in the SAME THREAD
        if is_large_pdf:
            await status_msg.edit_text("📄 Generating **50+ pages**. This is the longest step. Please wait...")
        else:
            await status_msg.edit_text("📄 Generating PDF file...")
        
        pdf_buffer = generate_fast_pdf(records, downloaded_images, title)

        # STEP 5: Send to Channel
        filename = f"{title.replace(' ', '_')}_{int(time.time())}.pdf"
        if CHANNEL_ID:
            await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=BytesIO(pdf_buffer.getvalue()),
                filename=filename,
                caption=f"📄 **{title}**"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ **PDF Saved to Channel!**\n\nCheck your Private Channel to download it.\nFilename: `{filename}`",
                parse_mode="Markdown"
            )
        else:
            await context.bot.send_document(
                chat_id=chat_id,
                document=BytesIO(pdf_buffer.getvalue()),
                filename=filename,
                caption=f"📄 **{title}** Ready!"
            )
        
        await status_msg.delete()
        
        # Cleanup
        for path in downloaded_images.values():
            if path and os.path.exists(path):
                try: os.remove(path)
                except: pass
                
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:50]}")

# ==================== COMMANDS ====================
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
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

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
    await update.message.reply_text(
        "📚 *Bittu Study Bot*\n\nSave photos/text and generate PDFs!\n\nUse the buttons below:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    await update.message.reply_text("💡 Quick Actions:", reply_markup=get_main_keyboard())

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    context.user_data['pending'] = ('photo', update.message.photo[-1].file_id)
    await update.message.reply_text("📸 Photo received! Reply with **#topic**", parse_mode="Markdown")
    return WAITING_FOR_TOPIC

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.startswith("/"): return
    if update.message.text in ["📅 Daily PDF", "📊 Weekly PDF", "📈 Monthly PDF", "🏷️ Topic PDF", "📅 By Date", "📅 Date Range", "📁 My Data", "📋 All Topics", "⚡ Instant Report", "🔍 Search", "📊 Stats", "🗑️ Clear Cache", "❓ Help", "📚 Menu"]:
        await handle_buttons(update, context); return
    context.user_data['pending'] = ('text', update.message.text)
    await update.message.reply_text("📝 Text received! Reply with **#topic**", parse_mode="Markdown")
    return WAITING_FOR_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"): topic = f"#{topic}"
    item = context.user_data.get('pending')
    if not item:
        await update.message.reply_text("❌ Session expired.")
        return ConversationHandler.END
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT INTO content_store (type, content, topic) VALUES (?, ?, ?)", (item[0], item[1], topic))
    conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Saved under {topic}!")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ==================== PDF FUNCTIONS ====================
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
    await generate_and_send(update, context, sql, (today,), "Daily Report")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
    await generate_and_send(update, context, sql, (week_ago,), "Weekly Report")

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
    await generate_and_send(update, context, sql, (month_ago,), "Monthly Report")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /topic_pdf [topic]"); return
    topic = context.args[0].strip()
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE topic LIKE ? ORDER BY timestamp ASC"
    await generate_and_send(update, context, sql, (f"%{topic}%",), f"Topic: {topic}")

async def date_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /date_pdf YYYY-MM-DD"); return
    date_str = context.args[0].strip()
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
    await generate_and_send(update, context, sql, (f"{date_str}%",), f"Date: {date_str}")

async def date_range_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /daterange YYYY-MM-DD YYYY-MM-DD"); return
    start, end = context.args[0], context.args[1]
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp ASC"
    await generate_and_send(update, context, sql, (f"{start}%", f"{end}%"), f"Range: {start} to {end}")

async def all_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = get_all_topics()
    if not topics: await update.message.reply_text("No topics found.")
    else: await update.message.reply_text(f"Topics: {', '.join(topics)}")

async def my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT id, type, topic, timestamp FROM content_store ORDER BY timestamp DESC LIMIT 10"); records = cursor.fetchall(); conn.close()
    if not records: await update.message.reply_text("No data.")
    else: await update.message.reply_text("\n".join([f"#{r[0]} | {r[1]} | {r[2]}" for r in records]))

async def instant_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await daily_pdf(update, context)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM content_store"); total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT topic) FROM content_store"); topics = cursor.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"📊 Stats:\nTotal Notes: {total}\nTopics: {topics}")

async def search_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search query"); return
    query = ' '.join(context.args).lower()
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    cursor.execute("SELECT content, topic FROM content_store WHERE LOWER(content) LIKE ? LIMIT 5", (f'%{query}%',))
    results = cursor.fetchall(); conn.close()
    if not results: await update.message.reply_text("No results.")
    else: await update.message.reply_text("\n\n".join([f"*{r[1]}*:\n{r[0][:100]}..." for r in results]), parse_mode="Markdown")

async def clear_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cache cleared.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use buttons or commands:\n/daily_pdf, /weekly_pdf, /monthly_pdf, /topic_pdf, /date_pdf, /daterange")

# ==================== CALLBACKS ====================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "menu_daily": await daily_pdf(update, context)
    elif data == "menu_weekly": await weekly_pdf(update, context)
    elif data == "menu_monthly": await monthly_pdf(update, context)
    elif data == "menu_topic": await topic_pdf(update, context)
    elif data == "menu_date": await date_pdf_command(update, context)
    elif data == "menu_daterange": await date_range_pdf_command(update, context)
    elif data == "menu_my_data": await my_data(update, context)
    elif data == "menu_all_topics": await all_topics(update, context)
    elif data == "menu_instant": await instant_report(update, context)
    elif data == "menu_search": await search_notes(update, context)
    elif data == "menu_stats": await stats_command(update, context)
    elif data == "menu_clear_cache": await clear_cache(update, context)
    elif data == "menu_help": await help_command(update, context)

# ==================== MAIN ====================
async def main():
    if not BOT_TOKEN:
        logger.error("Missing BOT_TOKEN")
        return

    app_bot = Application.builder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, receive_photo), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
        states={WAITING_FOR_TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("daily_pdf", daily_pdf))
    app_bot.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    app_bot.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    app_bot.add_handler(CommandHandler("topic_pdf", topic_pdf))
    app_bot.add_handler(CommandHandler("date_pdf", date_pdf_command))
    app_bot.add_handler(CommandHandler("daterange", date_range_pdf_command))
    app_bot.add_handler(CallbackQueryHandler(callback_handler))
    app_bot.add_handler(conv)
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    await app_bot.initialize()
    await app_bot.start()
    await app_bot.updater.start_polling()
    
    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
