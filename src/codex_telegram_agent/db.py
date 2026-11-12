"""Private durable queue: committed running tasks never execute twice."""

import sqlite3
import time
import uuid
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, prompt TEXT,
                status TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)

    def recover(self):
        with self.db:
            count = self.db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[
                0
            ]
            self.db.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")
            if count:
                self.emit(
                    "⚠️ Попереднє завдання було перервано перезапуском. "
                    "Воно не повторюється автоматично. Перевірте результат перед новою командою."
                )

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def submit(self, request_id: str, prompt: str):
        row = self.db.execute("SELECT id FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if row:
            return row[0]
        count = self.db.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if count >= 5:
            raise ValueError("queue_full")
        job = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,'queued',?)", (job, request_id, prompt, time.time())
            )
        return job

    def next(self):
        row = self.db.execute(
            "SELECT id,prompt,created FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        job, prompt, created = row
        if time.time() - created > 900:
            self.finish(job, "expired")
            self.emit(
                "⚠️ Завдання надто довго чекало в черзі й не запускалося. "
                "Надішліть його ще раз, якщо воно актуальне."
            )
            return None
        with self.db:
            self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        return job, prompt

    def finish(self, job, status):
        with self.db:
            self.db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job))

    def cancel_queued(self):
        with self.db:
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE status='queued'")

    def emit(self, text):
        # Telegram is limited to 4096 characters. Split at Unicode codepoint boundaries.
        with self.db:
            for start in range(0, min(len(text), 45_000), 3000):
                self.db.execute(
                    "INSERT INTO messages(text) VALUES (?)", (text[start : start + 3000],)
                )

    def poll(self):
        return [
            {"id": row[0], "text": row[1]}
            for row in self.db.execute("SELECT id,text FROM messages ORDER BY id LIMIT 10")
        ]

    def ack(self, ids):
        with self.db:
            self.db.executemany("DELETE FROM messages WHERE id=?", ((value,) for value in ids))
