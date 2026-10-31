import os

import requests


class ApiError(Exception):
    """Safe to show to the Telegram user."""

    def __init__(self, message, outcome_unknown=False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def request(method, path, **kwargs):
    base = os.environ["LINKWARDEN_API_URL"].rstrip("/")
    headers = {"Authorization": "Bearer " + os.environ["LINKWARDEN_API_KEY"]}
    try:
        response = requests.request(method, base + "/api/v1/" + path, headers=headers,
                                    timeout=10, allow_redirects=False, **kwargs)
        if 400 <= response.status_code < 500:
            raise ApiError(f"Linkwarden отклонил запрос (HTTP {response.status_code}).")
        if not 200 <= response.status_code < 300:
            raise ValueError("Unexpected HTTP status")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data
    except (requests.RequestException, ValueError) as error:
        raise ApiError("Не удалось получить корректный ответ Linkwarden.",
                       outcome_unknown=method == "POST") from error


def options(rows):
    if not isinstance(rows, list):
        raise ApiError("Некорректный список в ответе Linkwarden.")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int
                or not isinstance(row.get("name"), str) or not row["name"]):
            raise ApiError("Некорректный элемент списка Linkwarden.")
        result[row["id"]] = row["name"]
    return result


def get_collections():
    rows = request("GET", "collections").get("response")
    names = options(rows)
    # The list endpoint returns only owned and shared collections. Resolve
    # membership against the token holder only when shared collections exist.
    if any(not isinstance(row.get("members"), list) for row in rows):
        raise ApiError("В ответе Linkwarden отсутствуют права на коллекции.")
    if not any(row["members"] for row in rows):
        return names
    profile = request("GET", "users/me").get("response")
    if not isinstance(profile, dict) or type(profile.get("id")) is not int:
        raise ApiError("Не удалось определить права на коллекции.")
    user_id = profile["id"]
    return {
        row["id"]: names[row["id"]] for row in rows
        if row.get("ownerId") == user_id or any(
            isinstance(member, dict) and member.get("userId") == user_id
            and member.get("canCreate") is True for member in row.get("members", [])
        )
    }


def get_tags():
    result = {}
    params = {}
    cursors = set()
    while True:
        body = request("GET", "tags", params=params)
        page = body.get("data")
        if body.get("success") is not True or not isinstance(page, dict) or "nextCursor" not in page:
            raise ApiError("Некорректная страница тегов Linkwarden.")
        result.update(options(page.get("tags")))
        cursor = page["nextCursor"]
        if cursor is None:
            return result
        if type(cursor) is not int or cursor in cursors:
            raise ApiError("Некорректный указатель следующей страницы тегов.")
        cursors.add(cursor)
        params = {"cursor": cursor}


def save_link(url, collection_id, tags):
    # A fresh request has no retry adapter. A lost POST response can mean
    # that the server already created the link.
    body = request("POST", "links", json={
        "url": url, "collection": {"id": collection_id},
        "tags": [{"id": tag_id, "name": name} for tag_id, name in tags.items()],
    })
    link = body.get("response")
    if not isinstance(link, dict) or type(link.get("id")) is not int:
        raise ApiError("Linkwarden не вернул идентификатор ссылки.", outcome_unknown=True)
    return link["id"]


def find_link(url):
    """Search visible links, checking exact URL matches on every result page."""
    params = {"searchQueryString": url}
    cursors = set()
    while True:
        rows = request("GET", "links", params=params).get("response")
        if not isinstance(rows, list):
            raise ApiError("Не удалось проверить существующие ссылки Linkwarden.")
        if not rows:
            return None
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("id")) is not int
                    or not isinstance(row.get("url"), (str, type(None)))):
                raise ApiError("Некорректный список ссылок Linkwarden.")
            if row.get("url") == url:
                return row["id"]
        cursor = rows[-1]["id"]
        if cursor in cursors:
            raise ApiError("Повтор страницы при проверке существующих ссылок.")
        cursors.add(cursor)
        params = {"searchQueryString": url, "cursor": cursor}
