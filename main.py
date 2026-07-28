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
from reportlab.lib.utils import simpleSplit

from pypdf import PdfReader, PdfWriter

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


def get_recent_topics(limit=8):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT topic, MAX(timestamp) as last_used, COUNT(*) as cnt
        FROM content_store
        GROUP BY topic
        ORDER BY last_used DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_all_topics():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT topic FROM content_store ORDER BY topic ASC")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


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
        [InlineKeyboardButton("📅 Daily PDF", callback_data="menu_daily"),
         InlineKeyboardButton("📊 Weekly PDF", callback_data="menu_weekly")],
        [InlineKeyboardButton("📈 Monthly PDF", callback_data="menu_monthly"),
         InlineKeyboardButton("🏷️ Topic PDF", callback_data="menu_topic")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "📚 *Bittu Study Notes & Text Bot*\n\n"
        "📸 **How to save notes/text/PDF:**\n"
        "Send screenshots, text messages, or PDF files, then pick a topic "
        "from the buttons (or add a new one).\n\n"
        "👇 *Or use the menu below to generate PDFs instantly:*"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode="Markdown", reply_markup=reply_markup)


def build_topic_keyboard():
    topics = get_recent_topics(8)
    rows = []
    row = []
    for t in topics:
        row.append(InlineKeyboardButton(t, callback_data=f"settopic|{t}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("➕ New Topic", callback_data="newtopic")])
    return InlineKeyboardMarkup(rows)


async def _ask_topic_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE, label: str):
    if not context.user_data.get('asked_topic'):
        context.user_data['asked_topic'] = True
        await update.message.reply_text(
            f"📌 **{label} received!**\nPick a topic below, or add a new one:",
            parse_mode="Markdown",
            reply_markup=build_topic_keyboard(),
        )
    return WAITING_FOR_TOPIC


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    context.user_data.setdefault('pending_items', []).append(('photo', photo_file_id))
    return await _ask_topic_if_needed(update, context, "Screenshot")


async def receive_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or (doc.mime_type != "application/pdf" and not doc.file_name.lower().endswith(".pdf")):
        return  # not a pdf, ignore silently
    context.user_data.setdefault('pending_items', []).append(('pdf', doc.file_id))
    return await _ask_topic_if_needed(update, context, "PDF file")


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_content = update.message.text.strip()
    if text_content.startswith("/"):
        return

    context.user_data.setdefault('pending_items', []).append(('text', text_content))
    return await _ask_topic_if_needed(update, context, "Text")


async def save_pending_items(update_or_query, context: ContextTypes.DEFAULT_TYPE, topic: str, chat_id: int):
    if not topic.startswith("#"):
        topic = f"#{topic}"

    items = context.user_data.get('pending_items', [])
    if not items:
        return 0

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for item_type, content in items:
        cursor.execute(
            "INSERT INTO content_store (type, content, topic) VALUES (?, ?, ?)",
            (item_type, content, topic),
        )
        if CHANNEL_ID:
            try:
                caption = f"📝 **Topic:** {topic}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                if item_type == 'photo':
                    await context.bot.send_photo(chat_id=CHANNEL_ID, photo=content, caption=caption, parse_mode="Markdown")
                elif item_type == 'pdf':
                    await context.bot.send_document(chat_id=CHANNEL_ID, document=content, caption=caption, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to post to channel: {e}")
    conn.commit()
    conn.close()

    context.user_data.pop('pending_items', None)
    context.user_data.pop('asked_topic', None)
    return len(items)


async def receive_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback: user typed the topic as text instead of using buttons."""
    topic = update.message.text.strip()
    if not context.user_data.get('pending_items'):
        await update.message.reply_text("❌ Session expired. Please resend your items.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("💾 Saving...")
    count = await save_pending_items(update, context, topic, update.effective_chat.id)
    await status_msg.edit_text(f"✅ **Saved {count} item(s) under #{topic.lstrip('#')}!**", parse_mode="Markdown")
    return ConversationHandler.END


async def topic_button_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "newtopic":
        await query.message.reply_text(
            "✏️ Type the new topic name (e.g. `chemistry`, `21`):", parse_mode="Markdown"
        )
        return WAITING_FOR_CUSTOM_TOPIC

    topic = query.data.split("|", 1)[1]
    if not context.user_data.get('pending_items'):
        await query.message.reply_text("❌ Session expired. Please resend your items.")
        return ConversationHandler.END

    count = await save_pending_items(update, context, topic, query.message.chat_id)
    await query.message.reply_text(f"✅ **Saved {count} item(s) under {topic if topic.startswith('#') else '#' + topic}!**",
                                    parse_mode="Markdown")
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
        week_heading = (f"Weekly Report ({(datetime.now() - timedelta(days=7)).strftime('%d %b %Y')} "
                         f"to {datetime.now().strftime('%d %b %Y')})")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago,), week_heading)

    elif data == "menu_monthly":
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Study Notes (All Topics)")

    elif data == "menu_topic":
        topics = get_all_topics()
        if not topics:
            await query.message.reply_text("⚠️ No topics saved yet.")
            return
        rows = []
        row = []
        for t in topics[:20]:
            row.append(InlineKeyboardButton(t, callback_data=f"topicmenu|{t}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await query.message.reply_text("🏷️ Choose a topic:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("topicmenu|"):
        topic = data.split("|", 1)[1]
        rows = [
            [InlineKeyboardButton("📅 Daily", callback_data=f"topicperiod|{topic}|daily"),
             InlineKeyboardButton("📊 Weekly", callback_data=f"topicperiod|{topic}|weekly")],
            [InlineKeyboardButton("📈 Monthly", callback_data=f"topicperiod|{topic}|monthly"),
             InlineKeyboardButton("📁 All Time", callback_data=f"topicperiod|{topic}|all")],
        ]
        await query.message.reply_text(f"🏷️ Topic: {topic}\nChoose a period:", reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("topicperiod|"):
        _, topic, period = data.split("|", 2)
        topic_clean = topic.lstrip('#').lower()
        if period == "daily":
            today = datetime.now().strftime("%Y-%m-%d") + "%"
            sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
            await generate_and_send_pdf(update, context, sql, (today, f"%{topic_clean}%"), f"Daily Notes - {topic}")
        elif period == "weekly":
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
            await generate_and_send_pdf(update, context, sql, (week_ago, f"%{topic_clean}%"), f"Weekly Notes - {topic}")
        elif period == "monthly":
            month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
            await generate_and_send_pdf(update, context, sql, (month_ago, f"%{topic_clean}%"), f"Monthly Notes - {topic}")
        else:
            sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
            await generate_and_send_pdf(update, context, sql, (f"%{topic_clean}%",), f"All Notes - {topic}")


# ----------------- Fast Concurrent Fetching -----------------

def _process_image_sync(img_bytes, idx):
    img = Image.open(BytesIO(img_bytes)).convert("RGB")
    img.thumbnail((1200, 1600), Image.Resampling.LANCZOS)
    temp_path = f"temp_{idx}.jpg"
    img.save(temp_path, "JPEG", quality=85, optimize=True)
    return temp_path


async def fetch_image(bot, file_id, idx):
    try:
        tg_file = await bot.get_file(file_id)
        img_bytes = await tg_file.download_as_bytearray()
        temp_path = await asyncio.to_thread(_process_image_sync, bytes(img_bytes), idx)
        return idx, temp_path
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None


async def fetch_pdf_bytes(bot, file_id, idx):
    try:
        tg_file = await bot.get_file(file_id)
        pdf_bytes = await tg_file.download_as_bytearray()
        return idx, bytes(pdf_bytes)
    except Exception as e:
        logger.error(f"Error downloading pdf {file_id}: {e}")
        return idx, None


def draw_wrapped_text(c, text, page_w, page_h, title, topic_tag, ts_str, page_num_start):
    """Draws a text note across as many pages as needed, with header/footer/date on each."""
    margin_x = 30
    top_y = page_h - 90
    bottom_margin = 50
    font_name = "Helvetica"
    font_size = 12
    line_height = 16
    max_width = page_w - 2 * margin_x
    page_num = page_num_start

    def draw_header_footer():
        c.setFillColor(colors.HexColor("#2C3E50"))
        c.rect(0, page_h - 40, page_w, 40, fill=True, stroke=False)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20, page_h - 25, f"Title: {safe_str(title)}")

        c.setFillColor(colors.HexColor("#34495E"))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20, page_h - 60, f"Topic: {safe_str(topic_tag)} | Date: {ts_str[:16]}")

        c.setFillColor(colors.HexColor("#7F8C8D"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 20, 15, f"Page {page_num}")
        c.drawString(20, 15, datetime.now().strftime("%Y-%m-%d %H:%M"))

    draw_header_footer()
    y = top_y

    paragraphs = safe_str(text).replace("\r\n", "\n").split("\n")
    c.setFillColor(colors.black)
    c.setFont(font_name, font_size)

    for para in paragraphs:
        if para.strip() == "":
            y -= line_height
            if y < bottom_margin:
                c.showPage()
                page_num += 1
                draw_header_footer()
                c.setFillColor(colors.black)
                c.setFont(font_name, font_size)
                y = top_y
            continue
        wrapped_lines = simpleSplit(para, font_name, font_size, max_width)
        for line in wrapped_lines:
            if y < bottom_margin:
                c.showPage()
                page_num += 1
                draw_header_footer()
                c.setFillColor(colors.black)
                c.setFont(font_name, font_size)
                y = top_y
            c.drawString(margin_x, y, line)
            y -= line_height

    c.showPage()
    return page_num + 1


async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, query_sql: str, params: tuple, title: str):
    message_to_edit = update.message if update.message else update.callback_query.message
    status = await message_to_edit.reply_text("⏳ Compiling PDF... Please wait.")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query_sql, params)
    records = cursor.fetchall()
    conn.close()

    if not records:
        await status.edit_text("⚠️ No records found for this selection.")
        return

    # Download images and pdfs concurrently
    photo_records = [(idx, r) for idx, r in enumerate(records) if r[1] == 'photo']
    pdf_records = [(idx, r) for idx, r in enumerate(records) if r[1] == 'pdf']

    img_task = asyncio.gather(*[fetch_image(context.bot, r[2], idx) for idx, r in photo_records])
    pdf_task = asyncio.gather(*[fetch_pdf_bytes(context.bot, r[2], idx) for idx, r in pdf_records])
    downloaded_images, downloaded_pdfs = await asyncio.gather(img_task, pdf_task)

    img_map = {idx: path for idx, path in downloaded_images if path}
    pdf_bytes_map = {idx: b for idx, b in downloaded_pdfs if b}

    pdf_buffer = BytesIO()
    c = canvas.Canvas(pdf_buffer, pagesize=A4)
    page_w, page_h = A4

    i = 0
    total_records = len(records)
    page_num = 1

    while i < total_records:
        current_rec = records[i]
        current_type = current_rec[1]

        if current_type == 'text':
            _, _, content, topic_tag, ts_str = current_rec
            page_num = draw_wrapped_text(c, content, page_w, page_h, title, topic_tag, ts_str, page_num)
            i += 1
            continue

        # Header Banner (for photo / pdf-stub pages)
        c.setFillColor(colors.HexColor("#2C3E50"))
        c.rect(0, page_h - 40, page_w, 40, fill=True, stroke=False)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20, page_h - 25, f"Title: {safe_str(title)}")

        next_rec = records[i + 1] if i + 1 < total_records else None
        can_pack_two = (
            next_rec is not None and current_type == 'photo' and next_rec[1] == 'photo'
            and i in img_map and (i + 1) in img_map
        )

        if can_pack_two:
            items_to_draw = [(records[i], img_map[i]), (records[i + 1], img_map[i + 1])]
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

            elif current_type == 'pdf':
                c.setFillColor(colors.black)
                c.setFont("Helvetica", 12)
                c.drawString(30, page_h - 120, "📎 Original PDF attached — see Attachments section at the end.")

            i += 1

        c.setFillColor(colors.HexColor("#7F8C8D"))
        c.setFont("Helvetica", 9)
        c.drawRightString(page_w - 20, 15, f"Page {page_num}")
        page_num += 1
        c.showPage()

    c.save()
    pdf_buffer.seek(0)

    # Cleanup temp image files
    for path in img_map.values():
        if os.path.exists(path):
            os.remove(path)

    # Merge in original PDFs (with a divider page each) using pypdf
    writer = PdfWriter()
    main_reader = PdfReader(pdf_buffer)
    for page in main_reader.pages:
        writer.add_page(page)

    for idx, rec in pdf_records:
        if idx not in pdf_bytes_map:
            continue
        topic_tag, ts_str = rec[3], rec[4]

        divider_buf = BytesIO()
        dc = canvas.Canvas(divider_buf, pagesize=A4)
        dc.setFillColor(colors.HexColor("#2C3E50"))
        dc.rect(0, page_h - 40, page_w, 40, fill=True, stroke=False)
        dc.setFillColor(colors.white)
        dc.setFont("Helvetica-Bold", 10)
        dc.drawString(20, page_h - 25, f"Title: {safe_str(title)}")
        dc.setFillColor(colors.black)
        dc.setFont("Helvetica-Bold", 14)
        dc.drawString(30, page_h / 2, f"📎 Attached PDF - Topic: {safe_str(topic_tag)}")
        dc.setFont("Helvetica", 11)
        dc.drawString(30, page_h / 2 - 25, f"Saved on: {ts_str[:16]} (#{rec[0]})")
        dc.showPage()
        dc.save()
        divider_buf.seek(0)

        try:
            for page in PdfReader(divider_buf).pages:
                writer.add_page(page)
            for page in PdfReader(BytesIO(pdf_bytes_map[idx])).pages:
                writer.add_page(page)
        except Exception as e:
            logger.error(f"Failed to merge attached PDF #{rec[0]}: {e}")

    final_buffer = BytesIO()
    writer.write(final_buffer)
    final_buffer.seek(0)

    clean_filename = f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    chat_id = update.effective_chat.id
    await context.bot.send_document(
        chat_id=chat_id,
        document=final_buffer,
        filename=clean_filename,
        caption=f"📄 **{title}** Ready!",
        parse_mode="Markdown",
    )
    await status.delete()


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

    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago, f"%{topic.lower()}%"), f"{heading} - #{topic}")
    else:
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (week_ago,), heading)


async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago, f"%{topic.lower()}%"), f"Monthly Notes - #{topic}")
    else:
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf(update, context, sql, (month_ago,), "Monthly Notes (All)")


async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Specify topic! Example: `/topic_pdf 21`", parse_mode="Markdown")
        return
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf(update, context, sql, (f"%{user_topic.lower()}%",), f"Topic PDF - #{user_topic}")


async def list_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topics = get_all_topics()
    if not topics:
        await update.message.reply_text("No topics saved yet.")
        return
    await update.message.reply_text("🏷️ **Topics:**\n" + "\n".join(f"• {t}" for t in topics), parse_mode="Markdown")


# ----------------- Main Execution Block -----------------

async def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, receive_photo),
            MessageHandler(filters.Document.PDF, receive_pdf),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
        ],
        states={
            WAITING_FOR_TOPIC: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Document.PDF, receive_pdf),
                CallbackQueryHandler(topic_button_chosen, pattern=r"^(settopic\||newtopic$)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic),
            ],
            WAITING_FOR_CUSTOM_TOPIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_topic),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("daily_pdf", daily_pdf))
    application.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    application.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    application.add_handler(CommandHandler("topic_pdf", topic_pdf))
    application.add_handler(CommandHandler("topics", list_topics))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(menu_|topicmenu\||topicperiod\|)"))

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
