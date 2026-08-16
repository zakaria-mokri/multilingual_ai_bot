# 🔧 Mo — AI Mechanic Bot

A Telegram bot that talks like an experienced mechanic at your local shop — not a generic AI assistant. Describe a car problem or send a photo of a part, and Mo diagnoses it, tells you what's safe to drive on, and when to bring it in.

Powered by Google's Gemini API, deployed on Render, source of truth on GitHub.

## Features

- 💬 **Real mechanic persona** — direct, plain-language answers instead of corporate AI-speak
- 📸 **Photo diagnosis** — send a picture of a part (brake pad, belt, leak, etc.) and get a read on what's wrong and how urgent it is
- ⚡ **Fast, streamed replies** — responses appear as they're generated instead of one big delayed dump
- 🧠 **Conversation memory** — remembers your last few messages per chat for context (reset anytime with `/start`)
- 📚 **Custom knowledge lookup** — pulls relevant entries from a local knowledge base (shop-specific info, common fixes, etc.) into every answer
- 🌍 **Multilingual** — replies in whatever language you write in
- 🔁 **Auto-deploy** — push to `main` and Render rebuilds and redeploys automatically

## Tech Stack

- **Python 3.11**
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — Telegram integration
- [google-genai](https://github.com/googleapis/python-genai) — Gemini API client
- **SQLite** — chat history + knowledge base storage
- **Docker** — containerized for deployment
- **Render** — hosting (webhook-based web service)

## How It Works

- `bot.py` runs a Telegram bot using webhooks (via Render) or polling (locally)
- Every text message pulls the last few turns of conversation + relevant knowledge-base entries, then streams a reply from Gemini using a mechanic-persona system prompt
- Every photo message is sent directly to Gemini's vision-capable model alongside the caption (or a default prompt) for visual diagnosis
- Chat history is stored per-user in SQLite and wiped with `/start`

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/zakaria-mokri/ai-mechanic-bot.git
cd ai-mechanic-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment variables

Create a `.env` file in the project root:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
GEMINI_API_KEY=your_gemini_api_key
```

- Get a Telegram bot token from [@BotFather](https://t.me/BotFather)
- Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/)

### 4. Run locally

```bash
python bot.py
```

With no `RENDER_EXTERNAL_URL` set, the bot runs in local polling mode — no webhook setup needed for local testing.

## Deployment (Render)

This repo is set up to deploy as a Render **Web Service** using the included `Dockerfile`:

1. Connect your GitHub repo to a new Render Web Service
2. Set the environment variables (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) in the Render dashboard
3. Render builds the Docker image and runs `python bot.py` automatically
4. Once live, the bot switches to webhook mode using Render's `RENDER_EXTERNAL_URL`

Every `git push origin main` triggers an automatic rebuild and redeploy — no manual steps needed unless you change an environment variable (then use **Manual Deploy → Deploy latest commit**).

## Project Structure

```
.
├── bot.py              # Main bot logic
├── Dockerfile           # Container build config
├── Procfile              # Process definition
├── requirements.txt      # Python dependencies
├── runtime.txt           # Python version pin
└── bot_knowledge.db       # SQLite database (knowledge + chat history)
```

## Commands

| Command  | Description                          |
|----------|---------------------------------------|
| `/start` | Greets you and clears your chat history for a fresh conversation |

## Notes

- The knowledge base (`knowledge` table) can be populated with shop-specific info — entries are matched to each question by keyword relevance rather than dumped wholesale, so responses stay focused even as the table grows
- For anything safety-critical (brakes, steering, suspension), the bot is instructed to flag uncertainty rather than guess — always confirm with a real mechanic before acting on a diagnosis
