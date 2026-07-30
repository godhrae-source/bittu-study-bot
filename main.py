import os
import io
import re
import sqlite3
import logging
import threading
from datetime import datetime, timedelta, date
from pathlib import Path

from flask import Flask
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app_flask = Flask(__name__)
BASE_DIR = Path(".")
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

pending_topic = {}
pending_type = {}
pending_payload = {}


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        item_type TEXT NOT NULL,
        content TEXT,
        file_id TEXT,
        file_path TEXT,
        topic TEXT,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def save_item(chat_id, user_id, username, item_type, content, file_id, file_path, topic):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO items (chat_id, user_id, username, item_type, content, file_id, file_path, topic, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        chat_id, user_id, username, item_type, content, file_id, file_path, topic,
        datetime.now().isoformat(timespec="seconds")
    ))
    conn.commit()
    conn.close()


def get_items(where_clause="", params=()):
    conn = db()
    cur = conn.cursor()
    q = "SELECT * FROM items"
    if where_clause:
        q += " WHERE " + where_clause
    q += " ORDER BY datetime(created_at) ASC, id ASC"
    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()
    return rows


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def pdf_safe_text(text):
    return (text or "").replace("
", " ").strip()


def add_wrapped_text(c, text, x, y, max_width, line_height=14, font="Helvetica", size=11):
    c.setFont(font, size)
    words = pdf_safe_text(text).split()
    line = ""
    for word in words:
        test = (line + " " + word).strip()
        if c.stringWidth(test, font, size) <= max_width:
            line = test
        else:
            c.drawString(x, y, line)
            y -= line_height
            line = word
    if line:
        c.drawString(x, y, line)
        y -= line_height
    return y


def build_pdf(rows, title="Telegram Saved Data"):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    page_w, page_h = A4
    margin = 15 * mm
    x = margin
    y = page_h - margin

    c.setTitle(title)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, title)
    y -= 12

    c.setFont("Helvetica", 9)
    c.drawString(x, y, f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    y -= 18

    if not rows:
        c.setFont("Helvetica", 12)
        c.drawString(x, y, "No records found.")
        c.showPage()
        c.save()
        buffer.seek(0)
        return buffer

    for idx, row in enumerate(rows, start=1):
        needed = 90
        if y < needed:
            c.showPage()
            y = page_h - margin

        c.setFont("Helvetica-Bold", 12)
        c.drawString(x, y, f"{idx}. Topic: #{row['topic'] or 'untagged'}")
        y -= 16

        c.setFont("Helvetica", 10)
        c.drawString(x, y, f"Date: {row['created_at']}")
        y -= 14
        c.drawString(x, y, f"Type: {row['item_type']}")
        y -= 14

        if row["content"]:
            y = add_wrapped_text(c, f"Text: {row['content']}", x, y, page_w - 2 * margin)
            y -= 4

        if row["item_type"] == "photo" and row["file_path"] and os.path.exists(row["file_path"]):
            try:
                img = ImageReader(row["file_path"])
                iw, ih = img.getSize()
                max_w = page_w - 2 * margin
                max_h = 110 * mm
                scale = min(max_w / iw, max_h / ih, 1.0)
                dw = iw * scale
                dh = ih * scale
                if y - dh < margin:
                    c.showPage()
                    y = page_h - margin
                c.drawImage(
                    img,
                    x,
                    y - dh,
                    width=dw,
                    height=dh,
                    preserveAspectRatio=True,
                    anchor='c'
                )
                y -= dh + 10
            except Exception:
                y -= 10

        y -= 10

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a photo or text. I will ask for a #topic and save it."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Commands:
"
        "/start
"
        "/help
"
        "/daily_pdf
"
        "/weekly_pdf
"
        "/monthly_pdf
"
        "/topic_pdf topicname
"
        "/date_pdf YYYY-MM-DD
"
        "/daterange YYYY-MM-DD YYYY-MM-DD"
    )
    await update.message.reply_text(msg)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    photo = msg.photo[-1]
    pending_topic[msg.chat_id] = None
    pending_type[msg.chat_id] = "photo"
    pending_payload[msg.chat_id] = {
        "file_id": photo.file_id,
        "caption": msg.caption or ""
    }
    await msg.reply_text("Please send a #topic for this photo.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = (msg.text or "").strip()

    if msg.chat_id in pending_type and pending_type[msg.chat_id] == "photo" and msg.chat_id in pending_payload:
        if text.startswith("#"):
            topic = text[1:].strip()
        else:
            topic = text.strip()
        data = pending_payload.pop(msg.chat_id)
        pending_type.pop(msg.chat_id, None)
        save_item(
            chat_id=msg.chat_id,
            user_id=msg.from_user.id,
            username=msg.from_user.username or "",
            item_type="photo",
            content=data["caption"],
            file_id=data["file_id"],
            file_path=None,
            topic=topic,
        )
        await msg.reply_text(f"Saved photo under topic #{topic}")
        return

    if msg.chat_id in pending_type and pending_type[msg.chat_id] == "text" and msg.chat_id in pending_payload:
        if text.startswith("#"):
            topic = text[1:].strip()
        else:
            topic = text.strip()
        data = pending_payload.pop(msg.chat_id)
        pending_type.pop(msg.chat_id, None)
        save_item(
            chat_id=msg.chat_id,
            user_id=msg.from_user.id,
            username=msg.from_user.username or "",
            item_type="text",
            content=data["content"],
            file_id=None,
            file_path=None,
            topic=topic,
        )
        await msg.reply_text(f"Saved text under topic #{topic}")
        return

    pending_topic[msg.chat_id] = None
    pending_type[msg.chat_id] = "text"
    pending_payload[msg.chat_id] = {"content": text}
    await msg.reply_text("Please send a #topic for this text.")


async def topic_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = (msg.text or "").strip()
    if not text.startswith("#"):
        return
    topic = text[1:].strip()

    if msg.chat_id in pending_type and pending_type[msg.chat_id] == "text":
        data = pending_payload.pop(msg.chat_id, {"content": ""})
        pending_type.pop(msg.chat_id, None)
        save_item(
            chat_id=msg.chat_id,
            user_id=msg.from_user.id,
            username=msg.from_user.username or "",
            item_type="text",
            content=data.get("content", ""),
            file_id=None,
            file_path=None,
            topic=topic,
        )
        await msg.reply_text(f"Saved text under topic #{topic}")
        return

    if msg.chat_id in pending_type and pending_type[msg.chat_id] == "photo":
        data = pending_payload.pop(msg.chat_id, {})
        pending_type.pop(msg.chat_id, None)
        save_item(
            chat_id=msg.chat_id,
            user_id=msg.from_user.id,
            username=msg.from_user.username or "",
            item_type="photo",
            content=data.get("caption", ""),
            file_id=data.get("file_id"),
            file_path=None,
            topic=topic,
        )
        await msg.reply_text(f"Saved photo under topic #{topic}")
        return


async def generate_and_send_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, rows, filename):
    if not rows:
        await update.message.reply_text("No records found.")
        return

    if len(rows) > 10:
        await update.message.reply_text("This may take up to 2 minutes. I am generating it now.")

    pdf_buffer = build_pdf(rows, title=filename.replace(".pdf", "").replace("_", " ").title())
    pdf_bytes = pdf_buffer.read()
    input_file = InputFile(io.BytesIO(pdf_bytes), filename=filename)

    try:
        if CHANNEL_ID:
            try:
                await context.bot.send_document(
                    chat_id=CHANNEL_ID,
                    document=input_file,
                    caption="PDF saved to your private channel!"
                )
                await update.message.reply_text("PDF saved to your private channel!")
                return
            except Exception:
                pass

        await update.message.reply_document(document=InputFile(io.BytesIO(pdf_bytes), filename=filename))
    except Exception:
        await update.message.reply_text("I could not send the PDF right now.")


async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    rows = get_items("date(created_at) = date(?)", (today,))
    await generate_and_send_pdf(update, context, rows, "daily.pdf")


async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = (date.today() - timedelta(days=7)).isoformat()
    rows = get_items("date(created_at) >= date(?)", (start,))
    await generate_and_send_pdf(update, context, rows, "weekly.pdf")


async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = (date.today() - timedelta(days=30)).isoformat()
    rows = get_items("date(created_at) >= date(?)", (start,))
    await generate_and_send_pdf(update, context, rows, "monthly.pdf")


async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /topic_pdf topicname")
        return
    topic = " ".join(context.args).strip()
    rows = get_items("lower(topic) = lower(?)", (topic,))
    await generate_and_send_pdf(update, context, rows, f"topic_{topic}.pdf")


async def date_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /date_pdf YYYY-MM-DD")
        return
    d = context.args[0]
    try:
        parse_date(d)
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD")
        return
    rows = get_items("date(created_at) = date(?)", (d,))
    await generate_and_send_pdf(update, context, rows, f"date_{d}.pdf")


async def daterange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /daterange YYYY-MM-DD YYYY-MM-DD")
        return
    d1, d2 = context.args[0], context.args[1]
    try:
        parse_date(d1)
        parse_date(d2)
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD")
        return
    rows = get_items("date(created_at) BETWEEN date(?) AND date(?)", (d1, d2))
    await generate_and_send_pdf(update, context, rows, f"daterange_{d1}_to_{d2}.pdf")


@app_flask.route("/health")
def health():
    return "ok", 200


def run_flask():
    app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("daily_pdf", daily_pdf))
    application.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    application.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    application.add_handler(CommandHandler("topic_pdf", topic_pdf))
    application.add_handler(CommandHandler("date_pdf", date_pdf))
    application.add_handler(CommandHandler("daterange", daterange))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling()


if __name__ == "__main__":
    main()
