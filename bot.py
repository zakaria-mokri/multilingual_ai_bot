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

# Current, active Gemini models (Aug 2026). Both are multimodal (handle images fine).
PRIMARY_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.6-flash"

# Mechanic-shop persona — this is what makes it feel like a real person, not a generic bot
SYSTEM_PROMPT_BASE = (
    "You are Mo, a senior mechanic at a trusted, family-run auto repair shop, talking to a "
    "customer over Telegram. You've got 20 years of hands-on experience and you talk like it: "
    "direct, warm, a little casual, zero corporate-assistant phrasing. No 'I am an AI' disclaimers, "
    "no excessive hedging. You use real shop terminology but explain it in plain terms as you go, "
    "the way you would to a customer standing at the counter who doesn't know cars.\n\n"
    "When a customer describes a problem: ask the couple of diagnostic questions a real mechanic "
    "would ask before guessing (sound, when it happens, warning lights, mileage, how long it's "
    "been happening) — but don't interrogate them, just enough to give a real answer.\n\n"
    "When a customer sends a photo of a part: look at it like you're actually inspecting it. "
    "Call out specifically what you see (rust, cracks, fluid leaks, wear patterns, corrosion, "
    "damage) and what that tells you. Give a straight read: is this safe to keep driving on, "
    "does it need to come in soon, or is it a tow-it-now situation. If it's something they could "
    "reasonably DIY, say so. If it needs a pro (especially anything brakes, steering, or "
    "suspension-related), say that clearly and don't guess past what the photo can actually tell you.\n\n"
    "Always prioritize safety over confidence — if a photo or description is genuinely ambiguous, "
    "say what you're not sure about rather than making a call you can't back up from what you see.\n\n"
    "Keep replies focused and conversational, not a wall of text. Respond in the user's language."
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
    Score knowledge-table entries by keyword overlap with the question and only pass
    the top matches into the prompt, instead of dumping the whole table every time.
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
    while not stop_event.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)


def build_history_and_prompt(past_msgs, user_query, knowledge_text):
    """Shared prompt assembly for both text and photo messages."""
    system_prompt = (
        f"{SYSTEM_PROMPT_BASE}\n\n"
        f"Relevant reference info from the shop's database:\n{knowledge_text}"
    )

    formatted_prompt_parts = []
    for role, msg in past_msgs:
        prefix = "Customer: " if role == "user" else "Mo: "
        formatted_prompt_parts.append(f"{prefix}{msg}")
    formatted_prompt_parts.append(f"Customer: {user_query}")

    return system_prompt, "\n".join(formatted_prompt_parts)


async def stream_reply(update, context, chat_id, contents, system_prompt):
    """
    Runs the model loop with streaming, editing one Telegram message as text arrives.
    `contents` can be a plain string or a list mixing text + image Part (for photos).
    Returns the final reply text.
    """
    models_to_try = get_model_candidates()
    full_text = ""
    sent_message = await update.effective_message.reply_text("…")

    for model_name in models_to_try:
        try:
            full_text = ""
            last_edit = time.time()

            stream = await client.aio.models.generate_content_stream(
                model=model_name,
                contents=contents,
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
                                chat_id=chat_id, message_id=sent_message.message_id, text=full_text
                            )
                        except Exception:
                            pass
                        last_edit = now

            if full_text.strip():
                break

        except Exception as e:
            logger.error(f"Error streaming from {model_name}: {e}")
            continue

    if not full_text.strip():
        full_text = "Hmm, having trouble reaching my diagnostic tools right now — give it another shot in a moment."

    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=full_text)
    except Exception:
        await update.effective_message.reply_text(full_text)

    return full_text


def save_history(user_id, user_query, reply_text):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)",
            (user_id, "user", user_query),
        )
        cursor.execute(
            "INSERT INTO chat_history (user_id, role, message) VALUES (?, ?, ?)",
            (user_id, "assistant", reply_text),
        )


def get_past_messages(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, message FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 6",
            (user_id,),
        )
        past_msgs = cursor.fetchall()
    past_msgs.reverse()
    return past_msgs


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))

    await update.message.reply_text(
        "Hey, Mo here — what's going on with your car? Tell me what you're hearing, seeing, or "
        "feeling, or just send me a photo of the part and I'll take a look."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_query = update.message.text
    chat_id = update.effective_chat.id

    stop_typing_event = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing_active(context, chat_id, stop_typing_event))

    try:
        past_msgs = get_past_messages(user_id)
        knowledge_text = get_relevant_knowledge(user_query)
        system_prompt, full_user_content = build_history_and_prompt(past_msgs, user_query, knowledge_text)

        reply_text = await stream_reply(update, context, chat_id, full_user_content, system_prompt)
        save_history(user_id, user_query, reply_text)

    except Exception as e:
        logger.error(f"Unhandled error in handle_message: {e}", exc_info=True)
        await update.message.reply_text("Sorry, something went wrong on my end there — try again?")
    finally:
        stop_typing_event.set()
        await typing_task


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Customer sends a photo of a part — Mo looks at it and diagnoses."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    caption = update.message.caption or "What's wrong with this part?"

    stop_typing_event = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing_active(context, chat_id, stop_typing_event))

    try:
        # Grab the largest available size of the photo
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = await tg_file.download_as_bytearray()

        image_part = types.Part.from_bytes(data=bytes(image_bytes), mime_type="image/jpeg")

        past_msgs = get_past_messages(user_id)
        knowledge_text = get_relevant_knowledge(caption)
        system_prompt, text_context = build_history_and_prompt(past_msgs, caption, knowledge_text)

        # Mixed content: conversation text + the actual image
        contents = [text_context, image_part]

        reply_text = await stream_reply(update, context, chat_id, contents, system_prompt)
        save_history(user_id, f"[sent a photo] {caption}", reply_text)

    except Exception as e:
        logger.error(f"Unhandled error in handle_photo: {e}", exc_info=True)
        await update.message.reply_text("Couldn't get a good look at that photo — mind resending it?")
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
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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