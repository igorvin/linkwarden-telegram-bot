"""Durable batch-import progress. Only confirmed jobs can reach the worker."""
import json
import secrets
import sqlite3

from submissions import SubmissionLedger


class ScanStore:
    def __init__(self):
        self.ledger = SubmissionLedger()
        with self.ledger.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS scan_jobs (
                id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, collection_id INTEGER NOT NULL,
                tags TEXT NOT NULL, urls TEXT NOT NULL, state TEXT NOT NULL,
                cursor INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                message_id INTEGER
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_scan
                ON scan_jobs(chat_id, thread_id)
                WHERE state IN ('queued', 'running', 'paused')""")

    def get(self, job_id):
        with self.ledger.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
        return self.decode(row)

    def active(self, chat_id, thread_id):
        with self.ledger.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT * FROM scan_jobs WHERE chat_id=? AND thread_id=?
                AND state IN ('queued', 'running', 'paused')""", (chat_id, thread_id or 0)).fetchone()
        return self.decode(row)

    @staticmethod
    def decode(row):
        if row is None:
            return None
        job = dict(row)
        job["urls"] = json.loads(job["urls"])
        job["tags"] = {int(key): value for key, value in json.loads(job["tags"]).items()}
        return job

    def create(self, draft):
        job_id = secrets.token_urlsafe(9)
        with self.ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            existing = db.execute("""SELECT * FROM scan_jobs WHERE chat_id=? AND thread_id=?
                AND state IN ('queued', 'running', 'paused')""", (draft.chat_id, draft.thread_id or 0)).fetchone()
            if existing is not None:
                return self.decode(existing)
            db.execute("""INSERT INTO scan_jobs
                (id, chat_id, thread_id, owner_id, collection_id, tags, urls, state, message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (job_id, draft.chat_id, draft.thread_id or 0, draft.owner_id, draft.collection,
                 json.dumps(draft.chosen), json.dumps(draft.urls), draft.message_id))
        return self.get(job_id)

    def start(self, job_id, message_id):
        with self.ledger.connect() as db:
            db.execute("""UPDATE scan_jobs SET state='running', error='', message_id=?
                WHERE id=? AND state IN ('queued', 'running', 'paused')""", (message_id, job_id))

    def advance(self, job_id, cursor, created):
        with self.ledger.connect() as db:
            db.execute("""UPDATE scan_jobs SET cursor=?, created=created+?,
                duplicates=duplicates+? WHERE id=? AND cursor=?""",
                (cursor + 1, int(created), int(not created), job_id, cursor))

    def state(self, job_id, state, error=""):
        with self.ledger.connect() as db:
            db.execute("""UPDATE scan_jobs SET state=?, error=? WHERE id=?
                AND state IN ('queued', 'running', 'paused')""", (state, error, job_id))


def progress_text(job):
    labels = {"queued": "Ожидает запуска", "running": "Импорт выполняется", "paused": "Импорт приостановлен",
              "done": "Импорт завершён", "cancelled": "Импорт отменён"}
    text = (f"{labels[job['state']]}\nОбработано: {job['cursor']} из {len(job['urls'])}\n"
            f"Добавлено: {job['created']}\nУже существовали: {job['duplicates']}")
    if job["error"]:
        text += "\n" + job["error"]
    if job["state"] in ("running", "paused"):
        text += "\nКоманда /scan покажет управление импортом."
    return text
