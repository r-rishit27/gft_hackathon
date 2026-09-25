"""Private question history and saved FAQs; never stores query results or SQL."""
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .contracts import Question


class SavedQuestion(Question):
    saved: bool = True


class Workspace:
    def __init__(self, path):
        if path != ":memory:":
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, owner TEXT, question TEXT, outcome TEXT, created TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS saved (owner TEXT, question TEXT, created TEXT, PRIMARY KEY(owner, question))")

    def record(self, owner, question, outcome):
        with self.lock, self.db:
            self.db.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?)", (str(uuid.uuid4()), owner, question, outcome, datetime.now(timezone.utc).isoformat()))
            self.db.execute("DELETE FROM history WHERE owner=? AND id NOT IN (SELECT id FROM history WHERE owner=? ORDER BY created DESC LIMIT 100)", (owner, owner))

    def get(self, owner):
        with self.lock:
            history = self.db.execute("SELECT question,outcome,created FROM history WHERE owner=? ORDER BY created DESC LIMIT 100", (owner,)).fetchall()
            frequent = self.db.execute("SELECT question,COUNT(*) AS count FROM history WHERE owner=? AND outcome='completed' GROUP BY question ORDER BY count DESC,MAX(created) DESC LIMIT 8", (owner,)).fetchall()
            saved = self.db.execute("SELECT question FROM saved WHERE owner=? ORDER BY created DESC", (owner,)).fetchall()
        return {"history": [dict(zip(("question", "outcome", "created"), row)) for row in history],
                "frequent": [{"question": q, "count": n} for q, n in frequent],
                "saved": [q for (q,) in saved]}

    def save(self, owner, question, saved):
        with self.lock, self.db:
            if saved:
                self.db.execute("INSERT OR IGNORE INTO saved VALUES (?, ?, ?)", (owner, question, datetime.now(timezone.utc).isoformat()))
                self.db.execute("DELETE FROM saved WHERE owner=? AND question NOT IN (SELECT question FROM saved WHERE owner=? ORDER BY created DESC LIMIT 50)", (owner, owner))
            else:
                self.db.execute("DELETE FROM saved WHERE owner=? AND question=?", (owner, question))

    def clear(self, owner):
        with self.lock, self.db:
            self.db.execute("DELETE FROM history WHERE owner=?", (owner,))

    def close(self):
        self.db.close()
