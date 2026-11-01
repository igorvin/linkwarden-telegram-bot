import os
import logging
import time
import asyncio
import sys
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, CallbackContext
from telegram.error import Conflict, TelegramError, TimedOut, NetworkError
from linkwarden import ApiError, get_collections, get_tags
from submissions import save_once, get_binding, set_binding, get_default_tags, set_default_tags
from scan_export import ExportError, MAX_EXPORT_BYTES, parse_export
from scan_jobs import ScanStore, progress_text
from selection import Draft, SESSION_TTL, parse_topic_collections
from dataclasses import replace
import re
from urllib.parse import urlparse
from collections import defaultdict
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

# Authorized Telegram group
ALLOWED_TELEGRAM_GROUP_ID = int(
    os.environ.get("ALLOWED_TELEGRAM_GROUP_ID", "0")
)

try:
    TOPIC_COLLECTIONS = parse_topic_collections(os.environ.get("TELEGRAM_TOPIC_COLLECTIONS", ""))
except (ValueError, TypeError):
    logger.error("Invalid TELEGRAM_TOPIC_COLLECTIONS configuration")
    sys.exit(1)


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if user is None or chat is None:
        return False

    # Private chat: only owner
    if chat.type == "private":
        return user.id == ALLOWED_TELEGRAM_USER_ID

    # Group: only the authorized supergroup
    if chat.type in ("group", "supergroup"):
        if chat.id != ALLOWED_TELEGRAM_GROUP_ID:
            return False

        # Reject anonymous administrators and channel identities
        message = update.effective_message
        if message is not None and message.sender_chat is not None:
            return False

        # Reject messages from other bots
        return not user.is_bot

    return False

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
    await send_message_with_retry(
        update, context,
        "Пришлите URL, выберите коллекцию и теги, затем подтвердите сохранение."
    )


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

    links = list(dict.fromkeys(extract_links(message)))
    
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

    sessions = active_sessions(context)
    original = update.effective_message
    chat_id = update.effective_chat.id
    thread_id = original.message_thread_id
    collections, error = await load_collections()
    bound_collection = await collection_for_chat(chat_id, thread_id)
    for link in links:
        if len(link) > 2048:
            await send_message_with_retry(update, context, "Ссылка длиннее 2048 символов.")
            continue
        if any(draft.chat_id == chat_id and draft.source_message_id == original.message_id
               and draft.url == link for draft in sessions.values()):
            continue
        draft = Draft(link, collections, user_id, chat_id, thread_id,
                      original.message_id, error=error, bound_collection=bound_collection)
        if bound_collection is not None and not error:
            await select_bound_collection(draft)
        # Register before sending. A timed-out send may still deliver its buttons.
        sessions[draft.session_id] = draft
        try:
            sent = await original.reply_text(
                **draft.view(), message_thread_id=thread_id
            )
            draft.message_id = sent.message_id
        except TelegramError:
            del sessions[draft.session_id]
            raise


async def collection_for_chat(chat_id, thread_id):
    binding = await asyncio.to_thread(get_binding, chat_id, thread_id)
    if binding is None and chat_id == ALLOWED_TELEGRAM_GROUP_ID:
        binding = TOPIC_COLLECTIONS.get(thread_id)
    # Zero is a persisted explicit removal, overriding environment defaults.
    return binding or None


async def bind_chat(update: Update, context: CallbackContext) -> None:
    await binding_command(update, context, "bind")


async def unbind_chat(update: Update, context: CallbackContext) -> None:
    await binding_command(update, context, "unbind")


async def binding_command(update: Update, context: CallbackContext, purpose: str) -> None:
    if not is_authorized(update):
        return
    if update.effective_user.id != ALLOWED_TELEGRAM_USER_ID:
        await send_message_with_retry(update, context, "Привязку может менять только владелец бота.")
        return
    if not check_rate_limit(update.effective_user.id):
        await send_message_with_retry(update, context, "Слишком много запросов. Повторите позже.")
        return
    original = update.effective_message
    bound = None
    collections, error = {}, ""
    if purpose == "unbind":
        bound = await collection_for_chat(update.effective_chat.id, original.message_thread_id)
        if bound is None:
            await send_message_with_retry(update, context, "Этот чат/тема уже без привязки к коллекции.")
            return
    else:
        collections, error = await load_collections()
    draft = Draft("", collections, update.effective_user.id, update.effective_chat.id,
                  original.message_thread_id, original.message_id, purpose=purpose, error=error,
                  bound_collection=bound, stage="confirm" if purpose == "unbind" else "collections")
    sessions = active_sessions(context)
    sessions[draft.session_id] = draft
    try:
        sent = await original.reply_text(**draft.view(), message_thread_id=draft.thread_id)
        draft.message_id = sent.message_id
    except TelegramError:
        del sessions[draft.session_id]
        raise


def active_sessions(context):
    sessions = context.bot_data.setdefault("selection_sessions", {})
    now = time.monotonic()
    for session_id, draft in list(sessions.items()):
        if not draft.busy and now - draft.created >= SESSION_TTL:
            del sessions[session_id]
    return sessions


async def load_collections():
    try:
        items = await asyncio.to_thread(get_collections)
        return items, "" if items else "Нет доступных для записи коллекций."
    except ApiError as error:
        return {}, str(error)


async def select_bound_collection(draft):
    if draft.bound_collection not in draft.collections:
        draft.error = "Привязанная коллекция недоступна. Проверьте настройки темы или права Linkwarden."
        return
    draft.collection = draft.bound_collection
    await load_tags(draft)


async def load_tags(draft):
    draft.stage = "tags"
    draft.page = 0
    try:
        draft.tags = await asyncio.to_thread(get_tags)
        if draft.purpose in ("save", "scan") and not draft.defaults_loaded:
            defaults = await asyncio.to_thread(get_default_tags, draft.chat_id, draft.thread_id)
            draft.chosen = {key: name for key, name in draft.tags.items() if key in defaults}
            draft.defaults_loaded = True
        draft.chosen = {key: name for key, name in draft.tags.items() if key in draft.chosen}
        draft.error = ""
        if not draft.tags and draft.purpose != "default_tags":
            draft.stage = "confirm"
    except ApiError as error:
        draft.error = str(error)




async def load_settings(draft):
    draft.purpose = "settings"
    draft.stage = "collections"
    draft.error = ""
    draft.page = 0
    draft.bound_collection = await collection_for_chat(draft.chat_id, draft.thread_id)
    collections, error = await load_collections()
    draft.collections = collections
    if draft.bound_collection is None:
        collection = "Ручной выбор"
    else:
        name = collections.get(draft.bound_collection, "Недоступна")
        collection = f"{name[:200]} (#{draft.bound_collection})"
    ids = await asyncio.to_thread(get_default_tags, draft.chat_id, draft.thread_id)
    tag_error = ""
    try:
        tags = await asyncio.to_thread(get_tags)
    except ApiError as failure:
        tags = {}
        tag_error = str(failure)
    names = [tags.get(tag_id, f"#{tag_id}")[:75] for tag_id in ids[:10]]
    description = ", ".join(names) if names else "Нет"
    if len(ids) > 10:
        description += f" и ещё {len(ids) - 10}"
    draft.settings_summary = (
        f"Настройки текущего чата/темы\nКоллекция: {collection}\n"
        f"Теги по умолчанию: {description}"
    )
    if error or tag_error:
        draft.settings_summary += "\nЧасть данных Linkwarden недоступна. Можно обновить меню."


async def settings_chat(update: Update, context: CallbackContext) -> None:
    if not is_authorized(update):
        return
    if update.effective_user.id != ALLOWED_TELEGRAM_USER_ID:
        await send_message_with_retry(update, context, "Настройки доступны только владельцу бота.")
        return
    if not check_rate_limit(update.effective_user.id):
        await send_message_with_retry(update, context, "Слишком много запросов. Повторите позже.")
        return
    original = update.effective_message
    draft = Draft("", {}, update.effective_user.id, update.effective_chat.id,
                  original.message_thread_id, original.message_id, purpose="settings")
    await load_settings(draft)
    await send_draft(update, context, draft)


async def send_draft(update, context, draft):
    sessions = active_sessions(context)
    sessions[draft.session_id] = draft
    try:
        sent = await update.effective_message.reply_text(
            **draft.view(), message_thread_id=draft.thread_id
        )
        draft.message_id = sent.message_id
    except TelegramError:
        del sessions[draft.session_id]
        raise


async def scan_chat(update: Update, context: CallbackContext) -> None:
    if not is_authorized(update):
        return
    if update.effective_user.id != ALLOWED_TELEGRAM_USER_ID:
        await send_message_with_retry(update, context, "Импорт доступен только владельцу бота.")
        return
    original = update.effective_message
    if (update.effective_chat.id != ALLOWED_TELEGRAM_GROUP_ID
            or original.message_thread_id in (None, 1)):
        await send_message_with_retry(update, context, "Отправьте /scan в нужной отдельной теме группы.")
        return
    if not check_rate_limit(update.effective_user.id):
        await send_message_with_retry(update, context, "Слишком много запросов. Повторите позже.")
        return
    job = ScanStore().active(update.effective_chat.id, original.message_thread_id)
    draft = Draft("", {}, update.effective_user.id, update.effective_chat.id,
                  original.message_thread_id, original.message_id, purpose="scan_upload")
    if job:
        draft.purpose = "scan_control"
        draft.job_id = job["id"]
        draft.scan_summary = progress_text(job)
        task = context.bot_data.get("scan_tasks", {}).get(job["id"])
        draft.scan_running = task is not None and not task.done()
    else:
        # Replace an earlier upload prompt for this topic, leaving active jobs intact.
        sessions = active_sessions(context)
        for key, previous in list(sessions.items()):
            if previous.purpose == "scan_upload" and previous.chat_id == draft.chat_id and previous.thread_id == draft.thread_id:
                del sessions[key]
    await send_draft(update, context, draft)


async def scan_document(update: Update, context: CallbackContext) -> None:
    if not is_authorized(update) or update.effective_user.id != ALLOWED_TELEGRAM_USER_ID:
        return
    original = update.effective_message
    waiting = next((draft for draft in active_sessions(context).values()
                    if draft.purpose == "scan_upload" and draft.owner_id == update.effective_user.id
                    and draft.chat_id == update.effective_chat.id
                    and draft.thread_id == original.message_thread_id), None)
    if waiting is None:
        return
    document = original.document
    if not document.file_name or not document.file_name.lower().endswith(".json"):
        await send_message_with_retry(update, context, "Нужен файл JSON из экспорта Telegram Desktop.")
        return
    if document.file_size is None or document.file_size > MAX_EXPORT_BYTES:
        await send_message_with_retry(update, context, "Размер JSON должен быть не больше 10 МБ.")
        return
    try:
        file = await context.bot.get_file(document.file_id)
        content = await file.download_as_bytearray()
        result = await asyncio.to_thread(parse_export, content, waiting.chat_id,
                                         waiting.thread_id, extract_links, context.bot.id)
    except ExportError as error:
        await send_message_with_retry(update, context, str(error))
        return
    except TelegramError:
        await send_message_with_retry(update, context, "Не удалось загрузить файл. Отправьте JSON ещё раз.")
        return
    summary = (f"Текущая тема #{waiting.thread_id}\n"
               f"Сообщений со ссылками: {result.matched_messages}\n"
               f"Уникальных URL: {len(result.urls)}\n"
               f"Повторов внутри экспорта: {result.occurrences - len(result.urls)}\n"
               f"Пропущено сообщений из других тем: {result.other_messages}\n"
               f"Пропущено без определённой темы: {result.unknown_messages}\n"
               "Существующие ссылки будут пропущены при импорте.")
    collections, error = await load_collections()
    bound = await collection_for_chat(waiting.chat_id, waiting.thread_id)
    draft = Draft("", collections, waiting.owner_id, waiting.chat_id, waiting.thread_id,
                  original.message_id, purpose="scan", urls=result.urls,
                  scan_summary=summary, bound_collection=bound, error=error)
    if bound is not None and not error:
        await select_bound_collection(draft)
    await send_draft(update, context, draft)
    active_sessions(context).pop(waiting.session_id, None)


def start_scan_task(job, message_id, context):
    tasks = context.bot_data.setdefault("scan_tasks", {})
    current = tasks.get(job["id"])
    if current is not None and not current.done():
        return
    ScanStore().start(job["id"], message_id)
    tasks[job["id"]] = context.application.create_task(run_scan(job["id"], context))


async def notify_scan(job, context):
    try:
        await context.bot.edit_message_text(
            chat_id=job["chat_id"], message_id=job["message_id"], text=progress_text(job)
        )
    except TelegramError:
        logger.warning("Could not update scan progress")


async def run_scan(job_id, context):
    store = ScanStore()
    try:
        while True:
            job = store.get(job_id)
            if job is None or job["state"] != "running":
                return
            if job["cursor"] >= len(job["urls"]):
                store.state(job_id, "done")
                await notify_scan(store.get(job_id), context)
                return
            try:
                _, created = await asyncio.to_thread(
                    save_once, job["urls"][job["cursor"]], job["collection_id"], job["tags"]
                )
            except ApiError as error:
                store.state(job_id, "paused", str(error))
                await notify_scan(store.get(job_id), context)
                return
            store.advance(job_id, job["cursor"], created)
            latest = store.get(job_id)
            if latest["cursor"] % 5 == 0:
                await notify_scan(latest, context)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        store.state(job_id, "paused", "Обработка прервана. Используйте /scan для продолжения.")
        raise
    except Exception:
        store.state(job_id, "paused", "Ошибка обработки. Используйте /scan для продолжения.")
        logger.error("Scan worker stopped unexpectedly")
        await notify_scan(store.get(job_id), context)
    finally:
        context.bot_data.get("scan_tasks", {}).pop(job_id, None)


async def answer_callback(query, text=None):
    try:
        await query.answer(text)
    except TelegramError:
        logger.warning("Could not acknowledge callback")


async def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not is_authorized(update):
        await answer_callback(query, "Нет доступа.")
        return

    sessions = active_sessions(context)
    parts = query.data.split(":") if isinstance(query.data, str) else []
    draft = sessions.get(parts[1]) if len(parts) == 4 and parts[0] == "lw" else None
    if draft is None:
        await answer_callback(query, "Сессия истекла. Повторите команду или отправьте URL заново.")
        return
    if update.effective_user.id != draft.owner_id:
        await answer_callback(query, "Кнопки доступны только автору запроса.")
        return
    if draft.purpose in ("bind", "unbind", "scan", "scan_upload", "scan_control", "settings", "default_tags") and update.effective_user.id != ALLOWED_TELEGRAM_USER_ID:
        await answer_callback(query, "Привязку может менять только владелец бота.")
        return
    message = query.message
    if (message is None or update.effective_chat.id != draft.chat_id
            or message.message_id != draft.message_id
            or message.message_thread_id != draft.thread_id):
        await answer_callback(query, "Кнопка не относится к этому сообщению.")
        return
    action = draft.actions.get(parts[3])
    if parts[2] != draft.revision or action is None:
        await answer_callback(query, "Эта кнопка устарела.")
        return
    if draft.busy:
        await answer_callback(query, "Действие уже выполняется.")
        return

    draft.busy = True
    try:
        await answer_callback(query)
        operation, value = action
        if operation == "cancel":
            del sessions[draft.session_id]
            await query.edit_message_text("Отменено.")
            return
        if operation in ("settings_bind", "settings_unbind", "settings_tags", "settings_refresh"):
            changed = replace(draft, actions={}, busy=False, chosen={}, page=0, error="")
            if operation == "settings_bind":
                changed.purpose = "bind"
                changed.bound_collection = None
                changed.collection = None
                changed.stage = "collections"
                changed.collections, changed.error = await load_collections()
            elif operation == "settings_unbind":
                changed.bound_collection = await collection_for_chat(draft.chat_id, draft.thread_id)
                if changed.bound_collection is None:
                    await load_settings(changed)
                else:
                    changed.purpose = "unbind"
                    changed.stage = "confirm"
            elif operation == "settings_tags":
                changed.purpose = "default_tags"
                changed.bound_collection = None
                ids = await asyncio.to_thread(get_default_tags, draft.chat_id, draft.thread_id)
                changed.chosen = dict.fromkeys(ids, "")
                await load_tags(changed)
            else:
                await load_settings(changed)
            await query.edit_message_text(**changed.view())
            sessions[draft.session_id] = changed
            return
        if operation == "tags_save":
            changed = replace(draft, actions={}, busy=False)
            try:
                tags = await asyncio.to_thread(get_tags) if draft.chosen else {}
                if any(key not in tags for key in draft.chosen):
                    raise ApiError("Список тегов изменился. Обновите выбор перед сохранением.")
            except ApiError as error:
                changed.stage = "tags"
                changed.error = str(error)
                await query.edit_message_text(**changed.view())
                sessions[draft.session_id] = changed
                return
            await asyncio.to_thread(set_default_tags, draft.chat_id, draft.thread_id, tuple(draft.chosen))
            del sessions[draft.session_id]
            await query.edit_message_text("Теги по умолчанию сохранены. Они будут отмечены для новых URL и импортов.")
            return
        if operation in ("scan_resume", "scan_cancel"):
            store = ScanStore()
            job = store.get(draft.job_id)
            if job is None or job["owner_id"] != draft.owner_id or job["chat_id"] != draft.chat_id or job["thread_id"] != (draft.thread_id or 0):
                await query.edit_message_text("Импорт недоступен.")
                return
            del sessions[draft.session_id]
            if operation == "scan_cancel":
                store.state(job["id"], "cancelled")
                await query.edit_message_text(
                    "Импорт отменён. Уже сохранённые ссылки останутся. Текущий запрос может завершиться."
                )
            elif job["state"] in ("queued", "running", "paused"):
                await query.edit_message_text("Продолжаю импорт…")
                start_scan_task(job, draft.message_id, context)
            else:
                await query.edit_message_text(progress_text(job))
            return
        if operation == "unbind_save":
            current = await collection_for_chat(draft.chat_id, draft.thread_id)
            if current != draft.bound_collection:
                del sessions[draft.session_id]
                await query.edit_message_text("Привязка изменилась. Повторите /unbind.")
                return
            await asyncio.to_thread(set_binding, draft.chat_id, draft.thread_id, 0)
            del sessions[draft.session_id]
            await query.edit_message_text(
                "Привязка удалена. Для новых URL снова доступен выбор коллекции."
            )
            return
        if operation == "bind_save":
            collections, error = await load_collections()
            if error or draft.collection not in collections:
                changed = replace(draft, stage="collections", collections=collections,
                                  actions={}, busy=False, page=0,
                                  error=error or "Коллекция недоступна. Выберите другую после обновления.")
                await query.edit_message_text(**changed.view())
                sessions[draft.session_id] = changed
                return
            await asyncio.to_thread(set_binding, draft.chat_id, draft.thread_id, draft.collection)
            del sessions[draft.session_id]
            await query.edit_message_text(
                f"Привязка сохранена: {collections[draft.collection][:200]} (#{draft.collection}). "
                "Для новых URL останутся выбор тегов и подтверждение."
            )
            return
        if operation == "save" and draft.purpose == "scan":
            job = ScanStore().create(draft)
            del sessions[draft.session_id]
            await query.edit_message_text("Импорт подтверждён. Запускаю обработку…")
            start_scan_task(job, draft.message_id, context)
            return
        if operation == "save":
            del sessions[draft.session_id]
            try:
                link_id, created = await asyncio.to_thread(
                    save_once, draft.url, draft.collection, draft.chosen
                )
                text = (f"Ссылка сохранена в Linkwarden (#{link_id})." if created else
                        f"Ссылка уже есть в Linkwarden (#{link_id}). Новая копия не создана.")
            except ApiError as error:
                text = str(error)
                if error.outcome_unknown:
                    text += " Результат сохранения неизвестен. Проверьте Linkwarden."
            await query.edit_message_text(text)
            return

        changed = replace(draft, chosen=draft.chosen.copy(), actions={}, busy=False)
        if operation == "back":
            changed.page = 0
            changed.error = ""
            if draft.purpose == "default_tags":
                if draft.stage == "confirm":
                    changed.stage = "tags"
                    await load_tags(changed)
                else:
                    await load_settings(changed)
            elif draft.purpose == "bind":
                changed.stage = "collections"
                changed.collections, changed.error = await load_collections()
            elif draft.stage == "confirm":
                await load_tags(changed)
                changed.stage = "tags"
            else:
                changed.stage = "collections"
                changed.collections, changed.error = await load_collections()
        elif operation == "retry":
            if changed.stage == "collections":
                changed.collections, changed.error = await load_collections()
                if changed.bound_collection is not None and not changed.error:
                    await select_bound_collection(changed)
            else:
                await load_tags(changed)
        elif operation in ("next", "prev"):
            changed.page += 1 if operation == "next" else -1
        elif operation == "done":
            changed.stage = "confirm"
            changed.page = 0
        elif operation == "pick":
            if changed.stage == "collections":
                changed.collection = value
                if changed.purpose == "bind":
                    changed.stage = "confirm"
                    changed.page = 0
                else:
                    await load_tags(changed)
            elif value in changed.chosen:
                del changed.chosen[value]
            else:
                changed.chosen[value] = changed.tags[value]
        await query.edit_message_text(**changed.view())
        sessions[draft.session_id] = changed
    finally:
        draft.busy = False


async def send_message_with_retry(
    update: Update,
    context: CallbackContext,
    text: str,
    max_retries: int = 3
) -> None:
    message = update.effective_message

    if message is None:
        logger.warning("Cannot reply: no effective message")
        return

    for attempt in range(max_retries):
        try:
            await message.reply_text(text, message_thread_id=message.message_thread_id)
            return

        except TimedOut as e:
            if attempt < max_retries - 1:
                logger.warning(
                    "Attempt %s failed: %s. Retrying...",
                    attempt + 1,
                    e
                )
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error(
                    "Failed to send message after %s attempts: %s",
                    max_retries,
                    e
                )
                raise

        except TelegramError as e:
            logger.error(
                "Failed to send Telegram message: %s",
                e
            )
            return
async def error_handler(
    update: object,
    context: CallbackContext
) -> None:
    """Log errors and notify authorized users when possible."""

    logger.error(
        "Exception while handling an update:",
        exc_info=context.error
    )

    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.info(
            "Network error occurred. Message delivery is uncertain."
        )
        return

    if not isinstance(update, Update):
        return

    if not is_authorized(update):
        return

    message = update.effective_message

    if message is None:
        return

    try:
        await message.reply_text(
            "An error occurred while processing your request.",
            message_thread_id=message.message_thread_id
        )
    except TelegramError as e:
        logger.error(
            "Failed to send error notification: %s",
            e
        )
def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(False).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bind", bind_chat))
    application.add_handler(CommandHandler("unbind", unbind_chat))
    application.add_handler(CommandHandler("scan", scan_chat))
    application.add_handler(CommandHandler("settings", settings_chat))
    application.add_handler(MessageHandler(filters.Document.ALL, scan_document))
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
