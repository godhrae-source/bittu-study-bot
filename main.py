import os
import io
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image
import flask
from flask import Flask

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
DATABASE = 'media_storage.db'

# Initialize database
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS media
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  media_type TEXT,
                  file_id TEXT,
                  content TEXT,
                  topic TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# Flask app for health check
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return 'OK', 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))

# Database operations
def save_media(user_id, media_type, file_id, content, topic):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("INSERT INTO media (user_id, media_type, file_id, content, topic) VALUES (?, ?, ?, ?, ?)",
              (user_id, media_type, file_id, content, topic))
    conn.commit()
    conn.close()

def get_media_by_topic(topic):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT media_type, file_id, content, timestamp FROM media WHERE topic = ? ORDER BY timestamp DESC", (topic,))
    results = c.fetchall()
    conn.close()
    return results

def get_media_by_date(date_str):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT media_type, file_id, content, timestamp FROM media WHERE DATE(timestamp) = ? ORDER BY timestamp DESC", (date_str,))
    results = c.fetchall()
    conn.close()
    return results

def get_media_by_date_range(start_date, end_date):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT media_type, file_id, content, timestamp FROM media WHERE DATE(timestamp) BETWEEN ? AND ? ORDER BY timestamp DESC", 
              (start_date, end_date))
    results = c.fetchall()
    conn.close()
    return results

def get_media_by_period(period):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    
    now = datetime.now()
    if period == 'daily':
        start_date = now.strftime('%Y-%m-%d')
    elif period == 'weekly':
        start_date = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    elif period == 'monthly':
        start_date = (now - timedelta(days=30)).strftime('%Y-%m-%d')
    else:
        start_date = None
    
    if start_date:
        c.execute("SELECT media_type, file_id, content, timestamp FROM media WHERE DATE(timestamp) >= ? ORDER BY timestamp DESC", (start_date,))
    else:
        c.execute("SELECT media_type, file_id, content, timestamp FROM media ORDER BY timestamp DESC")
    
    results = c.fetchall()
    conn.close()
    return results

# PDF generation
async def generate_pdf_async(context: ContextTypes.DEFAULT_TYPE, chat_id, media_list, filename):
    try:
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        
        for idx, (media_type, file_id, content, timestamp) in enumerate(media_list):
            if idx > 0:
                c.showPage()
            
            # Add timestamp header
            c.setFont("Helvetica-Bold", 14)
            c.drawString(50, height - 50, f"Date: {timestamp}")
            
            if media_type == 'photo' and file_id:
                try:
                    file = await context.bot.get_file(file_id)
                    file_bytes = await file.download_as_bytearray()
                    image = Image.open(io.BytesIO(bytes(file_bytes)))
                    
                    # Compress image
                    image.thumbnail((800, 800), Image.Resampling.LANCZOS)
                    img_buffer = io.BytesIO()
                    image.save(img_buffer, format='JPEG', quality=85, optimize=True)
                    img_buffer.seek(0)
                    
                    img = ImageReader(img_buffer)
                    img_width, img_height = image.size
                    
                    # Calculate scaling to fit page
                    max_width = width - 100
                    max_height = height - 150
                    scale = min(max_width / img_width, max_height / img_height)
                    new_width = img_width * scale
                    new_height = img_height * scale
                    
                    x = (width - new_width) / 2
                    y = height - 100 - new_height
                    
                    c.drawImage(img, x, y, width=new_width, height=new_height)
                except Exception as e:
                    logger.error(f"Error processing image: {e}")
                    c.setFont("Helvetica", 12)
                    c.drawString(50, height - 100, "[Image could not be loaded]")
            
            elif media_type == 'text' and content:
                c.setFont("Helvetica", 12)
                text_object = c.beginText(50, height - 100)
                text_object.setTextOrigin(50, height - 100)
                
                # Word wrap
                words = content.split()
                line = ""
                for word in words:
                    test_line = line + word + " "
                    if c.stringWidth(test_line, "Helvetica", 12) < width - 100:
                        line = test_line
                    else:
                        text_object.textLine(line)
                        line = word + " "
                if line:
                    text_object.textLine(line)
                
                c.drawText(text_object)
        
        c.save()
        buffer.seek(0)
        
        # Send to channel
        try:
            await context.bot.send_document(
                chat_id=CHANNEL_ID,
                document=buffer,
                filename=filename,
                caption=f"Generated PDF: {filename}"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ PDF saved to your private channel!"
            )
        except Exception as e:
            logger.error(f"Error sending to channel: {e}")
            # Fallback: send directly to user
            buffer.seek(0)
            await context.bot.send_document(
                chat_id=chat_id,
                document=buffer,
                filename=filename
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ PDF generated and sent to you directly!"
            )
    
    except Exception as e:
        logger.error(f"Error generating PDF: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Error generating PDF. Please try again."
        )

def start_background_pdf(context, chat_id, media_list, filename):
    loop = context.application.loop
    loop.create_task(generate_pdf_async(context, chat_id, media_list, filename))

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Media-to-PDF Bot!\n\n"
        "Send me photos or text, and I'll ask you for a #topic to organize them.\n\n"
        "Commands:\n"
        "/daily_pdf - Generate PDF of today's media\n"
        "/weekly_pdf - Generate PDF of last 7 days\n"
        "/monthly_pdf - Generate PDF of last 30 days\n"
        "/topic_pdf #topic - Generate PDF for specific topic\n"
        "/date_pdf YYYY-MM-DD - Generate PDF for specific date\n"
        "/daterange YYYY-MM-DD YYYY-MM-DD - Generate PDF for date range"
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if update.message.photo:
        photo = update.message.photo[-1]  # Get highest resolution
        context.user_data['pending_media'] = {
            'type': 'photo',
            'file_id': photo.file_id
        }
        await update.message.reply_text("📸 Photo received! Please provide a #topic (e.g., #work, #personal):")
    
    elif update.message.text and not update.message.text.startswith('/'):
        if 'pending_media' in context.user_data:
            topic = update.message.text.strip()
            if not topic.startswith('#'):
                topic = '#' + topic
            
            save_media(
                user_id=user_id,
                media_type=context.user_data['pending_media']['type'],
                file_id=context.user_data['pending_media'].get('file_id'),
                content=update.message.text if context.user_data['pending_media']['type'] == 'text' else None,
                topic=topic
            )
            
            del context.user_data['pending_media']
            await update.message.reply_text(f"✅ Saved with topic: {topic}")
        else:
            context.user_data['pending_media'] = {
                'type': 'text',
                'content': update.message.text
            }
            await update.message.reply_text("📝 Text received! Please provide a #topic (e.g., #work, #personal):")

async def daily_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_pdf_command(update, context, 'daily', "daily_report.pdf")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_pdf_command(update, context, 'weekly', "weekly_report.pdf")

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await generate_pdf_command(update, context, 'monthly', "monthly_report.pdf")

async def topic_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Please provide a topic. Example: /topic_pdf #work")
        return
    
    topic = context.args[0]
    if not topic.startswith('#'):
        topic = '#' + topic
    
    media_list = get_media_by_topic(topic)
    if not media_list:
        await update.message.reply_text(f"❌ No media found for topic: {topic}")
        return
    
    await generate_pdf_command(update, context, None, f"topic_{topic[1:]}.pdf", media_list)

async def date_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Please provide a date. Example: /date_pdf 2024-01-15")
        return
    
    date_str = context.args[0]
    media_list = get_media_by_date(date_str)
    if not media_list:
        await update.message.reply_text(f"❌ No media found for date: {date_str}")
        return
    
    await generate_pdf_command(update, context, None, f"date_{date_str}.pdf", media_list)

async def daterange_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("❌ Please provide start and end dates. Example: /daterange 2024-01-01 2024-01-31")
        return
    
    start_date, end_date = context.args
    media_list = get_media_by_date_range(start_date, end_date)
    if not media_list:
        await update.message.reply_text(f"❌ No media found between {start_date} and {end_date}")
        return
    
    await generate_pdf_command(update, context, None, f"range_{start_date}_to_{end_date}.pdf", media_list)

async def generate_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE, period, filename, media_list=None):
    if media_list is None:
        media_list = get_media_by_period(period)
    
    if not media_list:
        await update.message.reply_text("❌ No media found for the specified period.")
        return
    
    item_count = len(media_list)
    
    if item_count > 10:
        await update.message.reply_text(
            f"⚠️ Found {item_count} items. This may take up to 2 minutes to generate. "
            f"I'll work in the background and notify you when done!"
        )
        # Start background task
        thread = threading.Thread(
            target=start_background_pdf,
            args=(context, update.effective_chat.id, media_list, filename)
        )
        thread.start()
    else:
        await update.message.reply_text(f"📄 Generating PDF with {item_count} items...")
        await generate_pdf_async(context, update.effective_chat.id, media_list, filename)

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ An error occurred. Please try again.")

def main():
    init_db()
    
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("daily_pdf", daily_pdf))
    application.add_handler(CommandHandler("weekly_pdf", weekly_pdf))
    application.add_handler(CommandHandler("monthly_pdf", monthly_pdf))
    application.add_handler(CommandHandler("topic_pdf", topic_pdf))
    application.add_handler(CommandHandler("date_pdf", date_pdf))
    application.add_handler(CommandHandler("daterange", daterange_pdf))
    
    # Handle photos and text
    application.add_handler(MessageHandler(filters.PHOTO, handle_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_media))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # Start bot
    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
