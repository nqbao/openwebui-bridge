import asyncio
import fcntl
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lease = path.with_suffix(path.suffix + ".lock").open("a")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close()
            raise RuntimeError(
                "This database already has a proxy process; use one worker"
            ) from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            chat_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cwd TEXT NOT NULL,
            prefix_hash TEXT NOT NULL, updated_at REAL NOT NULL
        )""")
        self.db.commit()

    def get(self, key: str):
        return self.db.execute("SELECT * FROM sessions WHERE chat_id = ?", (key,)).fetchone()

    def delete(self, key: str):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE chat_id = ?", (key,))

    def save(self, key: str, session_id: str, cwd: str, prefix_hash: str):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)",
                (key, session_id, cwd, prefix_hash, time.time()),
            )

    def close(self):
        self.db.close()
        self.lease.close()


class ChatLocks:
    def __init__(self):
        self.entries: dict[str, list] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        entry = self.entries.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                del self.entries[key]
