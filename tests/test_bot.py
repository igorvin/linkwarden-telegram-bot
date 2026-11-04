import asyncio
import os
import time
from types import SimpleNamespace
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


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_PATH", str(tmp_path / "state.sqlite3"))


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
    context.bot.edit_message_reply_markup = AsyncMock()
    return context


@pytest.fixture(autouse=True)
def preservation_transport():
    with patch("bot.get_preservation_status", return_value=(("Скриншот: ✅ готов",), False)):
        yield


@pytest.fixture(autouse=True)
def metadata_transport():
    with patch("bot.get_link_metadata", side_effect=ApiError("metadata unavailable")):
        yield


@pytest.fixture
def workflow():
    with (patch("bot.get_collections", return_value={1: "Inbox", 2: "Fishing"}) as collections,
          patch("bot.get_tags", return_value={3: "Knots", 4: "Bait"}) as tags,
          patch("bot.save_once", return_value=(91, True)) as save,
          patch("bot.check_rate_limit", return_value=True)):
        yield collections, tags, save


def draft(context):
    return next(reversed(context.bot_data["selection_sessions"].values()))


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
    save.assert_called_once_with("https://example.com", 2, {4: "Bait"})
    assert draft(context).purpose == "preservation"



@pytest.mark.asyncio
async def test_private_flow_and_zero_tags(update, context, workflow):
    workflow[1].return_value = {}
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    assert draft(context).stage == "tags"
    workflow[2].assert_not_called()
    await press(update, context, "done")
    workflow[2].assert_called_once_with("https://example.com", 1, {})



@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["collections", "tags", "done_button"])
@pytest.mark.parametrize("intruder", [201, 100])
async def test_other_member_including_owner_cannot_use_buttons(update, context, workflow, stage, intruder):
    group(update)
    await bot.handle_message(update, context)
    if stage != "collections":
        await press(update, context, "pick", 1)
    session = draft(context)
    action = {"collections": ("pick", 1), "tags": ("pick", 3), "done_button": ("done", None)}[stage]
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
    await press(update, context, "back")
    assert draft(context).stage == "collections"
    await press(update, context, "pick", 2)
    assert draft(context).chosen == {3: "Knots"}
    workflow[2].assert_not_called()
    await press(update, context, "done")
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
    assert draft(context).stage == "tags"
    workflow[2].side_effect = ApiError("Неизвестный результат", outcome_unknown=True)
    await press(update, context, "done")
    assert "неизвестен" in update.callback_query.edit_message_text.await_args.kwargs["text"]



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
    data = data_for(context, "done")
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
        request.assert_not_called()
        await press(update, context, "done")
        assert request.call_args.kwargs["json"] == {
            "url": "https://example.com", "collection": {"id": 8},
            "tags": [{"id": 9, "name": "Knots"}],
        }
        # A second source message gets a new session but reuses the saved link.
        update.message.message_id = 24
        await bot.handle_message(update, context)
        await press(update, context, "pick", 8)
        await press(update, context, "done")
        assert request.call_count == 2
        assert "уже есть" in update.callback_query.edit_message_text.await_args.kwargs["text"]



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
    assert ("back", None) not in session.actions.values()
    assert "Fishing" in update.message.reply_text.await_args.kwargs["text"]
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 9
    await press(update, context, "pick", 3)
    workflow[2].assert_not_called()
    await press(update, context, "done")
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
    assert draft(context).stage == "tags"
    workflow[2].assert_not_called()
    await press(update, context, "done")
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

@pytest.mark.asyncio
async def test_bind_command_requires_confirmation_and_persists(update, context, workflow):
    from submissions import get_binding
    group(update, user_id=100, thread=9)
    await bot.bind_chat(update, context)
    assert draft(context).purpose == "bind"
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 9
    await press(update, context, "pick", 2)
    assert get_binding(-1001872870761, 9) is None
    assert "Привязать" in update.callback_query.edit_message_text.await_args.kwargs["text"]
    await press(update, context, "bind_save")
    assert get_binding(-1001872870761, 9) == 2
    # Simulate a new process context. The SQLite mapping remains.
    context.bot_data.clear()
    update.message.message_id = 24
    group(update, user_id=200, thread=9)
    await bot.handle_message(update, context)
    assert draft(context).collection == 2 and draft(context).stage == "tags"
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_bind_command_rejects_normal_member(update, context, workflow):
    group(update, user_id=200, thread=9)
    await bot.bind_chat(update, context)
    workflow[0].assert_not_called()
    assert not context.bot_data
    assert "владелец" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_bind_cancel_and_back_do_not_write(update, context, workflow):
    from submissions import get_binding
    group(update, user_id=100, thread=9)
    await bot.bind_chat(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "back")
    assert draft(context).stage == "collections"
    await press(update, context, "pick", 1)
    await press(update, context, "cancel")
    assert get_binding(-1001872870761, 9) is None
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_bind_refetches_permissions_before_commit(update, context, workflow):
    from submissions import get_binding
    group(update, user_id=100, thread=9)
    workflow[0].side_effect = [{2: "Fishing"}, {}]
    await bot.bind_chat(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "bind_save")
    assert draft(context).error and draft(context).stage == "collections"
    assert get_binding(-1001872870761, 9) is None


@pytest.mark.asyncio
async def test_bind_callback_rechecks_owner(update, context, workflow, monkeypatch):
    from submissions import get_binding
    group(update, user_id=100, thread=9)
    await bot.bind_chat(update, context)
    await press(update, context, "pick", 2)
    data = data_for(context, "bind_save")
    # If ownership configuration changes, even the session creator loses access.
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_ID", 999)
    await press(update, context, data=data)
    assert get_binding(-1001872870761, 9) is None
    assert draft(context).stage == "confirm"


@pytest.mark.asyncio
async def test_private_chat_can_be_bound_by_owner(update, context, workflow):
    from submissions import get_binding
    await bot.bind_chat(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "bind_save")
    assert get_binding(100, None) == 2
    update.message.message_id = 24
    await bot.handle_message(update, context)
    assert draft(context).collection == 2


@pytest.mark.asyncio
async def test_bind_unauthorized_chat_and_api_failure(update, context, workflow):
    group(update, user_id=100, thread=9)
    update.effective_chat.id = -999
    await bot.bind_chat(update, context)
    workflow[0].assert_not_called()
    update.effective_chat.id = -1001872870761
    workflow[0].side_effect = [ApiError("HTTP 503"), {2: "Fishing"}]
    await bot.bind_chat(update, context)
    assert draft(context).error == "HTTP 503"
    await press(update, context, "retry")
    await press(update, context, "pick", 2)
    assert draft(context).stage == "confirm"


@pytest.mark.asyncio
async def test_stored_binding_overrides_config_only_in_its_scope(update, context, workflow, monkeypatch):
    from submissions import set_binding
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 1})
    set_binding(-1001872870761, 9, 2)
    group(update, thread=9)
    await bot.handle_message(update, context)
    assert draft(context).collection == 2
    context.bot_data.clear()
    group(update, thread=42)
    await bot.handle_message(update, context)
    assert draft(context).stage == "collections"

@pytest.mark.asyncio
async def test_unbind_restores_collection_picker_after_restart(update, context, workflow):
    from submissions import get_binding, set_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.unbind_chat(update, context)
    assert get_binding(-1001872870761, 9) == 2
    assert draft(context).purpose == "unbind"
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 9
    await press(update, context, "unbind_save")
    assert get_binding(-1001872870761, 9) == 0
    context.bot_data.clear()
    group(update, user_id=200, thread=9)
    update.message.message_id = 24
    await bot.handle_message(update, context)
    assert draft(context).stage == "collections" and draft(context).bound_collection is None
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_unbind_cancel_preserves_assignment(update, context, workflow):
    from submissions import get_binding, set_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.unbind_chat(update, context)
    await press(update, context, "cancel")
    assert get_binding(-1001872870761, 9) == 2


@pytest.mark.asyncio
async def test_unbind_overrides_environment_binding(update, context, workflow, monkeypatch):
    from submissions import get_binding
    monkeypatch.setattr(bot, "TOPIC_COLLECTIONS", {9: 2})
    group(update, user_id=100, thread=9)
    await bot.unbind_chat(update, context)
    await press(update, context, "unbind_save")
    assert get_binding(-1001872870761, 9) == 0
    assert await bot.collection_for_chat(-1001872870761, 9) is None
    assert await bot.collection_for_chat(-1001872870761, 42) is None


@pytest.mark.asyncio
async def test_unbind_requires_owner_for_command_and_callback(update, context, workflow):
    from submissions import get_binding, set_binding
    group(update, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.unbind_chat(update, context)
    assert not context.bot_data
    group(update, user_id=100, thread=9)
    await bot.unbind_chat(update, context)
    data = data_for(context, "unbind_save")
    update.effective_user.id = 200
    await press(update, context, data=data)
    assert get_binding(-1001872870761, 9) == 2


@pytest.mark.asyncio
async def test_unbind_is_independent_of_linkwarden_api(update, context, workflow):
    from submissions import set_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    workflow[0].side_effect = ApiError("HTTP 503")
    await bot.unbind_chat(update, context)
    await press(update, context, "unbind_save")
    workflow[0].assert_not_called()
    workflow[1].assert_not_called()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_unbind_repeated_confirmation_and_changed_assignment(update, context, workflow):
    from submissions import get_binding, set_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.unbind_chat(update, context)
    data = data_for(context, "unbind_save")
    set_binding(-1001872870761, 9, 1)
    await press(update, context, data=data)
    assert get_binding(-1001872870761, 9) == 1
    assert "изменилась" in update.callback_query.edit_message_text.await_args.args[0]
    await bot.unbind_chat(update, context)
    data = data_for(context, "unbind_save")
    await asyncio.gather(press(update, context, data=data), press(update, context, data=data))
    assert get_binding(-1001872870761, 9) == 0


@pytest.mark.asyncio
async def test_unbind_only_current_scope_and_rebind(update, context, workflow):
    from submissions import get_binding, set_binding
    set_binding(-1001872870761, 9, 2)
    set_binding(-1001872870761, 42, 1)
    set_binding(100, None, 2)
    await bot.unbind_chat(update, context)
    await press(update, context, "unbind_save")
    assert get_binding(100, None) == 0
    assert get_binding(-1001872870761, 9) == 2
    assert get_binding(-1001872870761, 42) == 1
    await bot.bind_chat(update, context)
    await press(update, context, "pick", 1)
    await press(update, context, "bind_save")
    assert get_binding(100, None) == 1


@pytest.mark.asyncio
async def test_unbind_expired_confirmation_does_not_write(update, context, workflow):
    from submissions import get_binding, set_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.unbind_chat(update, context)
    data = data_for(context, "unbind_save")
    draft(context).created -= SESSION_TTL + 1
    await press(update, context, data=data)
    assert get_binding(-1001872870761, 9) == 2


@pytest.mark.asyncio
async def test_unbind_already_unbound(update, context, workflow):
    await bot.unbind_chat(update, context)
    assert "без привязки" in update.message.reply_text.await_args.args[0]
    assert not context.bot_data
    workflow[0].assert_not_called()

async def prepare_scan(update, context):
    import json
    group(update, user_id=100, thread=9)
    await bot.scan_chat(update, context)
    payload = json.dumps({"id": 1872870761, "messages": [
        {"id": 9, "type": "service", "action": "topic_created"},
        {"id": 10, "type": "message", "reply_to_message_id": 9, "text": "https://first.com"},
        {"id": 11, "type": "message", "reply_to_message_id": 9, "text": "https://second.com"},
    ]}).encode()
    update.message.document.file_name = "result.json"
    update.message.document.file_size = len(payload)
    context.bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=payload)))
    context.bot.id = 77
    update.message.message_id = 24
    await bot.scan_document(update, context)


@pytest.mark.asyncio
async def test_scan_preview_does_not_save_until_confirmation(update, context, workflow):
    from scan_jobs import ScanStore
    await prepare_scan(update, context)
    assert draft(context).purpose == "scan" and len(draft(context).urls) == 2
    assert "Уникальных URL: 2" in update.message.reply_text.await_args.kwargs["text"]
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    await press(update, context, "done")
    assert ScanStore().active(-1001872870761, 9) is None
    workflow[2].assert_not_called()
    data = data_for(context, "save")
    with patch("bot.start_scan_task") as start:
        await press(update, context, data=data)
        await press(update, context, data=data)
        start.assert_called_once()
    job = ScanStore().active(-1001872870761, 9)
    assert job["urls"] == ["https://first.com", "https://second.com"]
    assert job["collection_id"] == 2 and job["tags"] == {3: "Knots"}
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_scan_resume_control_and_cancel(update, context, workflow):
    from scan_jobs import ScanStore
    await prepare_scan(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    with patch("bot.start_scan_task"):
        await press(update, context, "save")
    job = ScanStore().active(-1001872870761, 9)
    await bot.scan_chat(update, context)
    assert draft(context).purpose == "scan_control"
    data = data_for(context, "scan_cancel")
    update.effective_user.id = 201
    await press(update, context, data=data)
    assert ScanStore().get(job["id"])["state"] == "queued"
    update.effective_user.id = 100
    await press(update, context, data=data)
    assert ScanStore().get(job["id"])["state"] == "cancelled"


@pytest.mark.asyncio
async def test_scan_document_requires_pending_owner_and_same_topic(update, context, workflow):
    group(update, user_id=200, thread=9)
    await bot.scan_chat(update, context)
    assert not context.bot_data
    group(update, user_id=100, thread=9)
    await bot.scan_chat(update, context)
    context.bot.get_file = AsyncMock()
    group(update, user_id=200, thread=9)
    await bot.scan_document(update, context)
    group(update, user_id=100, thread=42)
    await bot.scan_document(update, context)
    context.bot.get_file.assert_not_called()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_scan_bad_export_can_be_replaced(update, context, workflow):
    group(update, user_id=100, thread=9)
    await bot.scan_chat(update, context)
    update.message.document.file_name = "result.json"
    update.message.document.file_size = 5
    context.bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=b"bad")))
    await bot.scan_document(update, context)
    assert draft(context).purpose == "scan_upload"
    assert "JSON" in update.message.reply_text.await_args.args[0]
    await press(update, context, "cancel")
    assert not context.bot_data["selection_sessions"]
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_scan_wrong_file_size_and_general_topic(update, context, workflow):
    group(update, user_id=100, thread=1)
    await bot.scan_chat(update, context)
    assert not context.bot_data
    group(update, user_id=100, thread=9)
    await bot.scan_chat(update, context)
    context.bot.get_file = AsyncMock()
    update.message.document.file_name = "result.html"
    await bot.scan_document(update, context)
    update.message.document.file_name = "result.json"
    update.message.document.file_size = 11 * 1024 * 1024
    await bot.scan_document(update, context)
    context.bot.get_file.assert_not_called()

@pytest.mark.asyncio
async def test_settings_menu_shows_current_scope_and_default_tags(update, context, workflow):
    from submissions import set_binding, set_default_tags
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    set_default_tags(-1001872870761, 9, (3,))
    await bot.settings_chat(update, context)
    summary = update.message.reply_text.await_args.kwargs["text"]
    assert "Fishing" in summary and "Knots" in summary
    assert update.message.reply_text.await_args.kwargs["message_thread_id"] == 9
    assert draft(context).purpose == "settings"
    assert ("settings_unbind", None) in draft(context).actions.values()


@pytest.mark.asyncio
async def test_settings_tags_confirm_then_preselect_for_any_member(update, context, workflow):
    from submissions import get_default_tags, set_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.settings_chat(update, context)
    await press(update, context, "settings_tags")
    await press(update, context, "pick", 3)
    await press(update, context, "done")
    assert get_default_tags(-1001872870761, 9) == ()
    await press(update, context, "tags_save")
    assert get_default_tags(-1001872870761, 9) == (3,)
    group(update, user_id=200, thread=9)
    update.message.message_id = 24
    await bot.handle_message(update, context)
    assert draft(context).chosen == {3: "Knots"}
    await press(update, context, "pick", 3)
    assert draft(context).chosen == {}
    await press(update, context, "done")
    workflow[2].assert_called_once_with("https://example.com", 2, {})



@pytest.mark.asyncio
async def test_settings_rebind_and_unbind_use_existing_flow(update, context, workflow):
    from submissions import set_binding, get_binding
    group(update, user_id=100, thread=9)
    set_binding(-1001872870761, 9, 2)
    await bot.settings_chat(update, context)
    await press(update, context, "settings_bind")
    assert draft(context).bound_collection is None
    await press(update, context, "pick", 1)
    await press(update, context, "bind_save")
    assert get_binding(-1001872870761, 9) == 1
    await bot.settings_chat(update, context)
    await press(update, context, "settings_unbind")
    await press(update, context, "unbind_save")
    assert get_binding(-1001872870761, 9) == 0


@pytest.mark.asyncio
async def test_settings_defaults_cancel_back_and_clear(update, context, workflow):
    from submissions import get_default_tags, set_default_tags
    set_default_tags(100, None, (3,))
    await bot.settings_chat(update, context)
    await press(update, context, "settings_tags")
    assert draft(context).chosen == {3: "Knots"}
    await press(update, context, "pick", 4)
    await press(update, context, "back")
    assert draft(context).purpose == "settings"
    assert get_default_tags(100, None) == (3,)
    await press(update, context, "settings_tags")
    await press(update, context, "pick", 3)
    await press(update, context, "done")
    await press(update, context, "tags_save")
    assert get_default_tags(100, None) == ()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_settings_owner_checks_command_and_each_callback(update, context, workflow, monkeypatch):
    from submissions import get_default_tags
    group(update, user_id=200, thread=9)
    await bot.settings_chat(update, context)
    assert not context.bot_data
    workflow[0].assert_not_called()
    group(update, user_id=100, thread=9)
    await bot.settings_chat(update, context)
    data = data_for(context, "settings_tags")
    update.effective_user.id = 200
    await press(update, context, data=data)
    assert draft(context).purpose == "settings"
    update.effective_user.id = 100
    await press(update, context, data=data)
    await press(update, context, "done")
    data = data_for(context, "tags_save")
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_ID", 999)
    await press(update, context, data=data)
    assert get_default_tags(-1001872870761, 9) == ()


@pytest.mark.asyncio
async def test_settings_survives_api_failure_and_handles_deleted_tag(update, context, workflow):
    from submissions import set_default_tags, get_default_tags
    set_default_tags(100, None, (3,))
    workflow[0].side_effect = [ApiError("HTTP 503"), {1: "Inbox"}]
    workflow[1].side_effect = [ApiError("HTTP 503"), {3: "Knots"}, {}]
    await bot.settings_chat(update, context)
    assert "недоступна" in update.message.reply_text.await_args.kwargs["text"]
    await press(update, context, "settings_tags")
    await press(update, context, "done")
    await press(update, context, "tags_save")
    assert draft(context).error
    assert get_default_tags(100, None) == (3,)
    workflow[1].side_effect = None
    workflow[1].return_value = {}
    await press(update, context, "retry")
    await press(update, context, "done")
    await press(update, context, "tags_save")
    assert get_default_tags(100, None) == ()


@pytest.mark.asyncio
async def test_topic_defaults_do_not_leak_and_scan_uses_its_defaults(update, context, workflow):
    from submissions import set_default_tags
    set_default_tags(-1001872870761, 9, (3,))
    group(update, user_id=100, thread=42)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    assert draft(context).chosen == {}
    context.bot_data.clear()
    await prepare_scan(update, context)
    await press(update, context, "pick", 1)
    assert draft(context).purpose == "scan" and draft(context).chosen == {3: "Knots"}


@pytest.mark.asyncio
async def test_missing_default_tag_is_not_reintroduced(update, context, workflow):
    from submissions import set_default_tags
    set_default_tags(100, None, (3, 99))
    await bot.handle_message(update, context)
    await press(update, context, "pick", 1)
    assert draft(context).chosen == {3: "Knots"}

@pytest.mark.asyncio
async def test_saved_message_has_open_button_and_actual_metadata(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    with patch("bot.get_link_metadata", return_value=("Actual collection", ("Actual tag",))):
        await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args.kwargs
    assert "Actual collection" in reply["text"] and "Actual tag" in reply["text"]
    button = reply["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть в Linkwarden"
    assert button.url == "https://linkwarden.example.com/links/91"
    assert button.callback_data is None



@pytest.mark.asyncio
async def test_duplicate_result_does_not_claim_requested_metadata(update, context, workflow):
    workflow[2].return_value = (91, False)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    with patch("bot.get_link_metadata", return_value=("Original", ("Old tag",))):
        await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args.kwargs
    assert "уже есть" in reply["text"] and "Original" in reply["text"]
    assert "Fishing" not in reply["text"] and "Knots" not in reply["text"]



@pytest.mark.asyncio
async def test_metadata_failure_after_post_preserves_success(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args.kwargs
    assert "сохранена" in reply["text"] and "Fishing" in reply["text"] and "Knots" in reply["text"]
    assert "reply_markup" in reply
    workflow[2].assert_called_once()



@pytest.mark.asyncio
async def test_duplicate_metadata_failure_does_not_show_proposed_selection(update, context, workflow):
    workflow[2].return_value = (91, False)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    text = update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert "временно недоступны" in text and "Fishing" not in text



@pytest.mark.asyncio
async def test_failed_save_never_shows_success_button(update, context, workflow):
    workflow[2].side_effect = ApiError("Timeout", outcome_unknown=True)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args.kwargs
    assert "неизвестен" in reply["text"] and "reply_markup" not in reply



@pytest.mark.asyncio
async def test_invalid_public_url_keeps_success_without_exposing_secret(update, context, workflow, monkeypatch):
    monkeypatch.setenv("LINKWARDEN_PUBLIC_URL", "https://user:secret@example.com")
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args.kwargs
    assert "сохранена" in reply["text"]
    assert all(button.url is None for row in reply["reply_markup"].inline_keyboard for button in row)
    assert "secret" not in str(reply)



def result_item(link_id):
    return {"id": link_id, "name": f"Result {link_id}", "collection": "Fishing", "tags": ("Knots",)}


@pytest.fixture
def search_transport():
    with patch("bot.search_links", return_value=((result_item(12),), None)) as search:
        yield search


@pytest.mark.asyncio
async def test_group_search_is_available_to_members_and_topic_scoped(update, context, workflow, search_transport):
    from submissions import set_binding
    group(update, user_id=200, thread=9)
    set_binding(-1001872870761, 9, 2)
    context.args = ["узлы"]
    await bot.search_chat(update, context)
    search_transport.assert_called_once_with("узлы", 2, None)
    assert draft(context).purpose == "search"
    reply = update.message.reply_text.await_args.kwargs
    assert reply["message_thread_id"] == 9
    button = reply["reply_markup"].inline_keyboard[0][0]
    assert button.url == "https://linkwarden.example.com/links/12"
    assert button.callback_data is None
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_private_search_all_accessible_collections(update, context, workflow, search_transport):
    context.args = ["knot", "fishing"]
    await bot.search_chat(update, context)
    search_transport.assert_called_once_with("knot fishing", None, None)


@pytest.mark.asyncio
async def test_unbound_group_search_does_not_read_account_history(update, context, workflow, search_transport):
    group(update, user_id=200, thread=9)
    context.args = ["knot"]
    await bot.search_chat(update, context)
    search_transport.assert_not_called()
    assert "/bind" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_search_local_and_remote_pages_and_back(update, context, workflow, search_transport):
    context.args = ["knot"]
    search_transport.side_effect = [
        (tuple(result_item(i) for i in range(1, 8)), 8),
        ((result_item(8),), None),
    ]
    await bot.search_chat(update, context)
    await press(update, context, "search_next")
    assert draft(context).page == 1
    assert search_transport.call_count == 1
    await press(update, context, "search_next")
    assert draft(context).search_batch == 1 and draft(context).page == 0
    search_transport.assert_called_with("knot", None, 8)
    await press(update, context, "search_prev")
    assert draft(context).search_batch == 0 and draft(context).page == 1
    await press(update, context, "search_prev")
    assert draft(context).page == 0 and search_transport.call_count == 2


@pytest.mark.asyncio
async def test_search_error_retry_and_empty_results(update, context, workflow, search_transport):
    context.args = ["knot"]
    search_transport.side_effect = [ApiError("HTTP 503"), ((), None)]
    await bot.search_chat(update, context)
    assert "HTTP 503" in update.message.reply_text.await_args.kwargs["text"]
    await press(update, context, "search_refresh")
    assert "совпадений нет" in update.callback_query.edit_message_text.await_args.kwargs["text"]
    assert draft(context).error == ""


@pytest.mark.asyncio
async def test_search_next_failure_keeps_current_page_for_retry(update, context, workflow, search_transport):
    context.args = ["knot"]
    search_transport.side_effect = [((result_item(1),), 2), ApiError("HTTP 503"), ((result_item(2),), None)]
    await bot.search_chat(update, context)
    await press(update, context, "search_next")
    assert draft(context).error and draft(context).search_batch == 0
    await press(update, context, "search_next")
    assert not draft(context).error and draft(context).search_batch == 1


@pytest.mark.asyncio
async def test_search_resets_on_binding_change_before_cached_navigation(update, context, workflow, search_transport):
    from submissions import set_binding
    group(update, user_id=200, thread=9)
    set_binding(-1001872870761, 9, 2)
    context.args = ["knot"]
    search_transport.return_value = (tuple(result_item(i) for i in range(1, 8)), None)
    await bot.search_chat(update, context)
    data = data_for(context, "search_next")
    set_binding(-1001872870761, 9, 1)
    await press(update, context, data=data)
    assert not context.bot_data["selection_sessions"]
    assert "изменилась" in update.callback_query.edit_message_text.await_args.args[0]
    assert search_transport.call_count == 1


@pytest.mark.asyncio
async def test_search_callbacks_only_for_original_requester(update, context, workflow, search_transport):
    from submissions import set_binding
    group(update, user_id=200, thread=9)
    set_binding(-1001872870761, 9, 2)
    context.args = ["knot"]
    await bot.search_chat(update, context)
    data = data_for(context, "search_refresh")
    for intruder in (201, 100):
        update.effective_user.id = intruder
        await press(update, context, data=data)
    assert search_transport.call_count == 1


@pytest.mark.asyncio
async def test_search_validation_authorization_and_expiry(update, context, workflow, search_transport):
    context.args = []
    await bot.search_chat(update, context)
    context.args = ["x" * 201]
    await bot.search_chat(update, context)
    search_transport.assert_not_called()
    context.args = ["knot"]
    update.effective_user.id = 999
    await bot.search_chat(update, context)
    search_transport.assert_not_called()
    update.effective_user.id = 100
    await bot.search_chat(update, context)
    data = data_for(context, "search_refresh")
    draft(context).created -= SESSION_TTL + 1
    await press(update, context, data=data)
    assert search_transport.call_count == 1


@pytest.mark.asyncio
async def test_search_handles_sparse_pages_and_repeated_cursor(update, context, workflow, search_transport):
    context.args = ["knot"]
    search_transport.side_effect = [((), 50), ((), 100), ((result_item(1),), 50)]
    await bot.search_chat(update, context)
    await press(update, context, "search_next")
    await press(update, context, "search_next")
    await press(update, context, "search_next")
    assert "Повтор страницы" in draft(context).error
    assert search_transport.call_count == 3

@pytest.mark.asyncio
async def test_search_message_fits_telegram_with_long_unicode_metadata(update, context, workflow, search_transport):
    context.args = ["🐟" * 200]
    search_transport.return_value = (tuple({
        "id": i, "name": "🐟" * 500, "collection": "🐟" * 500,
        "tags": ("🐟" * 500,) * 50,
    } for i in range(1, 6)), None)
    await bot.search_chat(update, context)
    text = update.message.reply_text.await_args.kwargs["text"]
    assert len(text.encode("utf-16-le")) // 2 <= 4096

@pytest.mark.asyncio
async def test_save_adds_owner_bound_preservation_refresh(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    assert draft(context).purpose == "preservation"
    data = data_for(context, "preservation_refresh")
    update.effective_user.id = 201
    with patch("bot.get_preservation_status") as status:
        await press(update, context, data=data)
        status.assert_not_called()
    update.effective_user.id = 100
    with patch("bot.get_preservation_status", return_value=(("PDF: ✅ готов",), False)):
        await press(update, context, data=data)
    assert "PDF: ✅ готов" in update.callback_query.edit_message_text.await_args.kwargs["text"]
    workflow[2].assert_called_once()



@pytest.mark.asyncio
async def test_pending_new_link_starts_bounded_watcher(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    with patch("bot.get_preservation_status", return_value=(("Скриншот: ⏳ ожидается",), True)), patch("bot.start_preservation_watch") as start:
        await press(update, context, "done")
        start.assert_called_once()
    assert draft(context).preservation_pending



@pytest.mark.asyncio
async def test_preservation_api_error_does_not_undo_success(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    with patch("bot.get_preservation_status", side_effect=ApiError("Статус временно недоступен")), patch("bot.start_preservation_watch"):
        await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args.kwargs
    assert "сохранена" in reply["text"] and "временно недоступен" in reply["text"]
    assert draft(context).purpose == "preservation"
    workflow[2].assert_called_once()



@pytest.mark.asyncio
async def test_watcher_updates_once_when_ready(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    with patch("bot.get_preservation_status", return_value=(("Скриншот: ⏳ ожидается",), True)), patch("bot.start_preservation_watch"):
        await press(update, context, "done")
    session_id = draft(context).session_id
    context.bot.edit_message_text = AsyncMock()
    with patch("bot.get_preservation_status", return_value=(("Скриншот: ✅ готов",), False)) as status, patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.watch_preservation(session_id, context)
    status.assert_called_once_with(91)
    context.bot.edit_message_text.assert_awaited_once()
    assert context.bot.edit_message_text.await_args.kwargs["message_id"] == 55
    assert draft(context).preservation_pending is False
    workflow[2].assert_called_once()



@pytest.mark.asyncio
async def test_watcher_limit_and_no_repeated_identical_edits(update, context, workflow, monkeypatch):
    pending = (("Скриншот: ⏳ ожидается",), True)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    with patch("bot.get_preservation_status", return_value=pending), patch("bot.start_preservation_watch"):
        await press(update, context, "done")
    context.bot.edit_message_text = AsyncMock()
    monkeypatch.setattr(bot, "PRESERVATION_MAX_POLLS", 2)
    with patch("bot.get_preservation_status", return_value=pending) as status, patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.watch_preservation(draft(context).session_id, context)
    assert status.call_count == 2
    context.bot.edit_message_text.assert_not_awaited()



@pytest.mark.asyncio
async def test_watcher_skips_busy_session_and_exits_if_expired(update, context, workflow, monkeypatch):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    with patch("bot.get_preservation_status", return_value=(("pending",), True)), patch("bot.start_preservation_watch"):
        await press(update, context, "done")
    session = draft(context)
    session.busy = True
    monkeypatch.setattr(bot, "PRESERVATION_MAX_POLLS", 2)
    with patch("bot.get_preservation_status") as status, patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.watch_preservation(session.session_id, context)
        status.assert_not_called()
        session.busy = False
        session.created -= SESSION_TTL + 1
        await bot.watch_preservation(session.session_id, context)
        status.assert_not_called()



@pytest.mark.asyncio
async def test_preservation_watcher_capacity(update, context, workflow):
    await bot.handle_message(update, context)
    session = draft(context)
    context.bot_data["preservation_tasks"] = {str(i): object() for i in range(bot.MAX_PRESERVATION_WATCHERS)}
    bot.start_preservation_watch(session, context)
    context.application.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_preservation_manual_refresh_checks_topic(update, context, workflow):
    group(update, thread=9)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "done")
    data = data_for(context, "preservation_refresh")
    update.callback_query.message.message_thread_id = 42
    with patch("bot.get_preservation_status") as status:
        await press(update, context, data=data)
        status.assert_not_called()



@pytest.mark.asyncio
async def test_done_saves_without_an_additional_confirmation_screen(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    assert any(button.text == "Готово — сохранить" for row in keyboard.inline_keyboard for button in row)
    data = data_for(context, "done")
    workflow[2].assert_not_called()
    await press(update, context, data=data)
    workflow[2].assert_called_once_with("https://example.com", 2, {3: "Knots"})
    assert draft(context).purpose == "preservation"
    keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    assert all(button.text != "Сохранить" for row in keyboard.inline_keyboard for button in row)
    await press(update, context, data=data)
    workflow[2].assert_called_once()


@pytest.mark.asyncio
async def test_cancel_in_tag_menu_invalidates_direct_save_button(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    data = data_for(context, "done")
    await press(update, context, "cancel")
    await press(update, context, data=data)
    workflow[2].assert_not_called()


async def begin_creation(update, context, action):
    context.bot.id = 77
    context.bot.send_message.return_value.message_id = 66
    context.bot.edit_message_text = AsyncMock()
    await press(update, context, action)
    update.message.reply_to_message = SimpleNamespace(
        message_id=66, from_user=SimpleNamespace(id=77),
        text=context.bot.send_message.await_args.kwargs["text"],
    )
    update.message.message_id = 24


@pytest.mark.asyncio
async def test_new_collection_is_selected_without_saving_url(update, context, workflow):
    await bot.handle_message(update, context)
    await begin_creation(update, context, "new_collection")
    prompt = context.bot.send_message.await_args.kwargs
    assert prompt["reply_parameters"].message_id == 23
    assert prompt["reply_markup"].selective
    update.message.text = "Trips"
    with patch("bot.create_named_item", return_value=(9, "Trips")) as create:
        assert await bot.handle_creation_reply(update, context)
        create.assert_called_once_with("collection", "Trips")
    assert draft(context).collection == 9 and draft(context).stage == "tags"
    workflow[2].assert_not_called()
    update.callback_query.message.message_id = draft(context).message_id
    await press(update, context, "done")
    workflow[2].assert_called_once_with("https://example.com", 9, {})


@pytest.mark.asyncio
async def test_new_tag_is_checked_and_prior_choices_remain(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    await begin_creation(update, context, "new_tag")
    update.message.text = "New tag"
    with patch("bot.create_named_item", return_value=(9, "New tag")):
        assert await bot.handle_creation_reply(update, context)
    assert draft(context).chosen == {3: "Knots", 9: "New tag"}
    menu = context.bot.send_message.await_args.kwargs
    assert menu["reply_parameters"].message_id == 24
    buttons = [button.text for row in menu["reply_markup"].inline_keyboard for button in row]
    assert "✓ New tag" in buttons
    assert "Новый тег" in buttons and "Готово — сохранить" in buttons
    context.bot.edit_message_reply_markup.assert_awaited_once_with(
        chat_id=100, message_id=55, reply_markup=None
    )
    workflow[2].assert_not_called()
    # Buttons on the old message cannot save the link.
    await press(update, context, "done")
    workflow[2].assert_not_called()
    update.callback_query.message.message_id = draft(context).message_id
    await press(update, context, "done")
    workflow[2].assert_called_once_with("https://example.com", 2, {3: "Knots", 9: "New tag"})


@pytest.mark.asyncio
async def test_metadata_creation_not_offered_to_normal_member(update, context, workflow):
    group(update, user_id=200, thread=9)
    await bot.handle_message(update, context)
    assert ("new_collection", None) not in draft(context).actions.values()
    await press(update, context, "pick", 2)
    assert ("new_tag", None) not in draft(context).actions.values()


@pytest.mark.asyncio
async def test_creation_callback_rechecks_owner(update, context, workflow, monkeypatch):
    group(update, user_id=100, thread=9)
    await bot.handle_message(update, context)
    data = data_for(context, "new_collection")
    monkeypatch.setattr(bot, "ALLOWED_TELEGRAM_USER_ID", 999)
    await press(update, context, data=data)
    context.bot.send_message.assert_not_awaited()
    assert not draft(context).creation_kind


@pytest.mark.asyncio
async def test_creation_reply_must_be_from_owner_in_same_topic(update, context, workflow):
    group(update, user_id=100, thread=9)
    await bot.handle_message(update, context)
    await begin_creation(update, context, "new_collection")
    update.message.text = "Trips"
    with patch("bot.create_named_item") as create:
        update.effective_user.id = 201
        assert not await bot.handle_creation_reply(update, context)
        update.effective_user.id = 100
        update.message.message_thread_id = 42
        assert await bot.handle_creation_reply(update, context)
        create.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_name_input_retains_selection_and_ignores_stale_reply(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await press(update, context, "pick", 3)
    await begin_creation(update, context, "new_tag")
    await press(update, context, "creation_back")
    assert draft(context).chosen == {3: "Knots"} and not draft(context).creation_kind
    update.message.text = "New tag"
    with patch("bot.create_named_item") as create:
        assert await bot.handle_creation_reply(update, context)
        create.assert_not_called()
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_creation_error_keeps_prompt_for_retry(update, context, workflow):
    await bot.handle_message(update, context)
    await begin_creation(update, context, "new_collection")
    update.message.text = "Trips"
    with patch("bot.create_named_item", side_effect=[ApiError("Timeout", True), (9, "Trips")]) as create:
        assert await bot.handle_creation_reply(update, context)
        assert draft(context).creation_kind == "collection" and draft(context).error
        assert await bot.handle_creation_reply(update, context)
        assert draft(context).collection == 9 and not draft(context).creation_kind
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_creation_router_stops_url_handler_for_name_reply(update, context, workflow):
    from telegram.ext import ApplicationHandlerStop
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await begin_creation(update, context, "new_tag")
    update.message.text = "https://name.example"
    with patch("bot.create_named_item", return_value=(9, "https://name.example")):
        with pytest.raises(ApplicationHandlerStop):
            await bot.route_creation_reply(update, context)
    assert len(context.bot_data["selection_sessions"]) == 1
    workflow[2].assert_not_called()


@pytest.mark.asyncio
async def test_creation_prompt_expiry_does_not_create(update, context, workflow):
    await bot.handle_message(update, context)
    await begin_creation(update, context, "new_collection")
    draft(context).created -= SESSION_TTL + 1
    update.message.text = "Trips"
    with patch("bot.create_named_item") as create:
        assert await bot.handle_creation_reply(update, context)
        create.assert_not_called()
    assert "истекла" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_new_collection_in_bind_still_requires_binding_confirmation(update, context, workflow):
    from submissions import get_binding
    group(update, user_id=100, thread=9)
    await bot.bind_chat(update, context)
    await begin_creation(update, context, "new_collection")
    update.message.text = "Trips"
    with patch("bot.create_named_item", return_value=(9, "Trips")):
        assert await bot.handle_creation_reply(update, context)
    assert draft(context).purpose == "bind" and draft(context).stage == "confirm"
    assert get_binding(-1001872870761, 9) is None


@pytest.mark.asyncio
async def test_new_tag_can_be_used_in_default_preferences(update, context, workflow):
    from submissions import get_default_tags
    await bot.settings_chat(update, context)
    await press(update, context, "settings_tags")
    await begin_creation(update, context, "new_tag")
    update.message.text = "New tag"
    with patch("bot.create_named_item", return_value=(9, "New tag")):
        assert await bot.handle_creation_reply(update, context)
    assert get_default_tags(100, None) == ()
    workflow[1].return_value[9] = "New tag"
    update.callback_query.message.message_id = draft(context).message_id
    await press(update, context, "done")
    await press(update, context, "tags_save")
    assert get_default_tags(100, None) == (9,)


@pytest.mark.asyncio
async def test_created_tag_returns_menu_in_same_topic_and_allows_another_tag(update, context, workflow):
    group(update, user_id=100, thread=9)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await begin_creation(update, context, "new_tag")
    update.message.text = "First"
    context.bot.send_message.return_value.message_id = 67
    with patch("bot.create_named_item", return_value=(9, "First")) as create:
        assert await bot.handle_creation_reply(update, context)
        assert await bot.handle_creation_reply(update, context)
        create.assert_called_once()
    menu = context.bot.send_message.await_args.kwargs
    assert menu["chat_id"] == -1001872870761 and menu["message_thread_id"] == 9
    assert draft(context).message_id == 67
    workflow[1].return_value[9] = "First"
    update.callback_query.message.message_id = 67
    await begin_creation(update, context, "new_tag")
    update.message.text = "Second"
    context.bot.send_message.return_value.message_id = 68
    with patch("bot.create_named_item", return_value=(10, "Second")):
        assert await bot.handle_creation_reply(update, context)
    assert draft(context).chosen == {9: "First", 10: "Second"}
    update.callback_query.message.message_id = 68
    await press(update, context, "done")
    workflow[2].assert_called_once_with("https://example.com", 2, {9: "First", 10: "Second"})


@pytest.mark.asyncio
async def test_created_tag_send_failure_restores_original_menu(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await begin_creation(update, context, "new_tag")
    update.message.text = "New"
    context.bot.send_message.side_effect = TimedOut()
    with patch("bot.create_named_item", return_value=(9, "New")):
        assert await bot.handle_creation_reply(update, context)
    assert draft(context).message_id == 55 and draft(context).stage == "tags"
    assert context.bot.edit_message_text.await_args.kwargs["message_id"] == 55
    await press(update, context, "done")
    workflow[2].assert_called_once_with("https://example.com", 2, {9: "New"})


@pytest.mark.asyncio
async def test_old_keyboard_cleanup_failure_keeps_new_menu_usable(update, context, workflow):
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    await begin_creation(update, context, "new_tag")
    update.message.text = "New"
    context.bot.edit_message_reply_markup.side_effect = NetworkError("offline")
    with patch("bot.create_named_item", return_value=(9, "New")):
        assert await bot.handle_creation_reply(update, context)
    update.callback_query.message.message_id = draft(context).message_id
    await press(update, context, "done")
    workflow[2].assert_called_once()


@pytest.mark.asyncio
async def test_group_duplicate_does_not_reveal_metadata_id_or_status(update, context, workflow):
    group(update, user_id=200, thread=9)
    workflow[2].return_value = (91, False)
    await bot.handle_message(update, context)
    await press(update, context, "pick", 2)
    with patch("bot.get_link_metadata") as metadata, patch("bot.get_preservation_status") as status:
        await press(update, context, "done")
    reply = update.callback_query.edit_message_text.await_args
    assert reply.kwargs == {"text": "Эта ссылка уже есть в Linkwarden."}
    metadata.assert_not_called()
    status.assert_not_called()
    assert not context.bot_data["selection_sessions"]
