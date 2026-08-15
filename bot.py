import os
import asyncio
import sqlite3
import logging
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Render cloud settings
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

def get_db_connection():
    return sqlite3.connect("bot_knowledge.db")

def init_db():
    """Ensure database tables exist before performing queries."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT,
            content TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text("Hello! I am your cloud-hosted AI assistant connected to your database. Ask me anything!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_query = update.message.text

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT category, content FROM knowledge")
        all_knowledge = cursor.fetchall()
        
        db_context_str = "\n".join([f"- [{cat}]: {content}" for cat, content in all_knowledge]) if all_knowledge else "No custom database entries found yet."

        cursor.execute("SELECT role, message FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 6", (user_id,))
        past_msgs = cursor.fetchall()
        conn.close()

        past_msgs.reverse()

        system_prompt = (
            "You are a helpful, brilliant AI assistant on Telegram. "
            "Here is internal data from the user's connected database that you can use to answer questions accurately:\n"
            f"{db_context_str}\n\n"
            "Respond fluently in the user's language and keep formatting clean."
        )

        ai_reply = None
        # Using current, official model identifiers
        models_to_try = ['gemini-1.5-flash', 'gemini-1.5-pro']

        for model_name in models_to_try:
            success = False
            for attempt in range(2):
                try:
                    contents = [f"System Instructions: {system_prompt}"]
                    for role, msg in past_msgs:
                        prefix = "User: " if role == "user" else "Assistant: "
                        contents.append(prefix + msg)
                    contents.append(f"User Question: {user_query}")

                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                    )
                    ai_reply = response.text
                    success = True
                    break
                except Exception as e:
                    logger.error(f"Error querying {model_name} (attempt {attempt+1}): {e}")
                    await asyncio.sleep(1)  # Non-blocking async sleep
                    continue
            if success:
                break

        if not ai_reply:
            ai_reply = "The AI servers are busy right now. Please try your message again in a moment."

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)", (user_id, "user", user_query))
        cursor.execute("INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)", (user_id, "assistant", ai_reply))
        conn.commit()
        conn.close()

        # Send response without strict Markdown parsing to avoid crashes on special formatting characters
        await update.message.reply_text(ai_reply)

    except Exception as e:
        logger.error(f"Unhandled error in handle_message: {e}", exc_info=True)
        await update.message.reply_text("Sorry, an error occurred while processing your request.")

def main():
    if not TOKEN or not GEMINI_API_KEY:
        print("❌ Error: Missing tokens in .env file.")
        return

    # Initialize SQLite database tables
    init_db()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if RENDER_EXTERNAL_URL:
        logger.info(f"Starting cloud webhook on port {PORT}...")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{RENDER_EXTERNAL_URL}/{TOKEN}"
        )
    else:
        logger.info("Starting local polling...")
        application.run_polling()

if __name__ == "__main__":
    main()