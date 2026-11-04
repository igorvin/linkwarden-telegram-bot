from dataclasses import dataclass, field
from itertools import islice
import json
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from linkwarden import public_link_url

PAGE_SIZE = 8
SESSION_TTL = 3600
SEARCH_PAGE_SIZE = 5


def parse_topic_collections(value):
    mapping = json.loads(value or "{}")
    if not isinstance(mapping, dict):
        raise ValueError("Expected an object")
    if any(not key.isdecimal() or int(key) <= 0 or type(collection) is not int
           or collection <= 0 for key, collection in mapping.items()):
        raise ValueError("Topic and collection IDs must be positive integers")
    return {int(key): collection for key, collection in mapping.items()}


@dataclass
class Draft:
    url: str
    collections: dict
    owner_id: int
    chat_id: int
    thread_id: int | None
    source_message_id: int
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    message_id: int | None = None
    bound_collection: int | None = None
    purpose: str = "save"
    can_create: bool = False
    creation_kind: str = ""
    creation_prompt_id: int | None = None
    preservation_link_id: int | None = None
    preservation_text: str = ""
    preservation_lines: tuple = ()
    preservation_pending: bool = False
    search_query: str = ""
    search_pages: tuple = ()
    search_batch: int = 0
    defaults_loaded: bool = False
    settings_summary: str = ""
    urls: tuple = ()
    scan_summary: str = ""
    job_id: str = ""
    scan_running: bool = False
    stage: str = "collections"
    tags: dict = field(default_factory=dict)
    chosen: dict = field(default_factory=dict)
    collection: int | None = None
    page: int = 0
    revision: str = ""
    created: float = field(default_factory=time.monotonic)
    actions: dict = field(default_factory=dict)
    error: str = ""
    busy: bool = False

    def view(self):
        self.revision = secrets.token_hex(4)
        self.actions = {}
        rows = []

        def button(label, action, value=None):
            token = secrets.token_urlsafe(4)
            self.actions[token] = (action, value)
            return InlineKeyboardButton(
                label, callback_data=f"lw:{self.session_id}:{self.revision}:{token}"
            )

        if self.creation_kind:
            text = "Ответьте на отдельное сообщение бота названием новой коллекции." if self.creation_kind == "collection" else "Ответьте на отдельное сообщение бота названием нового тега."
            if self.error:
                text += "\n" + self.error
            return {"text": text, "reply_markup": InlineKeyboardMarkup([
                [button("Вернуться к выбору", "creation_back"), button("Отмена", "cancel")]
            ])}

        if self.purpose == "preservation":
            text = self.preservation_text + "\n\nАрхивы Linkwarden\n"
            text += self.error if self.error else "\n".join(self.preservation_lines)
            url = public_link_url(self.preservation_link_id)
            if url:
                rows.append([InlineKeyboardButton("Открыть в Linkwarden", url=url)])
            rows.append([button("Обновить статус", "preservation_refresh")])
            return {"text": text, "reply_markup": InlineKeyboardMarkup(rows),
                    "disable_web_page_preview": True}

        if self.purpose == "search":
            text = f"Поиск: {self.search_query}\n"
            text += f"Коллекция темы: #{self.collection}" if self.collection is not None else "Все доступные коллекции"
            if self.error:
                text += "\n" + self.error
            if self.search_pages:
                batch = self.search_pages[self.search_batch]
                start = self.page * SEARCH_PAGE_SIZE
                visible = batch["items"][start:start + SEARCH_PAGE_SIZE]
                if not visible:
                    text += "\nНа этой странице совпадений нет."
                preceding = sum(len(page["items"]) for page in self.search_pages[:self.search_batch])
                for number, item in enumerate(visible, preceding + start + 1):
                    title = item["name"].replace("\n", " ").replace("\r", " ")[:100]
                    collection = item["collection"].replace("\n", " ").replace("\r", " ")[:60]
                    text += f"\n\n{number}. {title}\nКоллекция: {collection}"
                    if item["tags"]:
                        tags = ", ".join(name.replace("\n", " ")[:35] for name in item["tags"][:3])
                        text += "\nТеги: " + tags
                        if len(item["tags"]) > 3:
                            text += f" и ещё {len(item['tags']) - 3}"
                    url = public_link_url(item["id"])
                    if url:
                        rows.append([InlineKeyboardButton(f"{number}. {title[:45]}", url=url)])
                nav = []
                if self.page or self.search_batch:
                    nav.append(button("←", "search_prev"))
                if start + SEARCH_PAGE_SIZE < len(batch["items"]) or batch["next_cursor"] is not None:
                    nav.append(button("→", "search_next"))
                if nav:
                    rows.append(nav)
            rows.append([button("Обновить поиск", "search_refresh"), button("Закрыть", "cancel")])
            return {"text": text, "reply_markup": InlineKeyboardMarkup(rows),
                    "disable_web_page_preview": True}

        if self.purpose == "settings":
            rows = [[button("Выбрать коллекцию", "settings_bind")]]
            if self.bound_collection is not None:
                rows.append([button("Удалить привязку", "settings_unbind")])
            rows.extend([
                [button("Теги по умолчанию", "settings_tags")],
                [button("Обновить", "settings_refresh"), button("Закрыть", "cancel")],
            ])
            return {"text": self.settings_summary, "reply_markup": InlineKeyboardMarkup(rows)}

        if self.purpose == "scan_upload":
            text = ("Пришлите JSON-экспорт Telegram Desktop в эту тему (до 10 МБ).\n"
                    "Будут выбраны только URL текущей темы. Сначала появится предварительный просмотр.")
            return {"text": text, "reply_markup": InlineKeyboardMarkup([[button("Отмена", "cancel")]])}

        if self.purpose == "scan_control":
            if not self.scan_running:
                rows.append([button("Продолжить импорт", "scan_resume")])
            rows.append([button("Отменить импорт", "scan_cancel"), button("Закрыть", "cancel")])
            return {"text": self.scan_summary, "reply_markup": InlineKeyboardMarkup(rows)}

        if self.purpose == "unbind":
            text = (f"Удалить привязку этого чата/темы к коллекции #{self.bound_collection}?\n"
                    "Для новых URL бот снова предложит выбрать коллекцию.")
            rows = [[button("Удалить привязку", "unbind_save"), button("Отмена", "cancel")]]
            return {"text": text, "reply_markup": InlineKeyboardMarkup(rows)}

        if self.purpose == "bind" and self.stage == "confirm":
            name = self.collections[self.collection][:200]
            text = f"Привязать этот чат/тему к коллекции «{name}» (#{self.collection})?"
            rows = [
                [button("Подтвердить привязку", "bind_save")],
                [button("Назад", "back"), button("Отмена", "cancel")],
            ]
            return {"text": text, "reply_markup": InlineKeyboardMarkup(rows)}

        text = ("Привязка коллекции для текущего чата/темы\n\n"
                if self.purpose == "bind" else self.url + "\n\n")
        if self.purpose == "scan":
            text = self.scan_summary + "\n\n"
        if self.error:
            text += self.error
            rows.append([button("Повторить загрузку", "retry")])
        else:
            if self.stage == "collections":
                items = self.collections
                text += "Выберите коллекцию"
            elif self.stage == "tags":
                items = self.tags
                if self.purpose == "default_tags":
                    text += "Теги по умолчанию для текущего чата/темы\n"
                else:
                    text += f"Коллекция: {self.collections[self.collection][:200]} (#{self.collection})\n"
                text += ("Отметьте теги и нажмите «Готово — сохранить»"
                         if self.purpose == "save" else "Отметьте теги и нажмите «Готово»")
                if not items:
                    text += "\nДоступных тегов нет. Можно продолжить без тегов."
            else:
                items = self.chosen
                if self.purpose == "default_tags":
                    text += "Сохранить теги по умолчанию для текущего чата/темы?\n"
                else:
                    text += f"Коллекция: {self.collections[self.collection][:200]} (#{self.collection})\n"
                text += "Теги:" if items else "Без тегов"
            start = self.page * PAGE_SIZE
            for item_id, name in islice(items.items(), start, start + PAGE_SIZE):
                if self.stage == "confirm":
                    text += "\n• " + name[:75]
                else:
                    mark = "✓ " if self.stage == "tags" and item_id in self.chosen else ""
                    rows.append([button(mark + name[:60], "pick", item_id)])
            pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
            if pages > 1:
                text += f"\nСтраница {self.page + 1} из {pages}"
                nav = []
                if self.page:
                    nav.append(button("←", "prev"))
                if self.page + 1 < pages:
                    nav.append(button("→", "next"))
                rows.append(nav)
            if self.stage == "tags":
                rows.append([button("Готово — сохранить" if self.purpose == "save" else "Готово", "done")])
            elif self.stage == "confirm" and self.purpose == "default_tags":
                rows.append([button("Сохранить настройки", "tags_save")])
            elif self.stage == "confirm":
                rows.append([button("Начать импорт" if self.purpose == "scan" else "Сохранить", "save")])
        if self.can_create and self.purpose in ("save", "scan", "bind", "default_tags"):
            if self.stage == "collections":
                rows.append([button("Новая коллекция", "new_collection")])
            elif self.stage == "tags":
                rows.append([button("Новый тег", "new_tag")])
        if self.stage == "confirm" or (self.stage == "tags" and self.bound_collection is None):
            rows.append([button("Назад", "back")])
        rows.append([button("Отмена", "cancel")])
        return {"text": text, "reply_markup": InlineKeyboardMarkup(rows),
                "disable_web_page_preview": True}
