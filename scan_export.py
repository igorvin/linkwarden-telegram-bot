"""Parse Telegram Desktop exports without fetching any links."""
from dataclasses import dataclass
import json

MAX_EXPORT_BYTES = 10 * 1024 * 1024
MAX_MESSAGES = 100000
MAX_URLS = 10000


class ExportError(ValueError):
    pass


@dataclass
class ScanResult:
    urls: tuple
    matched_messages: int
    occurrences: int
    unknown_messages: int
    other_messages: int


def parse_export(content, chat_id, thread_id, extract_links, bot_id=None):
    if len(content) > MAX_EXPORT_BYTES:
        raise ExportError("Экспорт больше 10 МБ. Разделите его по диапазону дат.")
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ExportError("Не удалось прочитать JSON-экспорт Telegram.") from error
    expected_id = -chat_id - 1000000000000
    if (not isinstance(data, dict) or type(data.get("id")) is not int
            or data["id"] != expected_id):
        raise ExportError("Экспорт принадлежит другой группе или имеет неподдерживаемую структуру.")
    rows = data.get("messages")
    if not isinstance(rows, list) or len(rows) > MAX_MESSAGES:
        raise ExportError("Некорректный или слишком большой список сообщений.")
    by_id = {}
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in by_id:
            raise ExportError("Некорректные или повторяющиеся ID сообщений.")
        by_id[row["id"]] = row
    roots = {row["id"] for row in rows if row.get("action") == "topic_created"}
    roots.add(thread_id)
    resolved = {}

    def topic(message_id):
        trail = []
        visited = set()
        current = message_id
        while current not in resolved:
            if current in roots:
                resolved[current] = current
                break
            if current in visited or current not in by_id:
                resolved[current] = None
                break
            visited.add(current)
            trail.append(current)
            row = by_id[current]
            explicit = row.get("message_thread_id")
            if type(explicit) is int and explicit > 0:
                resolved[current] = explicit
                break
            reply = row.get("reply_to_message_id")
            if type(reply) is not int or reply <= 0:
                resolved[current] = None
                break
            current = reply
        result = resolved[current]
        for item in trail:
            resolved[item] = result
        return result

    urls = {}
    matched = occurrences = unknown = other = 0
    for row in rows:
        if row.get("type") != "message" or row.get("from_id") == f"user{bot_id}":
            continue
        texts = []
        text = row.get("text", "")
        entities = row.get("text_entities", [])
        if not isinstance(text, (str, list)) or not isinstance(entities, list):
            raise ExportError("Некорректная структура текста сообщения.")
        for item in text if isinstance(text, list) else [text]:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                texts.extend(value for key in ("text", "href") if isinstance(value := item.get(key), str))
        for entity in entities:
            if isinstance(entity, dict):
                texts.extend(value for key in ("text", "href") if isinstance(value := entity.get(key), str))
        found = dict.fromkeys(url for part in texts for url in extract_links(part) if len(url) <= 2048)
        if not found:
            continue
        row_topic = topic(row["id"])
        if row_topic is None:
            unknown += 1
        elif row_topic != thread_id:
            other += 1
        else:
            matched += 1
            occurrences += len(found)
            urls.update(found)
    if not urls:
        raise ExportError("В экспорте нет URL, которые удалось надёжно отнести к текущей теме.")
    if len(urls) > MAX_URLS:
        raise ExportError("Больше 10000 уникальных URL. Разделите экспорт по диапазону дат.")
    return ScanResult(tuple(urls), matched, occurrences, unknown, other)
