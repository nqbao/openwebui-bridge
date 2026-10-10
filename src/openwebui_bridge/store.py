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
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(sessions)")}
        for name, definition in (
            ("user_message_id", "TEXT"),
            ("assistant_message_id", "TEXT"),
            ("parent_message_id", "TEXT"),
            ("parent_known", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
        self.db.execute("""CREATE TABLE IF NOT EXISTS branch_sessions (
            chat_id TEXT NOT NULL, assistant_message_id TEXT NOT NULL,
            session_id TEXT NOT NULL, cwd TEXT NOT NULL, prefix_hash TEXT NOT NULL,
            updated_at REAL NOT NULL, user_message_id TEXT, parent_message_id TEXT,
            parent_known INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, assistant_message_id)
        )""")
        self.db.commit()

    def get(self, key: str):
        return self.db.execute("SELECT * FROM sessions WHERE chat_id = ?", (key,)).fetchone()

    def delete(self, key: str):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE chat_id = ?", (key,))

    def get_branch(self, key: str, assistant_message_id: str):
        return self.db.execute(
            "SELECT * FROM branch_sessions WHERE chat_id = ? AND assistant_message_id = ?",
            (key, assistant_message_id),
        ).fetchone()

    def invalidate_session(self, key: str, session_id: str):
        # Native resume mutates a session. Old message IDs must never point to
        # its former state, including if the next turn fails or is cancelled.
        with self.db:
            for table in ("sessions", "branch_sessions"):
                self.db.execute(
                    f"DELETE FROM {table} WHERE chat_id = ? AND session_id = ?", (key, session_id)
                )

    def save(
        self,
        key: str,
        session_id: str,
        cwd: str,
        prefix_hash: str,
        user_message_id: str | None = None,
        assistant_message_id: str | None = None,
        parent_message_id: str | None = None,
        parent_known: bool = False,
    ):
        updated = time.time()
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO sessions
                (chat_id, session_id, cwd, prefix_hash, updated_at, user_message_id,
                 assistant_message_id, parent_message_id, parent_known)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    session_id,
                    cwd,
                    prefix_hash,
                    updated,
                    user_message_id,
                    assistant_message_id,
                    parent_message_id,
                    parent_known,
                ),
            )
            if assistant_message_id:
                self.db.execute(
                    """INSERT OR REPLACE INTO branch_sessions
                    (chat_id, assistant_message_id, session_id, cwd, prefix_hash, updated_at,
                     user_message_id, parent_message_id, parent_known)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        assistant_message_id,
                        session_id,
                        cwd,
                        prefix_hash,
                        updated,
                        user_message_id,
                        parent_message_id,
                        parent_known,
                    ),
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
