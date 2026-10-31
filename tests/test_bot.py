import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Update
from telegram.error import NetworkError, TimedOut

os.environ["TELEGRAM_TOKEN"] = "test-token"
os.environ["LINKWARDEN_API_URL"] = "https://linkwarden.example.com"
os.environ["LINKWARDEN_API_KEY"] = "test-api-key"
os.environ["ALLOWED_TELEGRAM_USER_ID"] = "100"
os.environ["ALLOWED_TELEGRAM_GROUP_ID"] = "-1001872870761"

import bot
from bot import extract_links, check_rate_limit, user_message_history, RATE_LIMIT_THRESHOLD
from linkwarden import ApiError
from selection import SESSION_TTL


@pytest.fixture
def update():
    update = MagicMock(spec=Update)
    update.effective_user.id = 100
    update.effective_user.is_bot = False
    update.effective_chat.id = 100
    update.effective_chat.type = "private"
    update.message.text = "https://example.com"
    update.message.message_id = 23
    update.message.message_thread_id = None
    update.message.sender_chat = None
    update.message.reply_text = AsyncMock()
    update.message.reply_text.return_value.message_id = 55
    update.effective_message = update.message
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.message_id = 55
    update.callback_query.message.message_thread_id = None
    return update


@pytest.fixture
def context():
    context = MagicMock()
    context.bot_data = {}
    context.bot.send_message = AsyncMock()
    return context


@pytest.fixture
def workflow():
    with (patch("bot.get_collections", return_value={1: "Inbox", 2: "Fishing"}) as collections,
          patch("bot.get_tags", return_value={3: "Knots", 4: "Bait"}) as tags,
          patch("bot.save_once", return_value=(91, True)) as save,
          patch("bot.check_rate_limit", return_value=True)):
        yield collections, tags, save


def draft(context):
    return next(iter(context.bot_data["selection_sessions"].values()))


def data_for(context, action, value=None):
    session = draft(context)
    token = next(key for key, item in session.actions.items() if item == (action, value))
    return f"lw:{session.session_id}:{session.revision}:{token}"


async def press(update, context, action=None, value=None, data=None):
    update.callback_query.data = data or data_for(context, action, value)
    await bot.handle_callback(update, context)


def group(update, user_id=200, thread=42):
    update.effective_user.id = user_id
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001872870761
    update.message.message_thread_id = thread
    update.callback_query.message.message_thread_id = thread


class TestExtractLinks:
    def test_extracts_basic_urls(self):
        text = "Here are some links: https://example.com and http://test.com"
        links = extract_links(text)
        assert links == ["https://example.com", "http://test.com"]

    def test_rejects_localhost(self):
        links = extract_links("Visit http://localhost/admin")
        assert links == []

    def test_rejects_private_ip(self):
        links = extract_links("Visit http://192.168.1.1/admin")
        assert links == []

    def test_rejects_loopback(self):
        links = extract_links("Visit http://127.0.0.1/admin")
        assert links == []

    def test_rejects_aws_metadata(self):
        links = extract_links("Visit http://169.254.169.254/latest/meta-data/")
        assert links == []

    def test_rejects_internal_tld(self):
        links = extract_links("Visit http://myserver.local/file")
        assert links == []

    def test_no_links(self):
        links = extract_links("No links here")
        assert links == []

    def test_multiple_links(self):
        text = "https://a.com https://b.com http://c.org/path?q=1"
        links = extract_links(text)
        assert len(links) == 3

    def test_rejects_empty_hostname(self):
        links = extract_links("https:///path")
        assert links == []


class TestRateLimit:
    def setup_method(self):
        user_message_history.clear()

    def test_allows_within_limit(self):
        assert check_rate_limit(999) is True

    def test_blocks_over_limit(self):
        for _ in range(RATE_LIMIT_THRESHOLD):
            check_rate_limit(888)
        assert check_rate_limit(888) is False

    def test_different_users_independent(self):
        for _ in range(RATE_LIMIT_THRESHOLD):
            check_rate_limit(777)
        assert check_rate_limit(666) is True



@pytest.mark.parametrize("kind,user_id,chat_id,is_bot,sender_chat,allowed", [
    ("private", 100, 100, False, None, True),
    ("private", 200, 200, False, None, False),
    ("supergroup", 200, -1001872870761, False, None, True),
    ("group", 200, -1001872870761, False, None, True),
    ("supergroup", 100, -123, False, None, False),
    ("supergroup", 200, -1001872870761, True, None, False),
    ("supergroup", 200, -1001872870761, False, object(), False),
    ("channel", 100, -1001872870761, False, None, False),
])
def test_authorization(update, kind, user_id, chat_id, is_bot, sender_chat, allowed):
    update.effective_chat.type = kind
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.effective_user.is_bot = is_bot
    update.effective_message.sender_chat = sender_chat
    assert bot.is_authorized(update) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [None, 1, 42, 99])
async def test_full_flow_preserves_topic(update, context, workflow, thread):
    group(update, thread=thread)
    collections, tags, save = workflow
    await bot.handle_message(update, context)
    save.assert_not_called()
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == thread
    session = draft(context)
    assert session.owner_id == 200 and session.thread_id == thread
    for row in update.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
            assert session.url not in button.callback_data
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    await press(update, context, "pick", 4)
    await press(update, context, "pick", 3)
    save.assert_not_called()
    await press(update, context, "done")
    summary = update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert "Fishing" in summary and "Bait" in summary and "Knots" not in summary
    await press(update, context, "save")
    save.assert_called_once_with("https://example.com", 2, {4: "Bait"})
    assert not context.bot_data["selection_sessions"]


@pytest.mark.asyncio
async def test_private_flow_and_zero_tags(update, context, workflow):
    workflow[1].return_value = {}
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    assert draft(context).stage == "confirm"
    await press(update, context, "save")
    workflow[2].assert_called_once_with("https://example.com", 1, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["collections", "tags", "confirm"])
@pytest.mark.parametrize("intruder", [201, 100])
async def test_other_member_including_owner_cannot_use_buttons(update, context, workflow, stage, intruder):
    group(update)
    await bot.handle_message(update, context)
    if stage != "collections":
        await press(update, context, "pick", 1)
    if stage == "confirm":
        await press(update, context, "done")
    session = draft(context)
    action = {"collections": ("pick", 1), "tags": ("pick", 3), "confirm": ("save", None)}[stage]
    data = data_for(context, *action)
    workflow[0].reset_mock()
    workflow[1].reset_mock()
    update.callback_query.edit_message_text.reset_mock()
    update.effective_user.id = intruder
    await press(update, context, data=data)
    assert draft(context) is session
    workflow[0].assert_not_called()
    workflow[1].assert_not_called()
    workflow[2].assert_not_called()
    update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["chat", "thread", "message", "user-bot", "anonymous", "private-user"])
async def test_callback_checks_all_bindings(update, context, workflow, change):
    group(update)
    await bot.handle_message(update, context)
    data = data_for(context, "pick", 1)
    if change == "chat":
        update.effective_chat.id = -999
    elif change == "thread":
        update.callback_query.message.message_thread_id = 999
    elif change == "message":
        update.callback_query.message.message_id = 999
    elif change == "user-bot":
        update.effective_user.is_bot = True
    elif change == "anonymous":
        update.effective_message.sender_chat = object()
    else:
        update.effective_chat.type = "private"
    await press(update, context, data=data)
    workflow[1].assert_not_called()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_back_retains_tags_and_changes_collection(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    await press(update, context, "pick", 3)
    await press(update, context, "done")
    await press(update, context, "back")
    assert draft(context).chosen == {3: "Knots"}
    await press(update, context, "back")
    assert draft(context).stage == "collections"
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    await press(update, context, "save")
    workflow[2].assert_called_once_with("https://example.com", 2, {3: "Knots"})


@pytest.mark.asyncio
async def test_cancel_and_expiry(update, context, workflow):
    await bot.handle_message(update, context)
    old = data_for(context, "pick", 1)
    draft(context).created -= SESSION_TTL + 1
    await press(update, context, data=old)
    assert not context.bot_data["selection_sessions"]
    await bot.handle_message(update, context)
    await press(update, context, "cancel")
    await press(update, context, data=old)
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_api_failures_and_empty_collections(update, context, workflow):
    workflow[0].side_effect = [ApiError("HTTP 503"), {}, {1: "Inbox"}]
    workflow[1].side_effect = [ApiError("HTTP 401"), {}]
    await bot.handle_message(update, context)
    assert draft(context).error == "HTTP 503"
    await press(update, context, "retry")
    assert draft(context).error
    await press(update, context, "retry")
    await press(update, context, "pick", 1)
    assert draft(context).error == "HTTP 401"
    await press(update, context, "retry")
    assert draft(context).stage == "confirm"
    workflow[2].side_effect = ApiError("Неизвестный результат", outcome_unknown=True)
    await press(update, context, "save")
    assert "неизвестен" in update.callback_query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_repeated_url_and_update_only_create_one_session(update, context, workflow):
    update.message.text = "https://example.com https://example.com"
    await bot.handle_message(update, context)
    await bot.handle_message(update, context)
    assert len(context.bot_data["selection_sessions"]) == 1
    update.message.reply_text.assert_awaited_once()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_repeated_save_is_claimed_before_network_await(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    await press(update, context, "done")
    data = data_for(context, "save")
    await asyncio.gather(press(update, context, data=data), press(update, context, data=data))
    workflow[2].assert_called_once()


@pytest.mark.asyncio
async def test_stale_forged_and_missing_session(update, context, workflow):
    await bot.handle_message(update, context)
    old = data_for(context, "pick", 1)
    await press(update, context, "pick", 1)
    await press(update, context, data=old)
    await press(update, context, data="lw:unknown:revision:save")
    await press(update, context, data="lw:" + draft(context).session_id + ":forged:save")
    assert draft(context).stage == "tags"
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_rejected_edit_preserves_current_buttons(update, context, workflow):
    await bot.handle_message(update, context)
    data = data_for(context, "pick", 1)
    update.callback_query.edit_message_text.side_effect = NetworkError("test")
    with pytest.raises(NetworkError):
        await press(update, context, data=data)
    update.callback_query.edit_message_text.side_effect = None
    await press(update, context, data=data)
    assert draft(context).stage == "tags"


@pytest.mark.asyncio
async def test_paging_stale_toggle_and_confirmation(update, context, workflow):
    workflow[1].return_value = {i: f"tag{i}" for i in range(20)}
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    await press(update, context, "next")
    old = data_for(context, "pick", 9)
    await press(update, context, data=old)
    await press(update, context, data=old)
    assert draft(context).chosen == {9: "tag9"}
    await press(update, context, "prev")
    await press(update, context, "done")
    assert "tag9" in update.callback_query.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_start_and_error_replies_use_topic(update, context):
    group(update)
    await bot.start(update, context)
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 42
    context.error = ValueError("test")
    await bot.error_handler(update, context)
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_rate_limit_and_unauthorized_message(update, context, workflow):
    with patch("bot.check_rate_limit", return_value=False):
        await bot.handle_message(update, context)
    assert update.message.reply_text.await_count == 1
    update.effective_user.id = 999
    await bot.handle_message(update, context)
    workflow[0].assert_not_called()


@pytest.mark.asyncio
async def test_retry_response_preserves_topic(update, context):
    group(update)
    update.message.reply_text.side_effect = [TimedOut(), None]
    with patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.send_message_with_retry(update, context, "test")
    assert update.message.reply_text.await_count == 2
    assert all(call.kwargs["message_thread_id"] == 42
               for call in update.message.reply_text.await_args_list)

@pytest.mark.asyncio
async def test_group_flow_uses_real_api_client_and_persistent_dedup(update, context, tmp_path, monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setenv("BOT_STATE_PATH", str(tmp_path / "journal.sqlite3"))
    group(update)
    empty = Mock(status_code=200)
    empty.json.return_value = {"response": []}
    saved = Mock(status_code=200)
    saved.json.return_value = {"response": {"id": 72}}
    with (patch("bot.get_collections", return_value={8: "Fishing"}),
          patch("bot.get_tags", return_value={9: "Knots"}),
          patch("bot.check_rate_limit", return_value=True),
          patch("linkwarden.requests.request", side_effect=[empty, saved]) as request):
        await bot.handle_message(update, context)
        await press(update, context, "pick", 8)
        await press(update, context, "pick", 9)
        await press(update, context, "done")
        request.assert_not_called()
        await press(update, context, "save")
        assert request.call_args.kwargs["json"] == {
            "url": "https://example.com", "collection": {"id": 8},
            "tags": [{"id": 9, "name": "Knots"}],
        }
        # A second source message gets a new session but reuses the saved link.
        update.message.message_id = 24
        await bot.handle_message(update, context)
        await press(update, context, "pick", 8)
        await press(update, context, "done")
        await press(update, context, "save")
        assert request.call_count == 2
        assert "уже есть" in update.callback_query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_missing_users_and_edited_messages_do_not_save(update, context, workflow):
    update.effective_user = None
    await bot.handle_message(update, context)
    workflow[0].assert_not_called()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_input_validation(update, context, workflow):
    for message in ("", "no links", "javascript:alert(1)", "x" * 51201):
        update.message.text = message
        await bot.handle_message(update, context)
    workflow[0].assert_not_called()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_multiple_sessions_are_independent(update, context, workflow):
    from unittest.mock import Mock
    group(update)
    update.message.reply_text.side_effect = [Mock(message_id=55), Mock(message_id=56)]
    update.message.text = "https://first.com"
    await bot.handle_message(update, context)
    first = draft(context)
    update.message.text = "https://second.com"
    update.message.message_id = 24
    update.effective_user.id = 201
    await bot.handle_message(update, context)
    second = next(item for item in context.bot_data["selection_sessions"].values()
                  if item is not first)
    token = next(key for key, action in first.actions.items() if action == ("pick", 1))
    await press(update, context, data=f"lw:{first.session_id}:{first.revision}:{token}")
    workflow[1].assert_not_called()
    assert second.owner_id == 201 and first.owner_id == 200
    workflow[2].assert_not_called()

@pytest.mark.parametrize("text,expected", [("", {}), ("{}", {}), ('{"9":1}', {9: 1}), ('{"9":1,"15":2}', {9: 1, 15: 2})])
def test_topic_mapping_config(text, expected):
    from selection import parse_topic_collections
    assert parse_topic_collections(text) == expected


@pytest.mark.parametrize("text", ["[]", "null", '{"0":1}', '{"9":0}', '{"9":true}', '{"9":"1"}', '{"bad":1}', "{"])
def test_invalid_topic_mapping(text):
    from selection import parse_topic_collections
    with pytest.raises((ValueError, TypeError)):
        parse_topic_collections(text)


@pytest.mark.asyncio
async def test_bound_topic_skips_collection_and_locks_back(update, context, workflow, monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 2})
    group(update, thread=9)
    await bot.handle_message(update, context)
    session = draft(context)
    assert session.collection == 2 and session.stage == "tags"
    assert not any(action == ("back", None) for action in session.actions.values())
    assert "Fishing" in update.message.reply_text.await_args.kwargs["text"]
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 9
    await press(update, context, "pick", 3)
    await press(update, context, "done")
    await press(update, context, "back")
    assert draft(context).stage == "tags" and draft(context).chosen == {3: "Knots"}
    assert not any(action == ("back", None) for action in draft(context).actions.values())
    await press(update, context, "done")
    await press(update, context, "save")
    workflow[2].assert_called_once_with("https://example.com", 2, {3: "Knots"})


@pytest.mark.asyncio
@pytest.mark.parametrize("private,thread", [(True, 9), (False, 42), (False, None)])
async def test_topic_binding_does_not_affect_other_chats(update, context, workflow, monkeypatch, private, thread):
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 2})
    if private:
        update.message.message_thread_id = thread
    else:
        group(update, thread=thread)
    await bot.handle_message(update, context)
    assert draft(context).stage == "collections"
    assert draft(context).bound_collection is None
    workflow[1].assert_not_called()


@pytest.mark.asyncio
async def test_bound_collection_missing_then_retry(update, context, workflow, monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 2})
    group(update, thread=9)
    workflow[0].side_effect = [{1: "Inbox"}, {2: "Fishing"}]
    await bot.handle_message(update, context)
    assert draft(context).error
    workflow[1].assert_not_called()
    assert all(action != "pick" for action, _ in draft(context).actions.values())
    await press(update, context, "retry")
    assert draft(context).stage == "tags" and draft(context).collection == 2
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_bound_fetch_errors_and_empty_tags(update, context, workflow, monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 2})
    group(update, thread=9)
    workflow[0].side_effect = [ApiError("HTTP 503"), {2: "Fishing"}]
    workflow[1].side_effect = [ApiError("HTTP 401"), {}]
    await bot.handle_message(update, context)
    await press(update, context, "retry")
    assert draft(context).stage == "tags" and draft(context).error == "HTTP 401"
    assert all(action != "back" for action, _ in draft(context).actions.values())
    await press(update, context, "retry")
    assert draft(context).stage == "confirm"
    await press(update, context, "save")
    workflow[2].assert_called_once_with("https://example.com", 2, {})


@pytest.mark.asyncio
async def test_bound_topic_still_rejects_other_member(update, context, workflow, monkeypatch):
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 2})
    group(update, thread=9)
    await bot.handle_message(update, context)
    data = data_for(context, "done")
    update.effective_user.id = 201
    await press(update, context, data=data)
    assert draft(context).stage == "tags"
    workflow[2].assert_not_called()
