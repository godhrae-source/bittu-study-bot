```python
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler

DB_FILE = "study_data.db"

async def show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show comprehensive analytics"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Basic stats
    cursor.execute("SELECT COUNT(*) FROM content_store")
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT topic) FROM content_store")
    topics = cursor.fetchone()[0]
    
    # Daily activity
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM content_store WHERE date(timestamp) = ?", (today,))
    today_count = cursor.fetchone()[0]
    
    # Weekly activity
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM content_store WHERE date(timestamp) >= ?", (week_ago,))
    week_count = cursor.fetchone()[0]
    
    # Top topics
    cursor.execute("""
        SELECT topic, COUNT(*) as count 
        FROM content_store 
        GROUP BY topic 
        ORDER BY count DESC 
        LIMIT 5
    """)
    top_topics = cursor.fetchall()
    
    conn.close()
    
    # Build message
    msg = f"📊 *Your Study Analytics*\n\n"
    msg += f"📚 Total Notes: {total}\n"
    msg += f"🏷️ Topics: {topics}\n"
    msg += f"📅 Today: {today_count} entries\n"
    msg += f"📆 This Week: {week_count} entries\n\n"
    
    if top_topics:
        msg += "*Top Topics:*\n"
        for topic, count in top_topics:
            bar = "▰" * min(count, 10) + "▱" * max(0, 10 - min(count, 10))
            msg += f"• {topic}: {bar} {count}\n"
    
    keyboard = [
        [InlineKeyboardButton("🔙 Back", callback_data="menu_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

def get_handlers():
    return [
        CommandHandler("stats", show_analytics),
    ]

def get_callback_handlers():
    return {
        "feature_analytics": show_analytics,
    }
```
