import os
import asyncio
import logging
import sqlite3
import hashlib
import time
import re
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
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

# ==================== GUJARATI AI SUMMARIZER ====================
class GujaratiSummarizer:
    """Simple AI summarizer for Gujarati text"""
    
    def __init__(self):
        # Common Gujarati stopwords
        self.stopwords = {
            'અને', 'આ', 'આવી', 'આવું', 'આવો', 'આવા', 'એ', 'એક', 
            'એમ', 'એવા', 'એવું', 'એવી', 'કે', 'કરી', 'કરે', 'કર્યું',
            'છે', 'છેઅ', 'છેતો', 'જ', 'જે', 'તે', 'તેમ', 'તેને',
            'તેની', 'તેનું', 'તેના', 'તો', 'થઈ', 'થયું', 'થાય',
            'થી', 'દ્વારા', 'ને', 'પણ', 'પર', 'પ્રમાણે', 'માટે',
            'મા', 'માં', 'રહી', 'રહે', 'વગેરે', 'વાળું', 'સાથે',
            'સુધી', 'હતા', 'હતી', 'હતું', 'હતો', 'હવે', 'હોય'
        }
    
    def extract_keywords(self, text):
        """Extract important keywords from Gujarati text"""
        # Clean text - keep Gujarati characters
        text = re.sub(r'[^\u0A80-\u0AFF\s]', '', text)
        words = text.split()
        
        # Remove stopwords and count frequency
        word_freq = {}
        for word in words:
            if word not in self.stopwords and len(word) > 1:
                word_freq[word] = word_freq.get(word, 0) + 1
        
        # Sort by frequency
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [word for word, freq in sorted_words[:5]]
    
    def generate_summary(self, text, max_words=150):
        """Generate summary of Gujarati text"""
        if not text or len(text.split()) <= 30:
            return text if text else "કોઈ ટેક્સ્ટ નથી."
        
        # Split into sentences
        sentences = re.split(r'[.!?।]', text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
        
        if len(sentences) <= 2:
            return text[:200] + '...'
        
        # Score sentences based on keyword presence
        keywords = self.extract_keywords(text)
        sentence_scores = {}
        
        for i, sentence in enumerate(sentences):
            score = 0
            for keyword in keywords:
                if keyword in sentence:
                    score += 2
            # Give higher score to first and last sentences
            if i == 0:
                score += 3
            if i == len(sentences) - 1:
                score += 2
            sentence_scores[sentence] = score
        
        # Sort by score and select top sentences
        sorted_sentences = sorted(sentence_scores.items(), key=lambda x: x[1], reverse=True)
        
        # Take top sentences until we reach max_words
        summary = []
        word_count = 0
        for sentence, score in sorted_sentences[:3]:
            words = sentence.split()
            if word_count + len(words) <= max_words:
                summary.append(sentence)
                word_count += len(words)
            else:
                break
        
        # If no summary generated, take first sentence
        if not summary:
            summary = [sentences[0][:100]]
        
        return ' '.join(summary) + '...'
    
    def summarize_for_pdf(self, records, title):
        """Generate summary of all content in Gujarati"""
        if not records:
            return "કોઈ સામગ્રી મળી નથી. (No content found)"
        
        # Collect all text content
        all_text = []
        for rec in records:
            if rec['type'] == 'text':
                all_text.append(rec['content'])
        
        if not all_text:
            return "ફક્ત ફોટાઓ છે, ટેક્સ્ટ સારાંશ ઉપલબ્ધ નથી.\n(Only photos, text summary not available)"
        
        combined_text = ' '.join(all_text)
        
        # Generate summary
        summary = self.generate_summary(combined_text, 150)
        
        # Add Gujarati header
        header = "📝 સારાંશ (Summary)\n"
        footer = f"\n---\n📊 કુલ {len(records)} એન્ટ્રીઓ | {len(all_text)} ટેક્સ્ટ એન્ટ્રીઓ"
        
        return header + summary + footer
    
    def get_topic_summary(self, records, topic):
        """Generate topic-specific summary in Gujarati"""
        if not records:
            return f"'{topic}' વિષય પર કોઈ સામગ્રી નથી."
        
        all_text = []
        for rec in records:
            if rec['type'] == 'text':
                all_text.append(rec['content'])
        
        if not all_text:
            return f"'{topic}' વિષય પર ફક્ત ફોટાઓ છે."
        
        combined_text = ' '.join(all_text)
        summary = self.generate_summary(combined_text, 120)
        
        return f"📌 {topic} વિષયનો સારાંશ:\n\n{summary}"

# Initialize summarizer
summarizer = GujaratiSummarizer()

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
    # Keep Gujarati characters
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
        
        if max(img.size) > 1200:
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        
        temp_path = f"temp_{idx}_{int(time.time())}.jpg"
        img.save(temp_path, "JPEG", quality=85, optimize=True)
        return idx, temp_path
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None

# ==================== PDF GENERATOR WITH GUJARATI SUMMARY ====================
def generate_pdf_with_summary(records, downloaded_images, title, summary_text):
    """Generate PDF with Gujarati summary on first page"""
    pdf_buffer = BytesIO()
    
    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                           leftMargin=30, rightMargin=30,
                           topMargin=50, bottomMargin=50)
    
    story = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    header_style = ParagraphStyle(
        'CustomHeader',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#2C3E50'),
        alignment=TA_CENTER,
        spaceAfter=15
    )
    
    summary_style = ParagraphStyle(
        'SummaryStyle',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.HexColor('#1A5276'),
        leftIndent=15,
        rightIndent=15,
        spaceAfter=15,
        backColor=colors.HexColor('#EBF5FB'),
        borderPadding=10,
        borderColor=colors.HexColor('#2980B9'),
        borderWidth=1
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
    
    # Title
    story.append(Paragraph(f"📚 {title}", header_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", 
                          styles['Italic']))
    story.append(Spacer(1, 10))
    
    # Gujarati Summary on first page
    if summary_text:
        story.append(Paragraph("📝 *AI સારાંશ (AI Summary)*", styles['Heading3']))
        story.append(Paragraph(summary_text.replace('\n', '<br/>'), summary_style))
        story.append(Spacer(1, 15))
    
    # Content
    for idx, rec in enumerate(records):
        # Topic
        topic_text = f"<b>વિષય (Topic):</b> {rec['topic']} | <b>તારીખ (Date):</b> {rec['timestamp'][:16]}"
        story.append(Paragraph(topic_text, topic_style))
        story.append(Spacer(1, 5))
        
        if rec['type'] == 'photo' and idx in downloaded_images:
            img_path = downloaded_images[idx]
            try:
                img = Image.open(img_path)
                img_width, img_height = img.size
                
                page_width = A4[0] - 60
                page_height = A4[1] - 150
                
                ratio = min(page_width / img_width, page_height / img_height)
                width = img_width * ratio * 0.9
                height = img_height * ratio * 0.9
                
                story.append(Spacer(1, 8))
                story.append(RLImage(img_path, width=width, height=height))
                story.append(Spacer(1, 8))
                
            except Exception as e:
                logger.error(f"Error adding image {idx}: {e}")
                story.append(Paragraph("⚠️ ચિત્ર લોડ કરી શકાયું નહીં", text_style))
            
        elif rec['type'] == 'text':
            text_content = safe_str(rec['content'])
            if len(text_content) > 500:
                text_content = text_content[:500] + "..."
            story.append(Paragraph(text_content, text_style))
        
        story.append(Spacer(1, 8))
        story.append(Paragraph("<hr/>", styles['Normal']))
        story.append(Spacer(1, 8))
    
    # Footer
    story.append(Spacer(1, 15))
    story.append(Paragraph(f"<i>📊 કુલ {len(records)} એન્ટ્રીઓ | Generated by Bittu Study Bot</i>", 
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
async def generate_and_send_pdf_with_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                              query_sql: str, params: tuple, title: str):
    """Generate and send PDF with Gujarati AI summary"""
    chat_id = update.effective_chat.id
    
    # Get message to edit
    if update.message:
        status_msg = await update.message.reply_text("⏳ PDF બનાવી રહ્યા છીએ...")
    elif update.callback_query:
        status_msg = await update.callback_query.message.reply_text("⏳ PDF બનાવી રહ્યા છીએ...")
    else:
        return
    
    try:
        # Check cache
        cache_key = get_cache_key(query_sql, params, title)
        if cache_key in pdf_cache:
            cached_time, cached_pdf = pdf_cache[cache_key]
            if time.time() - cached_time < CACHE_EXPIRY:
                await status_msg.edit_text("⚡ કેશમાંથી PDF મોકલીએ છીએ...")
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=BytesIO(cached_pdf),
                    filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                    caption=f"📄 **{title}** Ready! (Cached)"
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
            await status_msg.edit_text("⚠️ કોઈ રેકોર્ડ મળ્યા નથી.")
            return

        await status_msg.edit_text(f"📊 {len(records)} આઇટમ્સ પ્રોસેસ કરી રહ્યા છીએ...")

        # Generate Gujarati summary
        await status_msg.edit_text("🤖 AI સારાંશ બનાવી રહ્યા છીએ...")
        summary = summarizer.summarize_for_pdf(records, title)

        # Download images
        photo_records = [(idx, r) for idx, r in enumerate(records) if r['type'] == 'photo']
        downloaded_images = {}
        
        if photo_records:
            await status_msg.edit_text(f"📸 {len(photo_records)} ફોટાઓ ડાઉનલોડ કરી રહ્યા છીએ...")
            download_tasks = []
            for idx, rec in photo_records:
                download_tasks.append(fetch_image_fast(context.bot, rec['content'], idx))
            
            results = await asyncio.gather(*download_tasks)
            downloaded_images = {idx: path for idx, path in results if path}

        # Generate PDF
        await status_msg.edit_text("📄 PDF બનાવી રહ્યા છીએ...")
        loop = asyncio.get_event_loop()
        pdf_buffer = await loop.run_in_executor(
            executor,
            generate_pdf_with_summary,
            records,
            downloaded_images,
            title,
            summary
        )

        # Cache
        pdf_data = pdf_buffer.getvalue()
        pdf_cache[cache_key] = (time.time(), pdf_data)

        # Send PDF
        await status_msg.edit_text("📤 PDF મોકલી રહ્યા છીએ...")
        await context.bot.send_document(
            chat_id=chat_id,
            document=BytesIO(pdf_data),
            filename=f"Study_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
            caption=f"📄 **{title}** Ready! ({len(records)} items)\n🤖 AI સારાંશ સાથે"
        )
        
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"PDF generation error: {e}")
        await status_msg.edit_text(f"❌ એરર: {str(e)[:100]}")

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
        [InlineKeyboardButton("🤖 AI Summary", callback_data="menu_ai_summary"),
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
        "🤖 **AI Feature:**\n"
        "PDFમાં Gujarati AI સારાંશ ઉમેરાય છે.\n\n"
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
            "📌 **Content received!**\nReply with **#topic** (e.g., `#chemistry`):\n\n"
            "📸 **સામગ્રી મળી!**\n**#topic** સાથે જવાબ આપો.",
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
            "📝 **Text received!**\nReply with **#topic** (e.g., `#chemistry`):\n\n"
            "📝 **લખાણ મળ્યું!**\n**#topic** સાથે જવાબ આપો.",
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
    
    status_msg = await update.message.reply_text(f"💾 {len(items)} item(s) સેવ થઈ રહ્યા છે...")
    
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
    
    await status_msg.edit_text(f"✅ **{len(items)} item(s) under {topic} સેવ થયા!**",
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
        await generate_and_send_pdf_with_summary(update, context, sql, (today, f"%{topic.lower()}%"),
                                    f"Daily Notes - #{topic}")
    else:
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (today,), "Daily Notes (All)")

async def weekly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")
    heading = f"Weekly Report ({start_date} to {end_date})"
    
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (week_ago, f"%{topic.lower()}%"),
                                    f"{heading} - #{topic}")
    else:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (week_ago,), heading)

async def monthly_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        topic = context.args[0].strip().replace("#", "")
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? AND LOWER(topic) LIKE ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (month_ago, f"%{topic.lower()}%"),
                                    f"Monthly Notes - #{topic}")
    else:
        month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE timestamp >= ? ORDER BY timestamp ASC"
        await generate_and_send_pdf_with_summary(update, context, sql, (month_ago,), "Monthly Notes (All)")

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
                "🏷️ *Select a topic:*\n\nOr use `/topic_pdf topic_name`\n\n"
                "🏷️ *વિષય પસંદ કરો:*",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("⚠️ No topics found. Add some data first!")
        return
    
    user_topic = context.args[0].strip().replace("#", "")
    sql = "SELECT id, type, content, topic, timestamp FROM content_store WHERE LOWER(topic) LIKE ? ORDER BY timestamp ASC"
    await generate_and_send_pdf_with_summary(update, context, sql, (f"%{user_topic.lower()}%",),
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
    await generate_and_send_pdf_with_summary(update, context, sql, (today,), "📊 Instant Report - Today")

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

async def ai_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate AI summary for today's content in Gujarati"""
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    
    conn = sqlite3.connect(
