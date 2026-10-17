from pathlib import Path

import aiosqlite

from bot.sources import SourceFile

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'in_progress',  -- in_progress | completed | cancelled
    waiting_for TEXT,  -- user input the bot expects outside the normal flow: NULL | 'description' | 'source_files' | 'survey_confirm' | 'plan_edit' | 'style_ref'
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

-- Partial unique index: at most one in-progress session per user.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_user
    ON sessions(user_id) WHERE status = 'in_progress';

CREATE TABLE IF NOT EXISTS answers (
    session_id    INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    question_key  TEXT    NOT NULL,
    question_text TEXT    NOT NULL,
    answer        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, question_key)
);

-- Results of pipeline steps: 'tz' now, later 'plan', 'style', ...
CREATE TABLE IF NOT EXISTS artifacts (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    model       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, kind)
);

-- Customer's source files for step 02, in the order they were sent.
CREATE TABLE IF NOT EXISTS source_files (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    name        TEXT    NOT NULL,  -- original file name
    path        TEXT    NOT NULL,
    mime_type   TEXT    NOT NULL,
    text        TEXT,              -- extracted text; NULL = the model reads the file itself
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, position)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Bring databases created by older versions up to SCHEMA (CREATE IF NOT EXISTS won't add columns)."""
        async with self.conn.execute("PRAGMA table_info(sessions)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "waiting_for" not in columns:
            await self.conn.execute("ALTER TABLE sessions ADD COLUMN waiting_for TEXT")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() was not called"
        return self._conn

    async def get_active_session(self, user_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT id FROM sessions WHERE user_id = ? AND status = 'in_progress'",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        return row["id"] if row else None

    async def start_session(self, user_id: int) -> int:
        """Cancel the user's previous unfinished session (if any) and open a new one."""
        await self.conn.execute(
            "UPDATE sessions SET status = 'cancelled', finished_at = datetime('now') "
            "WHERE user_id = ? AND status = 'in_progress'",
            (user_id,),
        )
        cur = await self.conn.execute("INSERT INTO sessions (user_id) VALUES (?)", (user_id,))
        await self.conn.commit()
        return cur.lastrowid

    async def finish_session(self, session_id: int, status: str = "completed") -> None:
        await self.conn.execute(
            "UPDATE sessions SET status = ?, finished_at = datetime('now') WHERE id = ?",
            (status, session_id),
        )
        await self.conn.commit()

    async def get_waiting_for(self, session_id: int) -> str | None:
        async with self.conn.execute("SELECT waiting_for FROM sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
        return row["waiting_for"] if row else None

    async def set_waiting_for(self, session_id: int, value: str | None) -> None:
        await self.conn.execute("UPDATE sessions SET waiting_for = ? WHERE id = ?", (value, session_id))
        await self.conn.commit()

    async def save_answer(self, session_id: int, key: str, question: str, answer: str) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO answers (session_id, question_key, question_text, answer) "
            "VALUES (?, ?, ?, ?)",
            (session_id, key, question, answer),
        )
        await self.conn.commit()

    async def delete_answers(self, session_id: int, keys: list[str]) -> None:
        await self.conn.executemany(
            "DELETE FROM answers WHERE session_id = ? AND question_key = ?", [(session_id, k) for k in keys]
        )
        await self.conn.commit()

    async def get_answers(self, session_id: int) -> dict[str, str]:
        async with self.conn.execute(
            "SELECT question_key, answer FROM answers WHERE session_id = ?", (session_id,)
        ) as cur:
            rows = await cur.fetchall()
        return {r["question_key"]: r["answer"] for r in rows}

    async def save_artifact(self, session_id: int, kind: str, content: str, model: str | None = None) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO artifacts (session_id, kind, content, model) VALUES (?, ?, ?, ?)",
            (session_id, kind, content, model),
        )
        await self.conn.commit()

    async def get_artifacts(self, session_id: int) -> dict[str, str]:
        async with self.conn.execute(
            "SELECT kind, content FROM artifacts WHERE session_id = ?", (session_id,)
        ) as cur:
            rows = await cur.fetchall()
        return {r["kind"]: r["content"] for r in rows}

    async def delete_artifact(self, session_id: int, kind: str) -> None:
        await self.conn.execute("DELETE FROM artifacts WHERE session_id = ? AND kind = ?", (session_id, kind))
        await self.conn.commit()

    async def add_source_file(self, session_id: int, file: SourceFile) -> None:
        await self.conn.execute(
            "INSERT INTO source_files (session_id, position, name, path, mime_type, text) "
            "VALUES (?, (SELECT COUNT(*) + 1 FROM source_files WHERE session_id = ?), ?, ?, ?, ?)",
            (session_id, session_id, file.name, str(file.path), file.mime_type, file.text),
        )
        await self.conn.commit()

    async def get_source_files(self, session_id: int) -> list[SourceFile]:
        async with self.conn.execute(
            "SELECT name, path, mime_type, text FROM source_files WHERE session_id = ? ORDER BY position",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [SourceFile(r["name"], Path(r["path"]), r["mime_type"], r["text"]) for r in rows]

    async def delete_source_files(self, session_id: int) -> None:
        await self.conn.execute("DELETE FROM source_files WHERE session_id = ?", (session_id,))
        await self.conn.commit()

    async def get_artifact(self, session_id: int, kind: str) -> str | None:
        async with self.conn.execute(
            "SELECT content FROM artifacts WHERE session_id = ? AND kind = ?", (session_id, kind)
        ) as cur:
            row = await cur.fetchone()
        return row["content"] if row else None
