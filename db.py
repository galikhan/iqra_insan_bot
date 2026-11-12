import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional, Tuple

DEFAULT_NOTIFY_TIMES = "10:00,18:00"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,          -- telegram user id
    chat_id     INTEGER NOT NULL,
    username    TEXT,
    full_name   TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code        TEXT PRIMARY KEY,             -- lowercase letters
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    code        TEXT NOT NULL,
    value       INTEGER NOT NULL,
    created_at  TEXT NOT NULL                 -- local ISO datetime
);
CREATE INDEX IF NOT EXISTS idx_entries_user_time ON entries(user_id, created_at);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES ('notify_times', ?)",
                (DEFAULT_NOTIFY_TIMES,),
            )

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # --- users ---
    def upsert_user(self, user_id: int, chat_id: int, username: Optional[str], full_name: str):
        with self.conn() as c:
            c.execute(
                """INSERT INTO users(id, chat_id, username, full_name, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET chat_id=excluded.chat_id,
                       username=excluded.username, full_name=excluded.full_name""",
                (user_id, chat_id, username, full_name, datetime.now().isoformat()),
            )

    def all_users(self) -> List[Tuple[int, int, str]]:
        with self.conn() as c:
            return c.execute("SELECT id, chat_id, full_name FROM users").fetchall()

    # --- codes ---
    def list_codes(self) -> List[Tuple[str, str]]:
        with self.conn() as c:
            return c.execute("SELECT code, description FROM codes ORDER BY code").fetchall()

    def code_exists(self, code: str) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM codes WHERE code=?", (code,)).fetchone() is not None

    def add_code(self, code: str, description: str):
        with self.conn() as c:
            c.execute(
                "INSERT INTO codes(code, description) VALUES (?, ?) "
                "ON CONFLICT(code) DO UPDATE SET description=excluded.description",
                (code, description),
            )

    def delete_code(self, code: str) -> bool:
        with self.conn() as c:
            return c.execute("DELETE FROM codes WHERE code=?", (code,)).rowcount > 0

    # --- todos ---
    def list_todos(self) -> List[Tuple[int, str]]:
        with self.conn() as c:
            return c.execute("SELECT id, text FROM todos ORDER BY id").fetchall()

    def add_todo(self, text: str):
        with self.conn() as c:
            c.execute("INSERT INTO todos(text) VALUES (?)", (text,))

    def delete_todo_by_position(self, pos: int) -> Optional[str]:
        todos = self.list_todos()
        if pos < 1 or pos > len(todos):
            return None
        todo_id, text = todos[pos - 1]
        with self.conn() as c:
            c.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        return text

    def clear_todos(self):
        with self.conn() as c:
            c.execute("DELETE FROM todos")

    # --- entries ---
    def add_entries(self, user_id: int, items: List[Tuple[str, int]], when: datetime):
        with self.conn() as c:
            c.executemany(
                "INSERT INTO entries(user_id, code, value, created_at) VALUES (?, ?, ?, ?)",
                [(user_id, code, value, when.isoformat()) for code, value in items],
            )

    def undo_last(self, user_id: int) -> Optional[Tuple[str, int]]:
        with self.conn() as c:
            row = c.execute(
                "SELECT id, code, value FROM entries WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM entries WHERE id=?", (row[0],))
            return row[1], row[2]

    def sums(self, since: datetime, user_id: Optional[int] = None) -> List[Tuple]:
        """Per-code sums since `since`. If user_id is None, grouped by user too."""
        with self.conn() as c:
            if user_id is not None:
                return c.execute(
                    """SELECT code, SUM(value) FROM entries
                       WHERE user_id=? AND created_at>=?
                       GROUP BY code ORDER BY code""",
                    (user_id, since.isoformat()),
                ).fetchall()
            return c.execute(
                """SELECT u.full_name, e.code, SUM(e.value) FROM entries e
                   JOIN users u ON u.id = e.user_id
                   WHERE e.created_at>=?
                   GROUP BY e.user_id, e.code ORDER BY u.full_name, e.code""",
                (since.isoformat(),),
            ).fetchall()

    # --- settings ---
    def get_notify_times(self) -> List[str]:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key='notify_times'").fetchone()
        return [t for t in (row[0] if row else DEFAULT_NOTIFY_TIMES).split(",") if t]

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: Optional[str]):
        with self.conn() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                c.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def set_notify_times(self, times: List[str]):
        with self.conn() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES ('notify_times', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (",".join(times),),
            )
