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

def test_find_link_paginates_and_compares_exactly(transport):
    from linkwarden import find_link
    transport.side_effect = [
        response({"response": [{"id": 7, "url": "https://example.com/longer"}]}),
        response({"response": [{"id": 3, "url": "https://example.com"}]}),
    ]
    assert find_link("https://example.com") == 3
    assert transport.call_args.kwargs["params"] == {
        "searchQueryString": "https://example.com", "cursor": 7
    }


def test_find_link_unknown_schema_is_not_an_empty_result(transport):
    from linkwarden import find_link
    transport.return_value = response({"data": {"links": []}})
    with pytest.raises(ApiError):
        find_link("https://example.com")


def test_find_link_repeated_page(transport):
    from linkwarden import find_link
    transport.return_value = response({"response": [{"id": 7, "url": "https://other.com"}]})
    with pytest.raises(ApiError):
        find_link("https://example.com")


def test_unknown_post_outcome_is_reported(transport):
    transport.side_effect = requests.Timeout()
    with pytest.raises(ApiError) as caught:
        save_link("https://example.com", 1, {})
    assert caught.value.outcome_unknown


def test_get_error_does_not_claim_unknown_save(transport):
    transport.side_effect = requests.Timeout()
    with pytest.raises(ApiError) as caught:
        get_tags()
    assert not caught.value.outcome_unknown

@pytest.mark.parametrize("public,api,expected", [
    ("", "https://lw.example.com", "https://lw.example.com/links/12"),
    ("https://public.example.com/", "http://linkwarden:3000", "https://public.example.com/links/12"),
    ("https://example.com/lw/", "https://api.example.com", "https://example.com/lw/links/12"),
    ("http://192.168.1.140:3000", "http://internal", "http://192.168.1.140:3000/links/12"),
])
def test_public_link_url(public, api, expected, monkeypatch):
    from linkwarden import public_link_url
    monkeypatch.setenv("LINKWARDEN_API_URL", api)
    monkeypatch.setenv("LINKWARDEN_PUBLIC_URL", public)
    assert public_link_url(12) == expected


@pytest.mark.parametrize("base", [
    "https://user:secret@example.com", "https://example.com?token=secret",
    "https://example.com/#secret", "javascript:alert(1)",
    "http://example.com:99999", "http://example.com:abc", "/relative",
    "https://example.com\n", "http://[invalid", "https://",
])
def test_unsafe_public_urls_do_not_leak_into_buttons(base, monkeypatch):
    from linkwarden import public_link_url
    monkeypatch.setenv("LINKWARDEN_PUBLIC_URL", base)
    assert public_link_url(12) is None


@pytest.mark.parametrize("link_id", [True, 0, -1, "12"])
def test_public_url_requires_link_id(link_id):
    from linkwarden import public_link_url
    assert public_link_url(link_id) is None


def test_actual_link_metadata(transport):
    from linkwarden import get_link_metadata
    transport.return_value = response({"response": {
        "id": 12, "collection": {"name": "Actual collection"},
        "tags": [{"id": 1, "name": "Actual tag"}],
    }})
    assert get_link_metadata(12) == ("Actual collection", ("Actual tag",))
    assert transport.call_args.args[:2] == ("GET", "https://linkwarden.example.com/api/v1/links/12")


@pytest.mark.parametrize("link", [
    {"id": 99, "collection": {"name": "A"}, "tags": []},
    {"id": 12, "collection": None, "tags": []},
    {"id": 12, "collection": {"name": "A"}, "tags": "bad"},
])
def test_bad_link_metadata(transport, link):
    from linkwarden import get_link_metadata
    transport.return_value = response({"response": link})
    with pytest.raises(ApiError):
        get_link_metadata(12)

def test_public_url_rejects_embedded_whitespace(monkeypatch):
    from linkwarden import public_link_url
    monkeypatch.setenv("LINKWARDEN_PUBLIC_URL", "https://ex ample.com")
    assert public_link_url(12) is None

def search_result(link_id, collection_id=2):
    return {"id": link_id, "name": "Result", "collectionId": collection_id,
            "collection": {"name": "Fishing"}, "tags": [{"id": 3, "name": "Knots"}]}


def test_search_api_schema_and_scope(transport):
    from linkwarden import search_links
    transport.return_value = response({"success": True, "data": {"links": [search_result(8)], "nextCursor": 7}})
    rows, cursor = search_links("knot", 2, 9)
    assert rows[0]["name"] == "Result" and rows[0]["tags"] == ("Knots",)
    assert cursor == 7
    assert transport.call_args.kwargs["params"] == {"searchQueryString": "knot", "collectionId": 2, "cursor": 9}


def test_search_private_scope_and_meili_empty(transport):
    from linkwarden import search_links
    transport.return_value = response({"success": True, "data": []})
    assert search_links("nothing") == ((), None)
    assert transport.call_args.kwargs["params"] == {"searchQueryString": "nothing"}


@pytest.mark.parametrize("body", [
    {"success": False, "data": {"links": [], "nextCursor": None}},
    {"success": True, "data": {"links": [], "nextCursor": True}},
    {"success": True, "data": {"links": [], "nextCursor": 9}},
    {"success": True, "data": {"links": [], "nextCursor": 0}},
    {"success": True, "data": {"links": []}},
    {"response": []},
    {"success": True, "data": {"links": [search_result(8, 3)], "nextCursor": None}},
    {"success": True, "data": {"links": [{"id": 1}], "nextCursor": None}},
])
def test_search_rejects_malformed_or_out_of_scope_results(transport, body):
    from linkwarden import search_links
    transport.return_value = response(body)
    with pytest.raises(ApiError):
        search_links("knot", 2, 9)


def test_search_empty_intermediate_page_retains_cursor(transport):
    from linkwarden import search_links
    transport.return_value = response({"success": True, "data": {"links": [], "nextCursor": 100}})
    assert search_links("knot", 2, 50) == ((), 100)
