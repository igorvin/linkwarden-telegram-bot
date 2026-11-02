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

Private messages are restricted to ALLOWED_TELEGRAM_USER_ID. Normal members of ALLOWED_TELEGRAM_GROUP_ID may submit links. Messages from bots, anonymous administrators, channel identities, and other groups are rejected. Only the original submitter can operate their session, including when another group member is the configured private-chat owner. Replies stay in the original topic using message_thread_id. Optional TELEGRAM_TOPIC_COLLECTIONS maps topic IDs to collection IDs in the configured group. For example, {"9":1} sends topic 9 to collection 1. Bound topics skip the collection picker and keep tag selection and confirmation. Back cannot change a bound collection. Missing or inaccessible bound collections produce an error with retry and cancel. Other topics and private chats retain the collection picker.

Use /bind in the target topic or private chat to assign a collection interactively. Only ALLOWED_TELEGRAM_USER_ID can create or confirm bindings. Choose a collection and click Confirm binding. Cancel and Back do not modify stored settings. The selected collection is checked against the API again at confirmation.

Bindings are stored in SQLite in the existing bot-state volume and take precedence over TELEGRAM_TOPIC_COLLECTIONS for the same chat and topic. They apply to newly submitted URLs. Repeating /bind changes the assignment. Use /unbind in that same topic or private chat to remove the assignment after confirmation. Only the owner can confirm removal. Explicit removal persists and overrides environment defaults, restoring the collection picker. Removal works even when Linkwarden is unavailable. Previously saved links and open URL sessions retain their selections. Each topic and private chat has an independent binding. Existing links and open URL sessions keep their previous selections.

Use /settings in a topic or private chat to view its collection binding and default tags. The menu is restricted to ALLOWED_TELEGRAM_USER_ID and offers collection assignment, removal, default-tag editing, and refresh. It operates only in the chat/topic where it was opened and uses the same owner and callback checks as /bind and /unbind.

Select Default tags, toggle zero or more existing Linkwarden tags, and confirm Save settings. Back and Cancel do not write changes. Defaults persist in the existing SQLite volume, independently of collection bindings. Clear all selected tags and confirm to remove defaults.

New URL sessions and new import previews preselect those tags for every authorized submitter in that scope. Submitters can change their own selection before saving. Back does not reapply tags they deselected. Missing or deleted tags are omitted from new link selections, and edited preferences are checked against the current API list before they are saved. Existing links, open drafts, and confirmed scan jobs retain their chosen tags.

Use /scan in a configured group topic to import old links from a Telegram Desktop JSON export. Only the bot owner can initiate, upload, confirm, resume, or cancel an import. Send /scan, then upload result.json as a document in the same topic. Exports are limited to 10 MiB, 100000 messages, and 10000 unique eligible URLs.

The parser checks the exported chat ID, resolves topic membership from topic-created messages, explicit message_thread_id values, and reply_to_message_id chains, and extracts plain and hidden HTTP/HTTPS links through the existing URL validator. It skips the bot's own exported messages, other topics, and messages without a provable topic. The preview lists included and skipped counts. General and private-chat imports are currently unsupported.

The collection and tag menus use the current topic binding when present. Only clicking Start import creates a durable job and starts saving. All imported URLs use save_once, including duplicate checks and uncertain-outcome handling. Already existing links keep their metadata.

Jobs persist their URL list, chosen metadata, counts, and cursor in the existing SQLite volume. The original export file is processed in memory. The worker processes one URL at a time, pauses on the first API error, and leaves remaining URLs unprocessed. Use /scan to resume or cancel a pending job, including after a restart. A confirmed job may continue in the background while normal bot messages are processed. Only one active job per topic is allowed.

Cancel stops subsequent requests. A request already sent may complete, and saved links remain. Imports do not resume automatically after restart. To retry a cancelled batch, upload the export again and confirm a new job, existing links will be skipped. Avoid removing the state volume.

Sessions expire after one hour or a bot restart. Stale, forged, wrong-message, and wrong-topic callbacks are rejected. Telegram group privacy settings must allow the bot to receive the messages containing URLs.

Collections are read from response on GET /api/v1/collections. Tags are read from data.tags on GET /api/v1/tags, following data.nextCursor with the cursor parameter. Save sends collection: {id} and tags: [{id, name}], then validates response.id. Linkwarden performs native screenshot preservation according to its archive preferences.

Use /search text to find saved links by name, address, description, or tags through the installed Linkwarden search API. Queries are limited to 200 characters. In the authorized group, a topic must have a collection binding and results are restricted to that collection. Group members can search, but only the query author can navigate its menu. In the owner's private chat, search covers all collections visible to the API account.

Results are shown five at a time with Open in Linkwarden buttons, previous and next pages, refresh, and close. API cursors are used for server pagination, including sparse Meilisearch pages. Previous pages use the session cache. Failed page requests can be retried without discarding the current results. Sessions expire after one hour or a restart.

Every navigation callback rechecks authorization, author, chat, message, topic, and the current topic binding. If the binding changes or is removed, the menu requires a new query. Returned collection IDs are also checked locally before group results are displayed. Searches never create or change links.

After an individual Save, the result includes the current collection, tags, and an Open in Linkwarden button pointing to /links/ID. Existing duplicate links display their own metadata. If metadata cannot be read after a successful creation, the confirmed selection is shown and the save remains successful. Failed or uncertain saves do not show a success button.

LINKWARDEN_PUBLIC_URL optionally sets a browser-facing base address. It defaults to LINKWARDEN_API_URL. Use the address reachable from the user's browser when the API address is internal to Docker. URL credentials, query parameters, fragments, and invalid addresses are rejected for buttons. The API token is never included in the button, and the Linkwarden web login still controls access.

The bot searches existing visible links using GET /api/v1/links with searchQueryString and cursor, and compares exact URL strings. If the URL exists, it reports the existing link without moving or retagging it. A SQLite journal in the bot-state Docker volume atomically claims each URL for the configured API account. Repeated confirmations, repeated messages, concurrent submissions, and container restarts reuse the recorded result. Keep this volume when replacing the container.

A timeout, server error, or malformed response after POST has an unknown outcome. The bot retains that claim and checks Linkwarden before another attempt. If the link cannot be found, it blocks another POST and asks for manual verification. GET failures and definite 4xx rejections allow a later attempt. The journal deduplicates exact URL strings. Redirect aliases and simultaneous writes from clients outside this bot require Linkwarden's own duplicate prevention. Native API duplicate checking is not an atomic idempotency mechanism.

Run tests independently of the live bot:

```bash
python -m compileall -q bot.py linkwarden.py selection.py submissions.py scan_export.py scan_jobs.py
python -m pytest -q -o asyncio_default_fixture_loop_scope=function
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | Yes | - | Telegram bot token from BotFather |
| `LINKWARDEN_API_URL` | Yes | - | Linkwarden server URL |
| `LINKWARDEN_PUBLIC_URL` | No | API base URL | Browser-facing Linkwarden address for open buttons |
| `LINKWARDEN_API_KEY` | Yes | - | Linkwarden API key |
| `ALLOWED_TELEGRAM_USER_ID` | Yes | - | Numeric Telegram user ID allowed in private chat |
| `ALLOWED_TELEGRAM_GROUP_ID` | No | `0` | Authorized group ID, 0 disables groups |
| `TELEGRAM_TOPIC_COLLECTIONS` | No | `{}` | JSON topic-to-collection mapping in the authorized group |
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
