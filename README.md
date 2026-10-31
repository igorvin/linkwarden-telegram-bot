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

Send HTTP/HTTPS links in your private chat with the bot. Each link gets its own collection picker. Select any existing tags, review the selection, and confirm to save. Drafts expire after one hour or a bot restart. Linkwarden handles screenshots according to its archive settings.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | Yes | - | Telegram bot token from BotFather |
| `LINKWARDEN_API_URL` | Yes | - | Linkwarden server URL |
| `LINKWARDEN_API_KEY` | Yes | - | Linkwarden API key |
| `ALLOWED_TELEGRAM_USER_ID` | Yes | - | Numeric Telegram user ID allowed in private chat |
| `LOG_LEVEL` | No | `WARNING` | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| `RATE_LIMIT_THRESHOLD` | No | `10` | Max messages per user per window |
| `RATE_LIMIT_WINDOW` | No | `60` | Rate limit window in seconds |
| `MAX_MESSAGE_SIZE` | No | `51200` | Max message size in bytes |
| `MAX_LINKS_PER_MESSAGE` | No | `10` | Max links per message |
| `ALLOW_LOCAL_LINKWARDEN` | No | `false` | Allow localhost/private API URLs |

## Badges

![Build Status](https://img.shields.io/github/actions/workflow/status/igorvin/linkwarden-telegram-bot/docker-publish.yml?branch=main)
![License](https://img.shields.io/github/license/igorvin/linkwarden-telegram-bot)
