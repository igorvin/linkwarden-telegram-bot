from dataclasses import dataclass, field
from itertools import islice
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 8


@dataclass
class Draft:
    url: str
    collections: dict
    stage: str = "collections"
    tags: dict = field(default_factory=dict)
    chosen: dict = field(default_factory=dict)
    collection: int | None = None
    page: int = 0
    revision: str = ""
    created: float = field(default_factory=time.monotonic)
    actions: set = field(default_factory=set)
    error: str = ""

    def view(self):
        self.revision = secrets.token_hex(6)
        self.actions.clear()
        rows = []

        def button(label, action):
            self.actions.add(action)
            return InlineKeyboardButton(label, callback_data=f"lw:{self.revision}:{action}")

        text = self.url + "\n\n"
        if self.error:
            text += self.error
            rows.append([button("Обновить", "retry")])
        else:
            if self.stage == "collections":
                items = self.collections
                text += "Выберите коллекцию"
            elif self.stage == "tags":
                items = self.tags
                text += "Отметьте теги и нажмите «Готово»"
            else:
                items = self.chosen
                text += f"Коллекция: {self.collections[self.collection][:200]} (#{self.collection})\n"
                text += "Теги:" if items else "Без тегов"
            start = self.page * PAGE_SIZE
            for item_id, name in islice(items.items(), start, start + PAGE_SIZE):
                if self.stage == "confirm":
                    text += "\n• " + name[:75]
                else:
                    mark = "✓ " if self.stage == "tags" and item_id in self.chosen else ""
                    rows.append([button(mark + name[:60], f"pick:{item_id}")])
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
                rows.append([button("Готово", "done")])
            elif self.stage == "confirm":
                rows.append([button("Сохранить", "save")])
        rows.append([button("Отмена", "cancel")])
        return {"text": text, "reply_markup": InlineKeyboardMarkup(rows),
                "disable_web_page_preview": True}
