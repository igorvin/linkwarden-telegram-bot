import os
import pytest
from linkwarden import ApiError
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace
from unittest.mock import Mock

# Set required environment variables before importing bot
os.environ.setdefault('TELEGRAM_TOKEN', 'test-token')
os.environ.setdefault('LINKWARDEN_API_URL', 'https://linkwarden.example.com')
os.environ.setdefault('LINKWARDEN_API_KEY', 'test-api-key')
os.environ['ALLOWED_TELEGRAM_USER_ID'] = '100'

from bot import (
    extract_links,
    handle_message,
    handle_callback,
    start,
    send_message_with_retry,
    error_handler,
    check_rate_limit,
    user_message_history,
    RATE_LIMIT_THRESHOLD,
)


@pytest.fixture
def mock_update():
    mock = MagicMock()
    mock.effective_chat.type = "private"
    mock.effective_chat.id = 12345
    mock.effective_user.id = 100
    mock.message.text = "Check out this link: https://example.com"
    return mock


@pytest.fixture
def mock_context():
    context = MagicMock()
    context.user_data = {}
    context.bot.send_message = AsyncMock()
    return context


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


class TestHandleMessage:
    @pytest.mark.asyncio
    @patch('bot.get_collections', return_value={1: "Inbox"})
    @patch('bot.check_rate_limit', return_value=True)
    async def test_offers_collection(self, mock_rate, mock_get, mock_update, mock_context):
        await handle_message(mock_update, mock_context)
        keyboard = mock_context.bot.send_message.await_args.kwargs["reply_markup"]
        assert keyboard.inline_keyboard[0][0].text == "Inbox"

    @pytest.mark.asyncio
    @patch('bot.send_message_with_retry', new_callable=AsyncMock)
    @patch('bot.check_rate_limit', return_value=True)
    async def test_no_links(self, mock_rate, mock_send, mock_update, mock_context):
        mock_update.message.text = "No links here"
        await handle_message(mock_update, mock_context)
        mock_send.assert_called_once_with(mock_update, mock_context, "No links found in the message.")

    @pytest.mark.asyncio
    @patch('bot.send_message_with_retry', new_callable=AsyncMock)
    async def test_rate_limited(self, mock_send, mock_update, mock_context):
        with patch('bot.check_rate_limit', return_value=False):
            await handle_message(mock_update, mock_context)
        mock_send.assert_called_once()
        assert "Rate limit" in mock_send.call_args[0][2]

    @pytest.mark.asyncio
    @patch('bot.send_message_with_retry', new_callable=AsyncMock)
    @patch('bot.check_rate_limit', return_value=True)
    async def test_empty_message(self, mock_rate, mock_send, mock_update, mock_context):
        mock_update.message.text = ""
        await handle_message(mock_update, mock_context)
        mock_send.assert_called_once()
        assert "too large or empty" in mock_send.call_args[0][2].lower()

    @pytest.mark.asyncio
    @patch('bot.send_message_with_retry', new_callable=AsyncMock)
    @patch('bot.check_rate_limit', return_value=True)
    async def test_suspicious_content(self, mock_rate, mock_send, mock_update, mock_context):
        mock_update.message.text = "javascript:alert(1)"
        await handle_message(mock_update, mock_context)
        mock_send.assert_called_once()
        assert "Suspicious" in mock_send.call_args[0][2]

    @pytest.mark.asyncio
    @patch('bot.send_message_with_retry', new_callable=AsyncMock)
    @patch('bot.check_rate_limit', return_value=True)
    async def test_too_many_links(self, mock_rate, mock_send, mock_update, mock_context):
        links = " ".join([f"https://site{i}.com" for i in range(15)])
        mock_update.message.text = links
        await handle_message(mock_update, mock_context)
        mock_send.assert_called_once()
        assert "Too many links" in mock_send.call_args[0][2]


class TestSendMessageWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self, mock_update, mock_context):
        await send_message_with_retry(mock_update, mock_context, "hello")
        mock_context.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self, mock_update, mock_context):
        from telegram.error import TimedOut
        mock_context.bot.send_message.side_effect = [TimedOut(), TimedOut(), None]
        await send_message_with_retry(mock_update, mock_context, "hello")
        assert mock_context.bot.send_message.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, mock_update, mock_context):
        from telegram.error import TimedOut
        mock_context.bot.send_message.side_effect = TimedOut()
        with pytest.raises(TimedOut):
            await send_message_with_retry(mock_update, mock_context, "hello", max_retries=2)
        assert mock_context.bot.send_message.call_count == 2


class TestErrorHandler:
    @pytest.mark.asyncio
    async def test_logs_error(self, mock_update, mock_context):
        mock_context.error = Exception("Test error")
        await error_handler(mock_update, mock_context)
        mock_context.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_network_error_no_message(self, mock_update, mock_context):
        from telegram.error import TimedOut
        mock_context.error = TimedOut()
        await error_handler(mock_update, mock_context)
        mock_context.bot.send_message.assert_not_called()


class TestStart:
    @pytest.mark.asyncio
    async def test_start_sends_welcome(self, mock_update, mock_context):
        await start(mock_update, mock_context)
        mock_context.bot.send_message.assert_called_once()
        text = mock_context.bot.send_message.call_args[1]['text']
        assert "Linkwarden" in text


class TestSelection:
    @pytest.fixture
    def workflow(self, mock_update, mock_context):
        query = mock_update.callback_query
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message.message_id = 55
        mock_context.bot.send_message.return_value.message_id = 55
        with (patch("bot.check_rate_limit", return_value=True),
              patch("bot.get_collections", return_value={1: "A", 2: "B"}) as collections,
              patch("bot.get_tags", return_value={3: "C", 4: "D"}) as tags,
              patch("bot.save_link", return_value=91) as save):
            async def press(action, data=None):
                draft = mock_context.user_data["drafts"].get(query.message.message_id)
                query.data = data or f"lw:{draft.revision}:{action}"
                await handle_callback(mock_update, mock_context)

            yield SimpleNamespace(update=mock_update, context=mock_context, query=query,
                                  collections=collections, tags=tags, save=save, press=press)

    @pytest.mark.asyncio
    async def test_full_flow(self, workflow):
        w = workflow
        await handle_message(w.update, w.context)
        initial = w.context.user_data["drafts"][55].revision
        await w.press("pick:2")
        await w.press("pick:3")
        await w.press("pick:4")
        await w.press("pick:3")
        await w.press("done")
        summary = w.query.edit_message_text.await_args.kwargs["text"]
        assert "https://example.com" in summary and "B (#2)" in summary and "• D" in summary
        assert "• C" not in summary
        await w.press("save", f"lw:{initial}:save")
        w.save.assert_not_called()
        await w.press("save")
        w.save.assert_called_once_with("https://example.com", 2, {4: "D"})
        await w.press("save", w.query.data)
        w.save.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("user_id,chat_type", [(999, "private"), (100, "group")])
    @pytest.mark.parametrize("action", ["pick:1", "retry", "save", "cancel"])
    async def test_unauthorized_button(self, workflow, user_id, chat_type, action):
        w = workflow
        await handle_message(w.update, w.context)
        draft = w.context.user_data["drafts"][55]
        if action == "save":
            await w.press("pick:1")
            await w.press("done")
        elif action == "retry":
            draft.error = "Error"
            draft.view()
        draft = w.context.user_data["drafts"][55]
        revision, actions = draft.revision, draft.actions.copy()
        w.collections.reset_mock()
        w.tags.reset_mock()
        w.query.edit_message_text.reset_mock()
        w.update.effective_user.id = user_id
        w.update.effective_chat.type = chat_type
        await w.press(action)
        assert w.context.user_data["drafts"][55] is draft
        assert draft.revision == revision and draft.actions == actions
        w.collections.assert_not_called()
        w.tags.assert_not_called()
        w.save.assert_not_called()
        w.query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_and_failed_fetch(self, workflow):
        w = workflow
        w.collections.side_effect = [ApiError("HTTP 503"), {}, {1: "A"}]
        w.tags.side_effect = [ApiError("HTTP 503"), {}]
        await handle_message(w.update, w.context)
        await w.press("retry")
        assert "Нет доступных" in w.query.edit_message_text.await_args.kwargs["text"]
        await w.press("retry")
        await w.press("pick:1")
        await w.press("retry")
        assert "Без тегов" in w.query.edit_message_text.await_args.kwargs["text"]
        await w.press("cancel")
        w.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_each_url_has_independent_buttons(self, workflow):
        w = workflow
        w.update.message.text = "https://a.com https://b.com"
        w.context.bot.send_message.side_effect = [Mock(message_id=55), Mock(message_id=56)]
        await handle_message(w.update, w.context)
        a, b = w.context.user_data["drafts"].values()
        assert a.url != b.url
        await w.press("pick:2", f"lw:{b.revision}:pick:2")
        w.tags.assert_not_called()
        await w.press("pick:2")
        assert w.context.user_data["drafts"][55].collection == 2 and b.collection is None

    @pytest.mark.asyncio
    async def test_paging_and_unknown_save(self, workflow):
        w = workflow
        w.tags.return_value = {i: f"tag{i}" for i in range(1, 11)}
        w.save.side_effect = ApiError("Нет ответа")
        await handle_message(w.update, w.context)
        await w.press("pick:1")
        await w.press("next")
        await w.press("pick:10")
        await w.press("prev")
        await w.press("done")
        await w.press("save")
        assert "проверьте коллекцию" in w.query.edit_message_text.await_args.args[0]
        await w.press("save", w.query.data)
        w.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_simultaneous_confirmations(self, workflow):
        import asyncio
        w = workflow
        w.tags.return_value = {}
        await handle_message(w.update, w.context)
        await w.press("pick:1")
        data = "lw:" + w.context.user_data["drafts"][55].revision + ":save"
        await asyncio.gather(w.press("save", data), w.press("save", data))
        w.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejected_keyboard_edit_can_be_retried(self, workflow):
        from telegram.error import NetworkError
        w = workflow
        await handle_message(w.update, w.context)
        w.query.edit_message_text.side_effect = NetworkError("test")
        with pytest.raises(NetworkError):
            await w.press("pick:1")
        original_data = w.query.data
        w.query.edit_message_text.side_effect = None
        await w.press("pick:1", original_data)
        assert w.context.user_data["drafts"][55].stage == "tags"
