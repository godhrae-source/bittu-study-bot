```python
import sqlite3
import json
import csv
from io import BytesIO, StringIO
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler

DB_FILE = "study_data.db"

async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export data in various formats"""
    keyboard = [
        [InlineKeyboardButton("📄 JSON", callback_data="feature_export_json")],
        [InlineKeyboardButton("📊 CSV", callback_data="feature_export_csv")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📤 *Export Data*\nChoose export format:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def export_json(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export as JSON"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM content_store")
    data = cursor.fetchall()
    conn.close()
    
    json_data = json.dumps(data, default=str, indent=2)
    
    await update.message.reply_document(
        document=BytesIO(json_data.encode()),
        filename=f"study_data_{datetime.now().strftime('%Y%m%d')}.json"
    )

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export as CSV"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM content_store")
    data = cursor.fetchall()
    conn.close()
    
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Type', 'Content', 'Topic', 'Timestamp'])
    writer.writerows(data)
    
    await update.message.reply_document(
        document=BytesIO(output.getvalue().encode()),
        filename=f"study_data_{datetime.now().strftime('%Y%m%d')}.csv"
    )

def get_handlers():
    return [
        CommandHandler("export", export_data),
    ]

def get_callback_handlers():
    return {
        "feature_export_json": export_json,
        "feature_export_csv": export_csv,
    }
```
a
