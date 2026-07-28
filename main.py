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
WAITING_FOR_CUSTOM_TOPIC = 2

# Database File
DB_FILE = "study_data.db"

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
    keyboard = [
        [InlineKeyboardButton("📅 Daily PDF", callback_data="menu_daily"), InlineKeyboardButton("📊 Weekly PDF", callback_data="menu_weekly")],
        [InlineKeyboardButton("📈 Monthly PDF", callback_data="menu_monthly"), InlineKeyboardButton("🏷️ Topic PDF", callback_data="menu_topic")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        "📚 *Bittu Study Notes & Text Bot*\n\n"
        "📸 **How to save notes/text:**\n"
        "Send screenshots or text messages, and reply with `#topic`.\n\n"
        "👇 *Or use the menu below to generate PDFs instantly:*"
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
            "📌 **Content received!**\nPlease reply with the **#topic** (e.g., `#21`, `#chemistry`):",
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
            "📝 **Text received!**\nPlease reply with the **#topic** (e.g., `#21`, `#chemistry`):",
            parse_mode="Markdown"
        )
    return WAITING_FOR_TOPIC

async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    if not topic.startswith("#"):
        topic = f"#{topic}"
        
    items = context.user_data.get('pending_items', [])
    if not items:
        await update.message.reply_text("❌ Session expired. Please resend your items.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text(f"💾 Saving {len(items)} item(s)...")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for item_type, content in items:
        cursor.execute("INSERT INTO content_store (type, content, topic) VALUES (?, ?, ?)", (item_type, content, topic))
        if item_type == 'photo' and CHANNEL_ID:
            try:
                caption = f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=content, caption=caption, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to post to channel: {e}")
    conn.commit()
    conn.close()

    await status_msg.edit_text(f"✅ **Saved {len(items)} item(s) under {topic}!**", parse_mode="Markdown")
    
    context.user_data.pop('pending_items', None)
    context.user_data.pop('asked_topic', None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_items', None)
    context.user_data.pop('asked_topic', None)
    await update.message.reply_text("Action canceled.")
    return ConversationHandler.END

# ----------------- Interactive Menu Handler -----------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_daily":
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (today,), "Daily Study Notes (All Topics)")
    elif data == "menu_weekly":
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        week_heading = f"Weekly Report ({ (datetime.now() - timedelta(days=7)).strftime('%d %b %Y') } to { datetime.now().strftime('%d %b %Y') })"
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago,), week_heading)
    elif data == "menu_monthly":
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Study Notes (All Topics)")
    elif data == "menu_topic":
        await query.message.reply_text("🏷️ Please reply with the topic you want (e.g., `/topic_pdf 21` or type `/topics` to see available list).")

# ----------------- Fast Multi-Image / Text PDF Engine -----------------

async def fetch_image(bot, file_id, idx):
    try:
        tg_file = await bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()
        img = Image.open(BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
        temp_path = f"temp_{idx}.jpg"
        img.save(temp_path, "JPEG", quality=85, optimize=True)
        return idx, temp_path
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None

async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, query_sql: str, params: tuple, title: str):
    message_to_edit = update.message if update.message else update.callback_query.message
    await message_to_edit.reply_text("⏳ Compiling smart PDF... Please wait.")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await message_to_edit.reply_text("⚠️ No records found for this selection.")
        return

    # Download images concurrently
    photo_records = [(r, idx) for idx, r in enumerate(records) if r[1] == 'photo']
    download_tasks = [fetch_image(context.bot, r[2], idx) for r, idx in photo_records]
    downloaded_images = await asyncio.gather(*download_tasks)
    img_map = {idx: path for idx, path in downloaded_images if path}

    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_w, page_h = A4

    i = 0
    total_records = len(records)
    page_num = 1

    while i < total_records:
        # Header Banner
        c.setFillColor(colors.HexColor("#2C3E50"))
        c.rect(0, page_h - 40, page_w, 40, fill=True, stroke=False)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20, page_h - 25, f"Title: {safe_str(title)}")

        # Check if current and next record are small enough to pack 2 on one page
        current_rec = records[i]
        current_type = current_rec[1]

        # Try 2-up packing if both are photos or short text
        next_rec = records[i + 1] if i + 1 < total_records else None
        
        can_pack_two = False
        if next_rec and current_type == 'photo' and next_rec[1] == 'photo':
            if i in img_map and (i + 1) in img_map:
                can_pack_two = True

        if can_pack_two:
            # --- TWO ITEMS ON ONE PAGE ---
            items_to_draw = [(records[i], img_map[i]), (records[i+1], img_map[i+1])]
            y_offsets = [page_h - 50, (page_h / 2) - 30]

            for slot_idx, (rec, temp_path) in enumerate(items_to_draw):
                y_base = y_offsets[slot_idx]
                topic_tag, ts_str = rec[3], rec[4]

                c.setFillColor(colors.HexColor("#34495E"))
                c.setFont("Helvetica-Bold", 9)
                c.drawString(20, y_base - 12, f"Topic: {safe_str(topic_tag)} | Date: {ts_str[:16]} (#{rec[0]})")

                img = Image.open(temp_path)
                orig_w, orig_h = img.size
                aspect = orig_w / float(orig_h)

                max_w = page_w - 40
                max_h = (page_h / 2) - 60

                if max_w / max_h < aspect:
                    draw_w = max_w
                    draw_h = max_w / aspect
                else:
                    draw_h = max_h
                    draw_w = max_h * aspect

                x = (page_w - draw_w) / 2
                y_img = y_base - 20 - draw_h
                c.drawImage(temp_path, x, y_img, width=draw_w, height=draw_h)

            i += 2
        else:
            # --- SINGLE FULL PAGE ITEM ---
            rec = current_rec
            topic_tag, ts_str = rec[3], rec[4]
            c.setFillColor(colors.HexColor("#34495E"))
            c.setFont("Helvetica-Bold", 10)
            c.drawString(20, page_h - 60, f"Topic: {safe_str(topic_tag)} | Date: {ts_str[:16]} (#{rec[0]})")

            if current_type == 'photo' and i in img_map:
                temp_path = img_map[i]
                img = Image.open(temp_path)
                orig_w, orig_h = img.size
                aspect = orig_w / float(orig_h)

                max_w = page_w - 40
                max_h = page_h - 100

                if max_w / max_h < aspect:
                    draw_w = max_w
                    draw_h = max_w / aspect
                else:
                    draw_h = max_h
                    draw_w = max_h * aspect

                x = (page_w - draw_w) / 2
                y_img = (page_h - 75 - draw_h) / 2
                c.drawImage(temp_path, x, y_img, width=draw_w, height=draw_h)
            elif current_type == 'text':
                c.setFillColor(colors.black)
                c.setFont("Helvetica", 12)
                text_content = safe_str(rec[2])
                c.drawString(30, page_h - 120, text_content[:100]) # Quick render for text block

            i += 1

        # Footer
        c.setFillColor(colors.HexColor("#7F8C8D"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 20, 15, f"Page {page_num}")
        page_num += 1

        c.showPage()

    # Cleanup temp paths
    for path in img_map.values():
        if os.path.exists(path):
            os.remove(path)

    c.save()
    pdf_buffer.seek(0)

    clean_filename = f"Study_Report_{datetime.now().strftime('%Y%m%d')}.pdf"
    chat_id = update.effective_chat.id
    await context.bot.send_document(
        chat_id=chat_id,
        document=pdf_buffer,
        filename=clean_filename,
        caption=f"📄 **{title}** Ready!"
    )

# PDF Command Handlers (With & Without Topic)
async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        await generate_and_send_pdf(update, context, sql, (today, f"%{topic.lower()}%"), f"Daily Notes - #{topic}")
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
        await generate_and_send_pdf(update, context, sql, (week_ago, f"%{topic.lower()}%"), f"{heading} - #{topic}")
    else:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago,), heading)

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago, f"%{topic.lower()}%"), f"Monthly Notes - #{topic}")
    else:
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Notes (All)")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Specify topic! Example: `/topic_pdf 21`", parse_mode="Markdown")
        return
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf(update, context, sql, (f"%{user_topic.lower()}%",), f"Topic PDF - #{user_topic}")

# ----------------- Main Execution Block -----------------

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, receive_photo),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)
        ],
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
    application.add_handler(CallbackQueryHandler(button_handler))
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
