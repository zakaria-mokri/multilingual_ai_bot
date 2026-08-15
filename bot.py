import os
import time
import asyncio
import sqlite3
import logging
from dotenv import load_dotenv
from google import genai
from google.genai import types
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

# Current, active Gemini models (Aug 2026). Lite first = fast; fallback if it fails.
PRIMARY_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.6-flash"

# Scoped system prompt — keeps the bot on-topic instead of drifting into general chat
SYSTEM_PROMPT_BASE = (
    "You are a focused car emergency and roadside-assistance assistant on Telegram. "
    "You help with: breakdowns, warning lights, flat tires, jump-starts, overheating, "
    "accident steps, basic diagnostics, and finding help nearby. "
    "If the user asks about something unrelated to cars/emergencies, briefly and politely "
    "redirect them back to what you can help with — don't answer unrelated topics at length. "
    "Keep replies short, clear, and actionable — this may be a stressful moment for the user. "
    "Respond in the user's language. Use plain text formatting (no heavy markdown)."
)


def get_db_connection():
    return sqlite3.connect("bot_knowledge.db")


def init_db():
    """Ensure database tables exist before performing queries."""
    with get_db_connection() as conn:
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


def get_relevant_knowledge(user_query: str, limit: int = 5) -> str:
    """
    Instead of dumping the whole knowledge table into every prompt (slow, unfocused),
    score entries by keyword overlap with the question and only pass the top matches.
    Good for small/medium tables. If yours grows past a few hundred rows, swap this
    for SQLite FTS5 or embeddings-based retrieval.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT category, content FROM knowledge")
        all_knowledge = cursor.fetchall()

    if not all_knowledge:
        return "No custom database entries found yet."

    query_words = {w.lower() for w in user_query.split() if len(w) > 3}
    if not query_words:
        return "No custom database entries found yet."

    scored = []
    for category, content in all_knowledge:
        haystack = f"{category} {content}".lower()
        score = sum(1 for w in query_words if w in haystack)
        if score > 0:
            scored.append((score, category, content))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    if not top:
        return "No directly relevant database entries found for this question."

    return "\n".join(f"- [{cat}]: {content}" for _, cat, content in top)


def get_model_candidates():
    """Dynamically discover valid models available for this API key, preferring the fast ones."""
    try:
        discovered = [m.name.replace("models/", "") for m in client.models.list()]
        logger.info(f"Discovered active models for API key: {discovered}")
        preferred = [m for m in (PRIMARY_MODEL, FALLBACK_MODEL) if m in discovered]
        if preferred:
            return preferred
        if discovered:
            return discovered[:2]
    except Exception as e:
        logger.error(f"Could not list models: {e}")

    return [PRIMARY_MODEL, FALLBACK_MODEL]


async def keep_typing_active(context, chat_id, stop_event):
    """Keeps sending 'typing' action every 4 seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))

    await update.message.reply_text(
        "Hi! I'm your car emergency assistant. Tell me what's going on with your car "
        "and I'll help you figure out what to do."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_query = update.message.text
    chat_id = update.effective_chat.id

    # Start continuous typing indicator loop
    stop_typing_event = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing_active(context, chat_id, stop_typing_event))

    sent_message = None
    full_text = ""

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT role, message FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 6",
                (user_id,),
            )
            past_msgs = cursor.fetchall()

        past_msgs.reverse()

        db_context_str = get_relevant_knowledge(user_query)
        system_prompt = (
            f"{SYSTEM_PROMPT_BASE}\n\n"
            f"Relevant reference info from the connected database:\n{db_context_str}"
        )

        formatted_prompt_parts = []
        for role, msg in past_msgs:
            prefix = "User: " if role == "user" else "Assistant: "
            formatted_prompt_parts.append(f"{prefix}{msg}")
        formatted_prompt_parts.append(f"User Question: {user_query}")
        full_user_content = "\n".join(formatted_prompt_parts)

        models_to_try = get_model_candidates()

        # Stream the reply and edit the Telegram message as text arrives —
        # feels fast even before the full answer is generated
        for model_name in models_to_try:
            try:
                full_text = ""
                sent_message = await update.message.reply_text("…")
                last_edit = time.time()

                stream = await client.aio.models.generate_content_stream(
                    model=model_name,
                    contents=full_user_content,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        thinking_config=types.ThinkingConfig(thinking_level="low"),
                    ),
                )

                async for chunk in stream:
                    if chunk.text:
                        full_text += chunk.text
                        now = time.time()
                        if now - last_edit > 1.0 and full_text.strip():
                            try:
                                await context.bot.edit_message_text(
                                    chat_id=chat_id,
                                    message_id=sent_message.message_id,
                                    text=full_text,
                                )
                            except Exception:
                                pass
                            last_edit = now

                if full_text.strip():
                    break  # success, no need to try the fallback model

            except Exception as e:
                logger.error(f"Error streaming from {model_name}: {e}")
                continue

        if not full_text.strip():
            full_text = "The AI servers are busy right now. Please try your message again in a moment."

        if sent_message:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=sent_message.message_id, text=full_text
                )
            except Exception:
                await update.message.reply_text(full_text)
        else:
            await update.message.reply_text(full_text)

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)",
                (user_id, "user", user_query),
            )
            cursor.execute(
                "INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)",
                (user_id, "assistant", full_text),
            )

    except Exception as e:
        logger.error(f"Unhandled error in handle_message: {e}", exc_info=True)
        if sent_message:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    text="Sorry, an error occurred while processing your request.",
                )
            except Exception:
                await update.message.reply_text("Sorry, an error occurred while processing your request.")
        else:
            await update.message.reply_text("Sorry, an error occurred while processing your request.")
    finally:
        stop_typing_event.set()
        await typing_task


def main():
    if not TOKEN or not GEMINI_API_KEY:
        logger.error("Error: Missing tokens in environment variables.")
        return

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