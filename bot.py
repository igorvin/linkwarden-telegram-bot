import os
import logging
import time
import asyncio
import sys
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, CallbackContext
from telegram.error import Conflict, TelegramError, TimedOut, NetworkError
from linkwarden import ApiError, get_collections, get_tags, save_link
from selection import Draft
import re
from urllib.parse import urlparse
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta

# Set up logging
logging_level = os.environ.get('LOG_LEVEL', 'WARNING').upper()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging_level)
logger = logging.getLogger(__name__)

# Environment variables - validate at startup
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
LINKWARDEN_API_URL = os.environ.get('LINKWARDEN_API_URL')
LINKWARDEN_API_KEY = os.environ.get('LINKWARDEN_API_KEY')
ALLOWED_TELEGRAM_USER_ID = os.environ.get('ALLOWED_TELEGRAM_USER_ID')

if not ALLOWED_TELEGRAM_USER_ID or not ALLOWED_TELEGRAM_USER_ID.isdecimal():
    logger.error("Invalid or missing ALLOWED_TELEGRAM_USER_ID")
    sys.exit(1)

ALLOWED_TELEGRAM_USER_ID = int(ALLOWED_TELEGRAM_USER_ID)


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    authorized = (
        user is not None
        and chat is not None
        and user.id == ALLOWED_TELEGRAM_USER_ID
        and chat.type == "private"
    )

    if not authorized:
        logger.warning(
            "Unauthorized Telegram request from user_id=%s",
            user.id if user else None
        )

    return authorized
# Validate required environment variables
if not all([TELEGRAM_TOKEN, LINKWARDEN_API_URL, LINKWARDEN_API_KEY]):
    logger.error("Missing required environment variables: TELEGRAM_TOKEN, LINKWARDEN_API_URL, LINKWARDEN_API_KEY")
    sys.exit(1)

# Rate limiting: max 10 messages per user per minute
RATE_LIMIT_THRESHOLD = int(os.environ.get('RATE_LIMIT_THRESHOLD', 10))
RATE_LIMIT_WINDOW = int(os.environ.get('RATE_LIMIT_WINDOW', 60))  # seconds
user_message_history = defaultdict(list)

# Message size limit (50KB)
MAX_MESSAGE_SIZE = int(os.environ.get('MAX_MESSAGE_SIZE', 51200))

# SSRF protection: validate Linkwarden API URL
def validate_api_url() -> None:
    """Validate that LINKWARDEN_API_URL is safe and well-formed."""
    try:
        parsed = urlparse(LINKWARDEN_API_URL)
        if parsed.scheme not in ('http', 'https'):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValueError("Invalid URL: no hostname")
        # Reject private/local APIs unless explicitly enabled
        if parsed.hostname in ('localhost', '127.0.0.1', '0.0.0.0', '::1'):  # nosec B104
            allow_local = os.environ.get('ALLOW_LOCAL_LINKWARDEN', 'false').lower() == 'true'
            if not allow_local:
                raise ValueError("Local Linkwarden instances not allowed. Set ALLOW_LOCAL_LINKWARDEN=true to enable.")
    except Exception as e:
        logger.error(f"Invalid LINKWARDEN_API_URL: {e}")
        sys.exit(1)

validate_api_url()

async def start(update: Update, context: CallbackContext) -> None:
    if not is_authorized(update):
        return
    await context.bot.send_message(chat_id=update.effective_chat.id, 
                                   text="Пришлите ссылку, выберите коллекцию и теги, затем подтвердите сохранение в Linkwarden.")

def extract_links(text: str) -> list:
    """Extract URLs from text using urllib.parse for better validation."""
    import ipaddress
    
    # Simple pattern to identify potential URLs
    url_pattern = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')
    matches = url_pattern.findall(text)
    
    valid_urls = []
    for url in matches:
        try:
            # Validate URL structure
            parsed = urlparse(url)
            
            # Reject non-http/https
            if parsed.scheme not in ('http', 'https'):
                continue
            
            # Reject URLs without hostname
            if not parsed.hostname:
                continue
            
            hostname = parsed.hostname.lower()
            
            # Reject private/local addresses (SSRF protection)
            blocked_hosts = {
                'localhost', '127.0.0.1', '0.0.0.0', '::1', '::',  # nosec B104
                '169.254.169.254'  # AWS metadata service
            }
            
            if hostname in blocked_hosts:
                logger.debug(f"Rejected blocked hostname: {hostname}")
                continue
            
            # Reject private IP ranges
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    logger.debug(f"Rejected private IP: {hostname}")
                    continue
            except ValueError:
                # Not an IP, continue with hostname validation
                pass
            
            # Additional validation: reject known internal TLDs
            if hostname.endswith(('.local', '.internal', '.localhost', '.test', '.invalid')):
                logger.debug(f"Rejected internal TLD: {hostname}")
                continue
            
            valid_urls.append(url)
        except Exception as e:
            logger.debug(f"Invalid URL rejected: {e}")
            continue
    
    return valid_urls

def check_rate_limit(user_id: int) -> bool:
    """Check if user has exceeded rate limit."""
    now = datetime.now()
    cutoff_time = now - timedelta(seconds=RATE_LIMIT_WINDOW)
    
    # Remove old messages outside the window
    user_message_history[user_id] = [
        msg_time for msg_time in user_message_history[user_id]
        if msg_time > cutoff_time
    ]
    
    # Check if over limit
    if len(user_message_history[user_id]) >= RATE_LIMIT_THRESHOLD:
        return False
    
    # Add current message
    user_message_history[user_id].append(now)
    return True

async def handle_message(update: Update, context: CallbackContext) -> None:
    """Handle incoming messages with rate limiting and validation."""
    if not is_authorized(update):
        return
    user_id = update.effective_user.id
    
    # Rate limiting check
    if not check_rate_limit(user_id):
        await send_message_with_retry(update, context, "⚠️ Rate limit exceeded. Max 10 messages per minute.")
        logger.warning(f"User {user_id} exceeded rate limit")
        return
    
    # Message size validation
    message = update.message.text
    if not message or len(message) > MAX_MESSAGE_SIZE:
        await send_message_with_retry(update, context, "⚠️ Message too large or empty. Please send smaller messages.")
        return
    
    # Check for suspicious patterns (basic injection detection)
    if any(pattern in message.lower() for pattern in ['javascript:', 'data:', 'vbscript:', 'file://']):
        await send_message_with_retry(update, context, "⚠️ Suspicious content detected. Only HTTP/HTTPS URLs are supported.")
        logger.warning(f"User {user_id} attempted to send suspicious content")
        return

    links = extract_links(message)
    
    if not links:
        await send_message_with_retry(update, context, "No links found in the message.")
        return

    # Limit number of links per message (prevent spam)
    MAX_LINKS_PER_MESSAGE = int(os.environ.get('MAX_LINKS_PER_MESSAGE', 10))
    if len(links) > MAX_LINKS_PER_MESSAGE:
        await send_message_with_retry(
            update, context,
            f"⚠️ Too many links. Maximum {MAX_LINKS_PER_MESSAGE} links per message."
        )
        logger.warning(f"User {user_id} sent {len(links)} links (limit: {MAX_LINKS_PER_MESSAGE})")
        return

    drafts = active_drafts(context)
    collections, error = await load_collections()
    for link in links:
        if len(link) > 2048:
            await send_message_with_retry(update, context, "Ссылка длиннее 2048 символов.")
            continue
        draft = Draft(link, collections, error=error)
        sent = await context.bot.send_message(chat_id=update.effective_chat.id, **draft.view())
        drafts[sent.message_id] = draft


def active_drafts(context):
    drafts = context.user_data.setdefault("drafts", {})
    now = time.monotonic()
    for message_id, draft in list(drafts.items()):
        if now - draft.created >= 3600:
            del drafts[message_id]
    return drafts


async def load_collections():
    try:
        items = await asyncio.to_thread(get_collections)
        return items, "" if items else "Нет доступных для записи коллекций."
    except ApiError as error:
        return {}, str(error)


async def load_tags(draft):
    draft.stage = "tags"
    draft.page = 0
    try:
        draft.tags = await asyncio.to_thread(get_tags)
        draft.error = ""
        if not draft.tags:
            draft.stage = "confirm"
    except ApiError as error:
        draft.error = str(error)


async def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not is_authorized(update):
        await query.answer()
        return

    drafts = active_drafts(context)
    message_id = query.message.message_id if query.message else None
    draft = drafts.get(message_id)
    parts = query.data.split(":", 2) if isinstance(query.data, str) else []
    if (not draft or len(parts) != 3 or parts[:2] != ["lw", draft.revision]
            or parts[2] not in draft.actions):
        await query.answer("Эта кнопка устарела.")
        return

    action = parts[2]
    await query.answer()
    if drafts.get(message_id) is not draft:
        return
    if action == "cancel":
        del drafts[message_id]
        await query.edit_message_text("Отменено.")
        return
    if action == "save":
        del drafts[message_id]
        try:
            link_id = await asyncio.to_thread(save_link, draft.url, draft.collection, draft.chosen)
            text = f"Ссылка сохранена в Linkwarden (#{link_id})."
        except ApiError as error:
            text = f"{error} Перед новой попыткой проверьте коллекцию в Linkwarden."
        await query.edit_message_text(text)
        return
    # Keep the current keyboard usable if Telegram rejects the edit.
    draft = replace(draft, chosen=draft.chosen.copy(), actions=set())
    if action == "retry":
        if draft.stage == "collections":
            draft.collections, draft.error = await load_collections()
        else:
            await load_tags(draft)
    elif action in ("next", "prev"):
        draft.page += 1 if action == "next" else -1
    elif action == "done":
        draft.stage = "confirm"
        draft.page = 0
    elif action.startswith("pick:"):
        item_id = int(action.split(":")[1])
        if draft.stage == "collections":
            draft.collection = item_id
            await load_tags(draft)
        elif item_id in draft.chosen:
            del draft.chosen[item_id]
        else:
            draft.chosen[item_id] = draft.tags[item_id]
    await query.edit_message_text(**draft.view())
    drafts[message_id] = draft


async def send_message_with_retry(update: Update, context: CallbackContext, text: str, max_retries: int = 3) -> None:
    for attempt in range(max_retries):
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
            return
        except TimedOut as e:
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying...")
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error(f"Failed to send message after {max_retries} attempts: {e}")
                raise

async def error_handler(update: object, context: CallbackContext) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.info("Network error occurred. The message might have been sent despite the error.")
    elif getattr(update, 'effective_chat', None) and is_authorized(update):
        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, 
                                           text="An error occurred. The developer has been notified.")
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")

def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(error_handler)

    while True:
        try:
            application.run_polling(timeout=10, poll_interval=10)  # Set poll_interval to 10 seconds
        except Conflict:
            logger.error("Conflict error occurred. Waiting before restarting...")
            time.sleep(30)
        except NetworkError as e:
            logger.error(f"Network error occurred: {e}. Restarting...")
            time.sleep(10)
        except TelegramError as e:
            logger.error(f"TelegramError occurred: {e}. Waiting before restarting...")
            time.sleep(30)
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}. Exiting...")
            break

if __name__ == '__main__':
    main()
