import json
import re
from types import SimpleNamespace

import pytest

from scan_export import ExportError, parse_export


CHAT = -1001872870761


def extract(text):
    return re.findall(r"https?://[^\s]+", text)


def content(rows, chat=1872870761):
    return json.dumps({"id": chat, "messages": rows}).encode()


def message(message_id, text, reply=9, **kwargs):
    return dict(id=message_id, type="message", text=text, reply_to_message_id=reply, **kwargs)


def test_topic_filter_hidden_links_and_dedup():
    rows = [
        {"id": 9, "type": "service", "action": "topic_created"},
        {"id": 20, "type": "service", "action": "topic_created"},
        message(10, "https://a.com"),
        message(11, [{"type": "text_link", "text": "A", "href": "https://b.com"}], reply=10),
        message(12, "https://a.com"),
        message(13, "https://other.com", reply=20),
        message(14, "https://unknown.com", reply=999),
        message(15, "https://bot.com", from_id="user77"),
    ]
    result = parse_export(content(rows), CHAT, 9, extract, 77)
    assert result.urls == ("https://a.com", "https://b.com")
    assert result.occurrences == 3 and result.matched_messages == 3
    assert result.other_messages == 1 and result.unknown_messages == 1


def test_entities_and_explicit_topic():
    rows = [message(10, "label", message_thread_id=9,
                    text_entities=[{"type": "text_link", "text": "label", "href": "https://a.com"}])]
    assert parse_export(content(rows), CHAT, 9, extract).urls == ("https://a.com",)


@pytest.mark.parametrize("payload", [b"not json", b"[]", content([], 123),
                                    content([message(10, "https://a.com", reply=99)]),
                                    content([message(10, "https://a.com"), message(10, "https://b.com")])])
def test_bad_or_unscoped_export(payload):
    with pytest.raises(ExportError):
        parse_export(payload, CHAT, 9, extract)


def test_cycles_cannot_be_assigned_to_topic():
    with pytest.raises(ExportError):
        parse_export(content([message(10, "https://a.com", 11), message(11, "text", 10)]), CHAT, 9, extract)


def test_forward_replies_are_resolved():
    rows = [message(12, "https://a.com", 11), message(11, "text", 10), message(10, "text", 9)]
    assert parse_export(content(rows), CHAT, 9, extract).urls == ("https://a.com",)


def test_limits(monkeypatch):
    import scan_export
    monkeypatch.setattr(scan_export, "MAX_EXPORT_BYTES", 3)
    with pytest.raises(ExportError):
        parse_export(b"1234", CHAT, 9, extract)
