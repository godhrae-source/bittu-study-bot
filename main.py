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

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Image as RLImage, PageBreak
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
        text = re.sub(r'[^\u0A80-\u0AFF\s]', '', text)
        words = text.split()
        word_freq = {}
        for word in words:
            if word not in self.stopwords and len(word) > 1:
                word_freq[word] = word_freq.get(word, 0) + 1
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [word for word, freq in sorted_words[:5]]
    
    def generate_summary(self, text, max_words=150):
        if not text or len(text.split()) <= 30:
            return text if text else "કોઈ ટેક્સ્ટ નથી."
        sentences = re.split(r'[.!?।]', text)
        sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
        if len(sentences) <= 2:
            return text[:200] + '...'
        keywords = self.extract_keywords(text)
        sentence_scores = {}
        for i, sentence in enumerate(sentences):
            score = 0
            for keyword in keywords:
                if keyword in sentence:
                    score += 2
            if i == 0:
                score += 3
            if i == len(sentences) - 1:
                score += 2
            sentence_scores[sentence] = score
        sorted_sentences = sorted(sentence_scores.items(), key=lambda x: x[1], reverse=True)
        summary = []
        word_count = 0
        for sentence, score in sorted_sentences[:3]:
            words = sentence.split()
            if word_count + len(words) <= max_words:
                summary.append(sentence)
                word_count += len(words)
            else:
                break
        if not summary:
            summary = [sentences[0][:100]]
        return ' '.join(summary) + '...'
    
    def summarize_for_pdf(self, records, title):
        if not records:
            return "કોઈ સામગ્રી મળી નથી. (No content found)"
        all_text = []
        for rec in records:
            if rec['type'] == 'text':
                all_text.append(rec['content'])
        if not all_text:
            return "ફક્ત ફોટાઓ છે, ટેક્સ્ટ સારાંશ ઉપલબ્ધ નથી.\n(Only photos, text summary not available)"
        combined_text = ' '.join(all_text)
        summary = self.generate_summary(combined_text, 150)
        header = "📝 સારાંશ (Summary)\n"
        footer = f"\n---\n📊 કુલ {len(records)} એન્ટ્રીઓ | {len(all_text)} ટેક્સ્ટ એન્ટ્રીઓ"
        return header + summary + footer

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

def get_gujarati_day(date_str):
    """Convert date to Gujarati day"""
    days = {
        'Monday': 'સોમવાર', 'Tuesday': 'મંગળવાર', 'Wednesday': 'બુધવાર',
        'Thursday': 'ગુરુવાર', 'Friday': 'શુક્રવાર', 'Saturday': 'શનિવાર',
        'Sunday': 'રવિવાર'
    }
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day_name = dt.strftime("%A")
        return days.get(day_name, day_name)
    except:
        return ""

# ==================== FLASK SERVER ====================
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

async def run_flask():
    from wsgiref.simple_server import make_server
    server = make_server("0.0.0.0", PORT, app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

# ==================== IMAGE HANDLING ====================
async def fetch_image_fast(bot, file_id, idx):
    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
        img = Image.open(BytesIO(file_bytes)).convert("RGB")
        if max(img.size) > 1200:
            img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        temp_path = f"temp_{idx}_{int(time.time())}.jpg"
        img.save(temp_path, "JPEG", quality=85, optimize=True)
        return idx, temp_path, img.size
    except Exception as e:
        logger.error(f"Error downloading photo {file_id}: {e}")
        return idx, None, None

# ==================== SMART PDF GENERATOR WITH INDEX ====================
def generate_smart_pdf(records, downloaded_images, title, summary_text, record_ids):
    """Smart PDF with index in Gujarati, no white space, smart packing"""
    pdf_buffer = BytesIO()
    
    # Page setup
    page_width, page_height = A4
    margin = 25
    usable_width = page_width - (2 * margin)
    usable_height = page_height - (2 * margin)
    
    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4,
                           leftMargin=margin, rightMargin=margin,
                           topMargin=margin, bottomMargin=margin)
    
    story = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    header_style = ParagraphStyle(
        'CustomHeader',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#1A237E'),
        alignment=TA_CENTER,
        spaceAfter=10
    )
    
    sub_header_style = ParagraphStyle(
        'SubHeader',
        parent=styles['Normal'],
        fontSize=12,
        textColor=colors.HexColor('#455A64'),
        alignment=TA_CENTER,
        spaceAfter=15
    )
    
    index_style = ParagraphStyle(
        'IndexStyle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#1A237E'),
        leftIndent=10,
        spaceAfter=4,
        backColor=colors.HexColor('#E8EAF6'),
        borderPadding=5
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
        fontSize=10,
        textColor=colors.HexColor('#455A64'),
        alignment=TA_CENTER,
        spaceAfter=5
    )
    
    # ==================== PAGE 1: INDEX IN GUJARATI ====================
    # Header with Date, Day, Total Images
    today_str = datetime.now().strftime("%Y-%m-%d")
    guj_day = get_gujarati_day(today_str)
    
    story.append(Paragraph(f"📚 {title}", header_style))
    story.append(Paragraph(f"📅 તારીખ (Date): {today_str} | {guj_day}", sub_header_style))
    story.append(Paragraph(f"📸 કુલ ફોટાઓ (Total Images): {len(records)}", sub_header_style))
    story.append(Spacer(1, 10))
    
    # Gujarati Summary
    if summary_text:
        story.append(Paragraph("📝 *AI સારાંશ (AI Summary)*", styles['Heading3']))
        story.append(Paragraph(summary_text.replace('\n', '<br/>'), summary_style))
        story.append(Spacer(1, 10))
    
    # Index Header
    story.append(Paragraph("📋 *સૂચિ (Index)*", styles['Heading2']))
    story.append(Spacer(1, 8))
    
    # Build Index
    index_entries = []
    for idx, rec in enumerate(records, 1):
        topic = rec['topic']
        timestamp = rec['timestamp'][:16]
        # Create a title from topic or content
        if rec['type'] == 'photo':
            title_text = f"Image {idx}"
        else:
            title_text = rec['content'][:50] + "..." if len(rec['content']) > 50 else rec['content']
        index_entries.append(f"{idx}. {topic} - {title_text} ({timestamp})")
    
    # Display index in columns for better use of space
    for entry in index_entries[:20]:  # Show up to 20 entries on index page
        story.append(Paragraph(entry, index_style))
        story.append(Spacer(1, 2))
    
    if len(records) > 20:
        story.append(Paragraph(f"... અને {len(records) - 20} વધુ એન્ટ્રીઓ", styles['Italic']))
    
    story.append(Spacer(1, 10))
    story.append(Paragraph("<hr/>", styles['Normal']))
    story.append(Spacer(1, 5))
    story.append(Paragraph(f"<i>કુલ {len(records)} એન્ટ્રીઓ | Generated by Bittu Study Bot</i>", 
                          styles['Italic']))
    
    # Page break after index
    story.append(PageBreak())
    
    # ==================== CONTENT PAGES: SMART PACKING ====================
    # Group images by type (photo vs text)
    photo_items = [(idx, rec) for idx, rec in enumerate(records) if rec['type'] == 'photo' and idx in downloaded_images]
    text_items = [(idx, rec) for idx, rec in enumerate(records) if rec['type'] == 'text']
    
    # Process photos first with smart packing
    current_page_photos = []
    current_page_texts = []
    current_page_height = 0
    max_page_height = usable_height - 80  # Reserve space for headers
    
    def flush_page():
        nonlocal story, current_page_photos, current_page_texts, current_page_height
        if current_page_photos or current_page_texts:
            # Add page header
            story.append(Paragraph(f"📷 પૃષ્ઠ (Page) {len(story) + 1}", sub_header_style))
            story.append(Spacer(1, 5))
            
            # Add photos with smart layout
            if current_page_photos:
                # Calculate grid layout
                num_photos = len(current_page_photos)
                if num_photos == 1:
                    # Single photo - use full page
                    img_path, img_size = current_page_photos[0][1], current_page_photos[0][2]
                    if img_path:
                        w, h = img_size
                        # Fit to page
                        max_w = usable_width - 20
                        max_h = usable_height - 100
                        ratio = min(max_w / w, max_h / h)
                        width = w * ratio * 0.95
                        height = h * ratio * 0.95
                        story.append(RLImage(img_path, width=width, height=height))
                elif num_photos == 2:
                    # Two photos - side by side or stacked based on orientation
                    img1_path, img1_size = current_page_photos[0][1], current_page_photos[0][2]
                    img2_path, img2_size = current_page_photos[1][1], current_page_photos[1][2]
                    
                    if img1_path and img2_path:
                        w1, h1 = img1_size
                        w2, h2 = img2_size
                        
                        # Check if both are horizontal (landscape)
                        if w1 > h1 and w2 > h2:
                            # Side by side for horizontal images
                            half_width = (usable_width - 10) / 2
                            ratio1 = min(half_width / w1, (usable_height - 80) / h1)
                            ratio2 = min(half_width / w2, (usable_height - 80) / h2)
                            width1 = w1 * ratio1 * 0.9
                            height1 = h1 * ratio1 * 0.9
                            width2 = w2 * ratio2 * 0.9
                            height2 = h2 * ratio2 * 0.9
                            
                            # Create side by side
                            story.append(Spacer(1, 5))
                            story.append(RLImage(img1_path, width=width1, height=height1))
                            story.append(Spacer(1, 5))
                            story.append(RLImage(img2_path, width=width2, height=height2))
                        else:
                            # Stack vertically
                            max_w = usable_width - 20
                            max_h = (usable_height - 100) / 2
                            
                            ratio1 = min(max_w / w1, max_h / h1)
                            width1 = w1 * ratio1 * 0.9
                            height1 = h1 * ratio1 * 0.9
                            
                            ratio2 = min(max_w / w2, max_h / h2)
                            width2 = w2 * ratio2 * 0.9
                            height2 = h2 * ratio2 * 0.9
                            
                            story.append(RLImage(img1_path, width=width1, height=height1))
                            story.append(Spacer(1, 5))
                            story.append(RLImage(img2_path, width=width2, height=height2))
                else:
                    # 3+ photos - grid layout
                    cols = 2 if num_photos <= 4 else 3
                    rows = (num_photos + cols - 1) // cols
                    
                    cell_width = (usable_width - (cols - 1) * 5) / cols
                    cell_height = (usable_height - 100 - (rows - 1) * 5) / rows
                    
                    # Add images in grid
                    for i, (img_path, img_size, rec) in enumerate(current_page_photos):
                        if img_path:
                            w, h = img_size
                            ratio = min(cell_width / w, cell_height / h)
                            width = w * ratio * 0.85
                            height = h * ratio * 0.85
                            
                            # Add topic label
                            story.append(Paragraph(f"📌 {rec['topic']}", topic_style))
                            story.append(RLImage(img_path, width=width, height=height))
                            story.append(Spacer(1, 3))
            
            # Add text items
            for idx, rec in current_page_texts:
                story.append(Paragraph(f"📝 *{rec['topic']}*", styles['Heading4']))
                text_content = safe_str(rec['content'])[:300]
                if len(rec['content']) > 300:
                    text_content += "..."
                story.append(Paragraph(text_content, styles['Normal']))
                story.append(Spacer(1, 5))
            
            story.append(Spacer(1, 10))
            story.append(Paragraph("<hr/>", styles['Normal']))
            story.append(Spacer(1, 5))
            
            # Reset
            current_page_photos = []
            current_page_texts = []
            current_page_height = 0
    
    # Process all items with smart packing
    for idx, rec in enumerate(records):
        if rec['type'] == 'photo' and idx in downloaded_images:
            img_path, img_size = downloaded_images[idx], downloaded_images.get(f"{idx}_size", (400, 300))
            # Estimate space needed
            photo_height = min(img_size[1], 400)
            
            # Check if adding this photo exceeds page height
            if current_page_height + photo_height + 60 > max_page_height and current_page_photos:
                flush_page()
                current_page_height = 0
            
            current_page_photos.append((img_path, img_size, rec))
            current_page_height += photo_height + 30
            
        elif rec['type'] == 'text':
            # Check if adding text exceeds page height
            text_height = min(len(rec['content']) / 20 * 15, 200)
            if current_page_height + text_height + 40 > max_page_height and current_page_texts:
                flush_page()
                current_page_height = 0
            
            current_page_texts.append((idx, rec))
            current_page_height += text_height + 30
    
    # Flush remaining items
    flush_page()
    
    # Footer on last page
    story.append(Spacer(1, 20))
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
    """Generate and send PDF with Gujarati index and smart packing"""
    chat_id = update.effective_chat.id
    
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
                    caption=f"📄 **{title}** Ready! (Cached)\n📸 Total: {len(records)} images" if 'records' in locals() else f"📄 **{title}** Ready! (Cached)"
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

        # Download images with size info
        photo_records = [(idx, r) for idx, r in enumerate(records) if r['type'] == 'photo']
        downloaded_images = {}
        
        if photo_records:
            await status_msg.edit_text(f"📸 {len(photo_records)} ફોટાઓ ડાઉનલોડ કરી રહ્યા છીએ...")
            download_tasks = []
            for idx, rec in photo_records:
                download_tasks.append(fetch_image_fast(context.bot, rec['content'], idx))
            
            results = await asyncio.gather(*download_tasks)
            for idx, path, size in results:
                if path:
                    downloaded_images[idx] = path
                    downloaded_images[f"{idx}_size"] = size

        # Generate PDF
        await status_msg.edit_text("📄 PDF બનાવી રહ્યા છીએ...")
        record_ids = [rec['id'] for rec in records]
        loop = asyncio.get_event_loop()
        pdf_buffer = await loop.run_in_executor(
            executor,
            generate_smart_pdf,
            records,
            downloaded_images,
            title,
            summary,
            record_ids
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
            caption=f"📄 **{title}** Ready! ({len(records)} items)\n📸 Total Images: {len(photo_records)}\n🤖 AI સારાંશ સાથે"
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
        "PDFમાં Gujarati AI સારાંશ + Index ઉમેરાય છે.\n"
        "📋 Index page with all image titles\n"
        "🖼️ Smart page packing for better space\n\n"
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
