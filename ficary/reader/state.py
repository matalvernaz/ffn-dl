"""Per-story reading position + bookmarks in a small SQLite DB.

Lives beside the JSON library index and the full-text search DB under the
portable root. The connection/PRAGMA/schema-version idiom is cloned from
:class:`ficary.library.fulltext.FullTextIndex`. The connection is opened
with ``check_same_thread=False`` but is NOT internally synchronized —
callers use it from the wx main thread (position autosave + bookmark ops),
so a single connection is fine.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import portable

SCHEMA_VERSION = "1"


def default_db_path() -> Path:
    return portable.portable_root() / "reader-state.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Bookmark:
    id: int
    story_key: str
    name: str
    chapter_number: int
    char_offset: int
    excerpt: str
    created_at: str


class ReaderStateDB:
    """Reading position (one row per story) and named bookmarks (many)."""

    def __init__(self, db_path: Optional[Path] = None):
        self._path = Path(db_path) if db_path is not None else default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._connect()
        except sqlite3.DatabaseError:
            # Corrupt DB file. Positions/bookmarks are rebuildable-loss
            # data — quarantine and start fresh rather than making the
            # reader permanently unopenable (house convention for corrupt
            # cache/state files).
            self._quarantine_corrupt()
            self._conn = self._connect()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def _quarantine_corrupt(self) -> None:
        quarantined = self._path.with_name(self._path.name + ".corrupt")
        try:
            quarantined.unlink(missing_ok=True)
            self._path.replace(quarantined)
        except OSError:
            self._path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            sidecar = self._path.with_name(self._path.name + suffix)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass

    def _init_schema(self) -> None:
        c = self._conn
        c.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS reading_position ("
            "story_key TEXT PRIMARY KEY, site TEXT, story_id TEXT, title TEXT, "
            "chapter_number INTEGER NOT NULL, char_offset INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS bookmark ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, story_key TEXT NOT NULL, "
            "name TEXT NOT NULL, chapter_number INTEGER NOT NULL, "
            "char_offset INTEGER NOT NULL DEFAULT 0, excerpt TEXT, "
            "created_at TEXT NOT NULL)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_bookmark_story ON bookmark(story_key)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS story_soundscape ("
            "story_key TEXT PRIMARY KEY, slug TEXT NOT NULL)"
        )
        c.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    # ── reading position ──────────────────────────────────────────
    def save_position(self, story_key: str, chapter_number: int,
                      char_offset: int = 0, *, title: Optional[str] = None,
                      site: Optional[str] = None,
                      story_id: Optional[str] = None) -> None:
        self._conn.execute(
            "INSERT INTO reading_position "
            "(story_key, site, story_id, title, chapter_number, char_offset, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(story_key) DO UPDATE SET "
            "  chapter_number = excluded.chapter_number, "
            "  char_offset = excluded.char_offset, "
            "  title = COALESCE(excluded.title, reading_position.title), "
            "  updated_at = excluded.updated_at",
            (story_key, site, story_id, title, chapter_number, char_offset, _now_iso()),
        )
        self._conn.commit()

    def load_position(self, story_key: str) -> Optional[tuple[int, int]]:
        row = self._conn.execute(
            "SELECT chapter_number, char_offset FROM reading_position WHERE story_key = ?",
            (story_key,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    # ── bookmarks ─────────────────────────────────────────────────
    def add_bookmark(self, story_key: str, name: str, chapter_number: int,
                     char_offset: int = 0, excerpt: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO bookmark "
            "(story_key, name, chapter_number, char_offset, excerpt, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (story_key, name, chapter_number, char_offset, excerpt, _now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_bookmarks(self, story_key: str) -> list[Bookmark]:
        rows = self._conn.execute(
            "SELECT id, story_key, name, chapter_number, char_offset, excerpt, created_at "
            "FROM bookmark WHERE story_key = ? ORDER BY chapter_number, char_offset",
            (story_key,),
        ).fetchall()
        return [Bookmark(*r) for r in rows]

    def delete_bookmark(self, bookmark_id: int) -> None:
        self._conn.execute("DELETE FROM bookmark WHERE id = ?", (bookmark_id,))
        self._conn.commit()

    # ── soundscape assignment ─────────────────────────────────────
    def set_soundscape(self, story_key: str, slug: Optional[str]) -> None:
        if slug:
            self._conn.execute(
                "INSERT INTO story_soundscape (story_key, slug) VALUES (?, ?) "
                "ON CONFLICT(story_key) DO UPDATE SET slug = excluded.slug",
                (story_key, slug))
        else:
            self._conn.execute("DELETE FROM story_soundscape WHERE story_key = ?", (story_key,))
        self._conn.commit()

    def get_soundscape(self, story_key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT slug FROM story_soundscape WHERE story_key = ?", (story_key,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReaderStateDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
