"""Durable claims prevent duplicate POSTs, including after an unknown outcome."""
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import sqlite3

from linkwarden import ApiError, find_link, save_link, get_collections, get_tags, create_collection, create_tag, metadata_name


class SubmissionLedger:
    def __init__(self):
        path = Path(os.environ.get("BOT_STATE_PATH", "data/submissions.sqlite3"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS submissions (
                fingerprint TEXT PRIMARY KEY, state TEXT NOT NULL, link_id INTEGER
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS topic_bindings (
                chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                collection_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            )""")

            db.execute("""CREATE TABLE IF NOT EXISTS topic_preferences (
                chat_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                default_tags TEXT NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            )""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def key(self, url):
        account = os.environ["LINKWARDEN_API_URL"].rstrip("/") + "\n" + os.environ["LINKWARDEN_API_KEY"]
        return hashlib.sha256((account + "\n" + url).encode()).hexdigest()

    def claim(self, key):
        with self.connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO submissions VALUES (?, 'pending', NULL)", (key,)
            ).rowcount
            state, link_id = db.execute(
                "SELECT state, link_id FROM submissions WHERE fingerprint=?", (key,)
            ).fetchone()
        return bool(inserted), state, link_id

    def finish(self, key, state, link_id=None):
        with self.connect() as db:
            db.execute("UPDATE submissions SET state=?, link_id=? WHERE fingerprint=?",
                       (state, link_id, key))

    def renew_saved(self, key, previous_id):
        with self.connect() as db:
            return bool(db.execute(
                """UPDATE submissions SET state='pending', link_id=NULL
                WHERE fingerprint=? AND state='saved' AND link_id=?""",
                (key, previous_id),
            ).rowcount)

    def release(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM submissions WHERE fingerprint=?", (key,))


def save_once(url, collection_id, tags):
    ledger = SubmissionLedger()
    key = ledger.key(url)
    claimed, state, link_id = ledger.claim(key)
    if not claimed and state == "saved":
        return link_id, False
    try:
        existing = find_link(url)
    except ApiError:
        if claimed:
            ledger.release(key)  # No POST was attempted.
        raise
    if existing is not None:
        ledger.finish(key, "saved", existing)
        return existing, False
    if not claimed:
        raise ApiError("Результат предыдущей отправки неизвестен. Проверьте Linkwarden перед новой попыткой.")
    try:
        link_id = save_link(url, collection_id, tags)
    except ApiError as error:
        if error.outcome_unknown:
            ledger.finish(key, "uncertain")
        else:
            ledger.release(key)
        raise
    ledger.finish(key, "saved", link_id)
    return link_id, True


def get_binding(chat_id, thread_id):
    """None means no override, zero explicitly disables a configured binding."""
    ledger = SubmissionLedger()
    with ledger.connect() as db:
        row = db.execute(
            "SELECT collection_id FROM topic_bindings WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id or 0),
        ).fetchone()
    return row[0] if row else None


def set_binding(chat_id, thread_id, collection_id):
    ledger = SubmissionLedger()
    with ledger.connect() as db:
        db.execute(
            """INSERT INTO topic_bindings VALUES (?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET collection_id=excluded.collection_id""",
            (chat_id, thread_id or 0, collection_id),
        )


def get_default_tags(chat_id, thread_id):
    ledger = SubmissionLedger()
    with ledger.connect() as db:
        row = db.execute(
            "SELECT default_tags FROM topic_preferences WHERE chat_id=? AND thread_id=?",
            (chat_id, thread_id or 0),
        ).fetchone()
    return tuple(json.loads(row[0])) if row else ()


def set_default_tags(chat_id, thread_id, tag_ids):
    ids = tuple(dict.fromkeys(tag_ids))
    if any(type(tag_id) is not int or tag_id <= 0 for tag_id in ids):
        raise ValueError("Invalid tag IDs")
    ledger = SubmissionLedger()
    with ledger.connect() as db:
        db.execute(
            """INSERT INTO topic_preferences VALUES (?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET default_tags=excluded.default_tags""",
            (chat_id, thread_id or 0, json.dumps(ids)),
        )


def create_named_item(kind, value):
    name = metadata_name(value, kind)
    ledger = SubmissionLedger()
    key = ledger.key(f"metadata:{kind}\0{name}")
    claimed, state, previous_id = ledger.claim(key)
    try:
        choices = get_collections() if kind == "collection" else get_tags()
    except ApiError:
        if claimed:
            ledger.release(key)
        raise
    matches = [item_id for item_id, existing in choices.items() if existing == name]
    if len(matches) > 1:
        if claimed:
            ledger.release(key)
        raise ApiError("Найдено несколько объектов с таким названием. Вернитесь к выбору существующего.")
    if matches:
        ledger.finish(key, "saved", matches[0])
        return matches[0], name
    if not claimed and state == "saved":
        claimed = ledger.renew_saved(key, previous_id)
    if not claimed:
        raise ApiError("Результат предыдущего создания неизвестен. Проверьте Linkwarden и повторите то же название.")
    try:
        item_id = create_collection(name) if kind == "collection" else create_tag(name)
    except ApiError as error:
        if error.outcome_unknown:
            ledger.finish(key, "uncertain")
        else:
            ledger.release(key)
        raise
    ledger.finish(key, "saved", item_id)
    return item_id, name
