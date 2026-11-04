# Linkwarden Telegram Bot

[Read the Russian guide](README.ru.md)

Save links from Telegram to your own Linkwarden instance. Choose a collection and tags in chat, organize bookmarks by group topic, and search your saved links without leaving Telegram.

**Send a link → choose a collection → select tags → Done and save.**

[What's new](#whats-new-in-this-fork) · [Commands](#commands) · [Quick start](#quick-start-with-docker-compose) · [Original project](https://github.com/orguetta/linkwarden-telegram-bot)

## Built on the original project

This fork is based on **[orguetta/linkwarden-telegram-bot](https://github.com/orguetta/linkwarden-telegram-bot)**. Thank you to **orguetta** and the original contributors for the Telegram-to-Linkwarden integration, URL handling, rate limiting, and URL safety checks.

This repository extends that foundation with interactive collection and tag selection, group topics, imports, search, and persistent state. The original [Apache 2.0 license](LICENSE) is preserved.

## What's new in this fork

The comparison below uses the original version this fork started from: [`bfd10d8`](https://github.com/orguetta/linkwarden-telegram-bot/tree/bfd10d807399f392b41f2f38dec9fd50f3a40391).

| Task | Original version | This fork |
|---|---|---|
| Choose a collection | One collection configured through `LINKWARDEN_COLLECTION_ID` | Fetch available collections from the API and choose with inline buttons |
| Save a link | Automatically on receipt | Save when you finish selecting tags and click Done and save |
| Add tags | No interactive tag picker | Select zero, one, or multiple existing tags |
| Create a collection or tag | No creation flow in Telegram | Create by name, then return to the selection menu |
| Organize group topics | No topic binding commands | `/bind`, `/unbind`, independent settings, and default tags per topic |
| Import older links | No Telegram export import | `/scan` with preview, confirmation, resume, and cancellation |
| Find saved links | No search command | Paginated `/search` results with buttons to open bookmarks |
| Control access | No owner and submitter-bound session model | Owner-only private chat, one allowed group, and buttons restricted to the requester |
| Handle repeated submissions | No persistent submission journal | SQLite journal, existing-link checks, and no repeated POST while an outcome is uncertain |
| Check preservation | No interactive archive status | Linkwarden archive status, automatic updates, and manual refresh |

## Everyday use

Button names below are English descriptions; the bot's current interface uses Russian labels.

### Save a link

1. Send a URL in your private chat with the bot or in the configured group.
2. Choose a collection. If the topic already has a collection binding, the bot skips straight to tags.
3. Select any tags you want, or leave them all unchecked.
4. Click **Done and save**. The link is saved immediately, with no additional confirmation screen.

The result includes an **Open in Linkwarden** button and archive status. A message containing several URLs gets a separate menu for each link. **Back** lets you change the collection when the topic is unbound; **Cancel** discards the draft. Replies stay in the original group topic.

### Create a collection or tag

The bot owner sees **New collection** and **New tag** in their respective menus. Click the button, then **reply to the bot's prompt with a name**.

Use Telegram's Reply action on the prompt to enter the name. The bot removes the prompt after creation or cancellation so it does not keep requesting a reply.

A new collection is selected automatically. After creating a tag, the bot sends a fresh menu below your reply: the new tag is checked and your previous selections remain. You can add another tag, change your selection, or save the link.

Creating a collection or tag does not save the URL. Cancelling a draft does not delete items already created in Linkwarden.

### Organize a group topic

For example, open your Fishing topic, run `/bind`, and select your fishing collection. New links in that topic will go directly to tag selection for that collection.

Use `/settings` to choose default tags. New drafts preselect those tags, and submitters can change the selection. `/unbind` removes only the current topic's or private chat's binding after confirmation.

## Commands

| Command | What it does | Who can use it |
|---|---|---|
| `/start` | Show a short introduction | Authorized users |
| `/bind` | Bind the current topic or private chat to a collection | Owner |
| `/unbind` | Remove the current topic's or private chat's binding | Owner |
| `/settings` | Manage the collection binding and default tags | Owner |
| `/search text` | Search bookmarks, browse result pages, and open links | Authorized users |
| `/scan` | Import a Telegram JSON export; resume or cancel an import | Owner, in a group topic other than General |

**Search:** group searches are restricted to the collection bound to the current topic. In the owner's private chat, searches cover collections visible to the API account.

**Import:** the bot cannot read old chat history directly. Run `/scan`, upload `result.json` from a Telegram Desktop export in the same topic, review the collection and tags, then click **Start import**. Existing links are skipped. Imports pause on an API error and can be resumed with `/scan`. The file limit is 10 MiB. See the [detailed Russian guide](README.ru.md) for topic detection and other import limits.

## Quick start with Docker Compose

You need a running Linkwarden instance and API token, a Telegram bot and its token, your numeric Telegram user ID, and Docker with Compose.

### 1. Get the code

```bash
git clone https://github.com/igorvin/linkwarden-telegram-bot.git
cd linkwarden-telegram-bot
cp example.env .env
```

### 2. Configure `.env`

```dotenv
TELEGRAM_TOKEN=your-telegram-bot-token
LINKWARDEN_API_URL=https://linkwarden.example.com
LINKWARDEN_API_KEY=your-linkwarden-api-token
ALLOWED_TELEGRAM_USER_ID=123456789

# Optional: one allowed group. Set to 0 to disable group access.
ALLOWED_TELEGRAM_GROUP_ID=0
```

To use a group, enter its numeric ID and configure Telegram Privacy Mode so the bot receives messages containing links. Private chat is restricted to `ALLOWED_TELEGRAM_USER_ID`. This fork does not require `LINKWARDEN_COLLECTION_ID`: choose collections through the menu or `/bind`.

### 3. Start the bot

For a new installation:

```bash
docker compose up --build -d
```

Open the bot in Telegram, send `/start`, then send a link. When updating an existing installation, back up the state database first and remember that restarting ends open selection sessions.

### Main settings

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Telegram bot token; required |
| `LINKWARDEN_API_URL` | Linkwarden base URL without `/api/v1`; required |
| `LINKWARDEN_API_KEY` | Linkwarden API token; required |
| `ALLOWED_TELEGRAM_USER_ID` | Numeric owner ID; required |
| `ALLOWED_TELEGRAM_GROUP_ID` | One allowed group; defaults to `0` |
| `LINKWARDEN_PUBLIC_URL` | Browser-facing address for open buttons, if different from the API address |
| `TELEGRAM_TOPIC_COLLECTIONS` | Optional initial topic bindings as JSON; saved command settings take precedence |
| `BOT_STATE_PATH` | SQLite path; Compose uses `/app/data/submissions.sqlite3` |

Bindings, default tags, the submission journal, and import jobs are stored in the persistent `bot-state` volume. **Keep this volume when updating.** Never publish `.env`, tokens, or chat exports.

The [detailed Russian guide](README.ru.md) covers API behavior, persistence, and operational limits.

## Good to know

- Members of the configured group can save and search links. Collection and tag creation, settings, and imports are owner-only. Only the original requester can operate a menu.
- Selection sessions expire after one hour or a bot restart. Settings and import jobs persist; imports must be resumed manually after a restart.
- Duplicate checks compare exact URL strings. Existing bookmarks keep their metadata. In groups, duplicate responses do not reveal the existing bookmark's ID, collection, tags, or archive status. If an API write has an uncertain outcome, the bot checks for the object before allowing further action.
- Screenshots and other archives are created by **Linkwarden itself**, according to its preservation settings. The bot reports API status and does not inspect screenshot content.
- An unavailable archive may mean that a format is disabled or that preservation failed on the Linkwarden side.

## Development checks

Install `requirements.txt` in a separate Python environment, then run:

```bash
python -m compileall -q bot.py linkwarden.py selection.py submissions.py scan_export.py scan_jobs.py
python -m pytest -q -p no:cacheprovider -o asyncio_default_fixture_loop_scope=function
```

Tests cover API handling, authorization and callbacks, collection and tag selection, creation, bindings, settings, search, imports, and duplicate-submission handling.

## License and credits

[Apache License 2.0](LICENSE). Original project: [orguetta/linkwarden-telegram-bot](https://github.com/orguetta/linkwarden-telegram-bot). This fork preserves the original history and license while adding the features described above.
