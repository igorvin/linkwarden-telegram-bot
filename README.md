# Linkwarden Telegram Bot

A Telegram bot that integrates with Linkwarden to save bookmarks directly from Telegram.

## Features

- Save links to your Linkwarden collection
- Rate limiting to prevent abuse
- SSRF protection for safe URL handling
- Configurable logging and limits

## Installation

### Using Docker

#### 1. Clone the repository

```bash
git clone https://github.com/igorvin/linkwarden-telegram-bot.git
cd linkwarden-telegram-bot
```

#### 2. Copy the example env file

```bash
cp example.env .env
```

#### 3. Edit the .env file and set the required environment variables

```bash
TELEGRAM_TOKEN=your_bot_token
LINKWARDEN_API_URL=https://your-linkwarden-instance.com
LINKWARDEN_API_KEY=your_api_key
ALLOWED_TELEGRAM_USER_ID=your_telegram_user_id
```

#### 4. Build and run with Docker Compose

```bash
docker compose up --build -d
```

### Manual Installation

```bash
pip install -r requirements.txt
```

### Set up environment variables

```bash
export TELEGRAM_TOKEN=your_bot_token
export LINKWARDEN_API_URL=https://your-linkwarden-instance.com
export LINKWARDEN_API_KEY=your_api_key
export ALLOWED_TELEGRAM_USER_ID=your_telegram_user_id
```

### Run the bot

```bash
python bot.py
```

## Usage

### Adding Links

Send HTTP/HTTPS links in a private chat or the configured group. The bot fetches collections and tags from Linkwarden, then presents a separate selection session for each URL. Choose one collection, toggle zero or more tags, review the URL and selections, and click Save. Back returns to the previous step and Cancel discards the draft.

Private messages are restricted to ALLOWED_TELEGRAM_USER_ID. Normal members of ALLOWED_TELEGRAM_GROUP_ID may submit links. Messages from bots, anonymous administrators, channel identities, and other groups are rejected. Only the original submitter can operate their session, including when another group member is the configured private-chat owner. Replies stay in the original topic using message_thread_id. Topics are not mapped to collection or tag IDs.

Sessions expire after one hour or a bot restart. Stale, forged, wrong-message, and wrong-topic callbacks are rejected. Telegram group privacy settings must allow the bot to receive the messages containing URLs.

Collections are read from response on GET /api/v1/collections. Tags are read from data.tags on GET /api/v1/tags, following data.nextCursor with the cursor parameter. Save sends collection: {id} and tags: [{id, name}], then validates response.id. Linkwarden performs native screenshot preservation according to its archive preferences.

The bot searches existing visible links using GET /api/v1/links with searchQueryString and cursor, and compares exact URL strings. If the URL exists, it reports the existing link without moving or retagging it. A SQLite journal in the bot-state Docker volume atomically claims each URL for the configured API account. Repeated confirmations, repeated messages, concurrent submissions, and container restarts reuse the recorded result. Keep this volume when replacing the container.

A timeout, server error, or malformed response after POST has an unknown outcome. The bot retains that claim and checks Linkwarden before another attempt. If the link cannot be found, it blocks another POST and asks for manual verification. GET failures and definite 4xx rejections allow a later attempt. The journal deduplicates exact URL strings. Redirect aliases and simultaneous writes from clients outside this bot require Linkwarden's own duplicate prevention. Native API duplicate checking is not an atomic idempotency mechanism.

Run tests independently of the live bot:

```bash
python -m compileall -q bot.py linkwarden.py selection.py submissions.py
python -m pytest -q -o asyncio_default_fixture_loop_scope=function
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | Yes | - | Telegram bot token from BotFather |
| `LINKWARDEN_API_URL` | Yes | - | Linkwarden server URL |
| `LINKWARDEN_API_KEY` | Yes | - | Linkwarden API key |
| `ALLOWED_TELEGRAM_USER_ID` | Yes | - | Numeric Telegram user ID allowed in private chat |
| `ALLOWED_TELEGRAM_GROUP_ID` | No | `0` | Authorized group ID, 0 disables groups |
| `BOT_STATE_PATH` | No | `data/submissions.sqlite3` | Durable submission journal, /app/data/submissions.sqlite3 in Compose |
| `LOG_LEVEL` | No | `WARNING` | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `RATE_LIMIT_THRESHOLD` | No | `10` | Max messages per user per window |
| `RATE_LIMIT_WINDOW` | No | `60` | Rate limit window in seconds |
| `MAX_MESSAGE_SIZE` | No | `51200` | Max message size in bytes |
| `MAX_LINKS_PER_MESSAGE` | No | `10` | Max links per message |
| `ALLOW_LOCAL_LINKWARDEN` | No | `false` | Allow localhost/private API URLs |

## Badges

![Build Status](https://img.shields.io/github/actions/workflow/status/igorvin/linkwarden-telegram-bot/docker-publish.yml?branch=main)
![License](https://img.shields.io/github/license/igorvin/linkwarden-telegram-bot)
