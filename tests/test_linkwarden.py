from unittest.mock import Mock, patch

import pytest
import requests

from linkwarden import ApiError, get_collections, get_tags, save_link


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("LINKWARDEN_API_URL", "https://linkwarden.example.com")
    monkeypatch.setenv("LINKWARDEN_API_KEY", "test-key")
    with patch("linkwarden.requests.request") as mock:
        yield mock


def response(body, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    return result


def tags_page(rows, cursor=None):
    return {"success": True, "data": {"tags": rows, "nextCursor": cursor}}


def test_collections(transport):
    transport.return_value = response({"response": [{"id": 4, "name": "Inbox", "members": []}]})
    assert get_collections() == {4: "Inbox"}


def test_shared_collections(transport):
    shared = {"ownerId": 1, "members": [{"userId": 2, "canCreate": True}]}
    transport.side_effect = [
        response({"response": [dict(shared, id=1, name="Shared"),
                               dict(shared, id=2, name="Read only", members=[{"userId": 2, "canCreate": False}]),
                               dict(shared, id=3, name="Mine", ownerId=2)]}),
        response({"response": {"id": 2}}),
    ]
    assert get_collections() == {1: "Shared", 3: "Mine"}


def test_paginated_tags(transport):
    transport.side_effect = [response(tags_page([{"id": 7, "name": "A"}], 7)),
                             response(tags_page([{"id": 4, "name": "B"}]))]
    assert get_tags() == {7: "A", 4: "B"}
    assert transport.call_args.kwargs["params"] == {"cursor": 7}


def test_repeated_cursor(transport):
    transport.return_value = response(tags_page([], 1))
    with pytest.raises(ApiError):
        get_tags()
    assert transport.call_count == 2


@pytest.mark.parametrize("load,body", [(get_collections, {"response": []}), (get_tags, tags_page([]))])
def test_empty(transport, load, body):
    transport.return_value = response(body)
    assert load() == {}


@pytest.mark.parametrize("load,body,status", [
    (get_tags, {"response": []}, 200), (get_collections, {"response": None}, 200),
    (get_collections, {"response": [{"id": True, "name": "A"}]}, 200),
    (get_tags, {"success": False, "data": {"tags": [], "nextCursor": None}}, 200),
    (get_collections, {}, 403), (get_tags, {}, 503),
])
def test_bad_responses(transport, load, body, status):
    transport.return_value = response(body, status)
    with pytest.raises(ApiError):
        load()


def test_save_payload(transport):
    transport.return_value = response({"response": {"id": 29}})
    assert save_link("https://example.com", 3, {4: "Python"}) == 29
    args, kwargs = transport.call_args
    assert args == ("POST", "https://linkwarden.example.com/api/v1/links")
    assert kwargs["json"] == {"url": "https://example.com", "collection": {"id": 3},
                              "tags": [{"id": 4, "name": "Python"}]}
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "missing-id", "server-error", "unauthorized"])
def test_save_failure_never_reposts(transport, failure):
    transport.return_value = response({"id": 2})
    if failure == "timeout":
        transport.side_effect = requests.Timeout()
    elif failure == "invalid-json":
        transport.return_value.json.side_effect = ValueError()
    elif failure in ("server-error", "unauthorized"):
        transport.return_value.status_code = 500 if failure == "server-error" else 401
    with pytest.raises(ApiError):
        save_link("https://example.com", 1, {})
    transport.assert_called_once()
