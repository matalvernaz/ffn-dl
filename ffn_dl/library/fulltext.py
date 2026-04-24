"""Full-text search over library chapter content.

``--library-find`` is a metadata-only surface — title, author, fandom,
URL. It can't answer "which fic had that scene at the orphanage?"
because the chapter bodies never make it into the index. This module
maintains a SQLite FTS5 index built from the same library files the
metadata index already tracks, so the question becomes a single query.

Design notes:

* **One SQLite file, one virtual table.** FTS5 supports UNINDEXED
  columns inline with indexed ones; that lets every retrieval go
  through the same row without a join. The DB lives in the portable
  root next to ``library-index.json`` so a portable-layout install
  keeps everything under one folder.

* **Plain text, not HTML.** We strip tags before indexing — a user
  searching for ``"shouted at the dragon"`` shouldn't have the match
  blocked by a ``<em>``. The strip uses BeautifulSoup in the same
  "get_text with a separator" shape the exporters already use, so a
  paragraph tag becomes a visible boundary rather than a word run-on.

* **Whole-story upserts.** Indexing is per-story: re-indexing a story
  drops every existing row for that ``(root, url)`` pair and inserts
  fresh rows for each chapter. Authors revising chapter 4 in place
  still land correctly because the old chapter 4 rows are gone before
  the new ones go in. The full-rebuild cost is a few milliseconds per
  story at chapter-size; bigger than a targeted update but far
  simpler to keep consistent.

* **Stored lightly.** The FTS5 table only carries what the CLI needs
  to render a hit: story identity (root + url + relpath), chapter
  position (number + title), and the body. Richer metadata lives in
  the library index — the CLI look-up resolves hits against that.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

from ..models import Chapter

SCHEMA_VERSION = 1
"""Stored in ``meta(key='schema_version')``. Bumped if the FTS5 table
layout ever changes incompatibly so :meth:`FullTextIndex.load` can
decide between migrate and rebuild."""


_WS_RE = re.compile(r"\s+")


@dataclass
class BootstrapReport:
    """Return value from :func:`populate_from_library`.

    Split by outcome so the CLI can report actionable numbers
    (indexed / skipped / failed) without the caller having to
    re-walk a list of results."""

    indexed: int = 0
    skipped_unsupported: int = 0
    skipped_missing: int = 0
    failed: int = 0
    chapters: int = 0

    def summary(self) -> str:
        lines = [
            f"Indexed {self.indexed} stor"
            f"{'y' if self.indexed == 1 else 'ies'} "
            f"({self.chapters} chapter{'s' if self.chapters != 1 else ''}).",
        ]
        if self.skipped_missing:
            lines.append(
                f"  • {self.skipped_missing} skipped (file missing)."
            )
        if self.skipped_unsupported:
            lines.append(
                f"  • {self.skipped_unsupported} skipped "
                "(unsupported format, e.g. TXT)."
            )
        if self.failed:
            lines.append(f"  • {self.failed} failed to parse.")
        return "\n".join(lines)


def default_db_path() -> Path:
    """Resolve the default DB location. Matches the pattern used by
    :func:`ffn_dl.library.index.default_index_path` so the portable
    layout keeps both files side by side."""
    from .. import portable
    return portable.portable_root() / "library-search.db"


def chapter_text(chapter_html: str | None) -> str:
    """Return plain text extracted from an HTML chapter body.

    Uses a newline separator so paragraph boundaries don't run
    together and the FTS5 tokenizer sees word boundaries where a
    reader would. Collapses runs of whitespace afterwards so the
    stored text doesn't waste space on formatting artefacts. Returns
    ``""`` for ``None`` / empty input so the caller can index an
    empty chapter without a branch.
    """
    if not chapter_html:
        return ""
    soup = BeautifulSoup(chapter_html, "html.parser")
    raw = soup.get_text(separator="\n")
    return _WS_RE.sub(" ", raw).strip()


@dataclass
class FullTextHit:
    """One search hit. ``snippet`` is an FTS5-highlighted excerpt
    around the matched terms — callers can render it directly."""

    root: str
    url: str
    relpath: str
    title: str
    author: str
    chapter_number: int
    chapter_title: str
    snippet: str


class FullTextIndex:
    """Wraps the on-disk SQLite FTS5 index.

    Not thread-safe — callers that need concurrency hold their own
    lock. The library-update path already serialises post-download
    hooks behind ``_hash_lock``, so piggybacking there is the
    cheapest way to stay consistent.
    """

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` mirrors how we use the library
        # index: callers that touch the DB from worker threads hold
        # an external lock. SQLite itself is fine with cross-thread
        # access when the connection isn't being multi-written.
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False,
        )
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        )
        cur.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chapters
            USING fts5(
                root UNINDEXED,
                url UNINDEXED,
                relpath UNINDEXED,
                title,
                author,
                chapter_number UNINDEXED,
                chapter_title,
                content,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """,
        )
        cur.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    # ── Write path ────────────────────────────────────────────────

    def drop_story(self, root: str, url: str) -> int:
        """Remove every chapter row for ``(root, url)``. Returns the
        number of rows deleted — callers typically don't need it, but
        it makes the "re-index replaces cleanly" invariant testable."""
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM chapters WHERE root = ? AND url = ?",
            (root, url),
        )
        deleted = cur.rowcount or 0
        self._conn.commit()
        return deleted

    def index_story(
        self,
        *,
        root: str,
        url: str,
        relpath: str,
        title: str,
        author: str,
        chapters: Iterable[Chapter],
    ) -> int:
        """Drop any existing rows for this story and insert fresh ones.

        Returns the number of chapters indexed. An empty chapter list
        still drops existing rows — an upstream edit that removed
        every chapter would otherwise leave stale text matchable in
        searches forever.
        """
        self.drop_story(root, url)
        rows = []
        for ch in sorted(chapters, key=lambda c: c.number):
            rows.append((
                root,
                url,
                relpath,
                title or "",
                author or "",
                str(ch.number),
                ch.title or "",
                chapter_text(ch.html),
            ))
        if rows:
            self._conn.executemany(
                """
                INSERT INTO chapters
                    (root, url, relpath, title, author,
                     chapter_number, chapter_title, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        self._conn.commit()
        return len(rows)

    # ── Read path ─────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        root: str | None = None,
        limit: int | None = 50,
    ) -> list[FullTextHit]:
        """Run ``query`` against the FTS5 index.

        FTS5 MATCH syntax is passed through verbatim so power users
        can use prefix wildcards (``dragon*``), NEAR, column filters,
        and boolean operators. A bare multi-word query works the way
        SQLite's default AND-of-terms search does — which is the
        expected shape for "find that scene" style queries.

        ``root`` scopes the search to one library root; ``None`` means
        all libraries. ``limit`` caps results; pass ``None`` for "no
        cap". Ordering is FTS5's BM25 ranking (lower = better match),
        so the most relevant hits float to the top even when a
        library is dominated by one fandom.
        """
        needle = (query or "").strip()
        if not needle:
            return []
        sql = (
            "SELECT root, url, relpath, title, author, chapter_number, "
            "       chapter_title, snippet(chapters, 7, '[', ']', '…', 16) "
            "FROM chapters "
            "WHERE chapters MATCH ? "
        )
        params: list[object] = [needle]
        if root is not None:
            sql += "AND root = ? "
            params.append(root)
        sql += "ORDER BY rank "
        if limit is not None:
            sql += "LIMIT ? "
            params.append(int(limit))
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
        except sqlite3.OperationalError as exc:
            # FTS5 raises OperationalError on malformed MATCH
            # expressions. Re-raise as ValueError so the CLI can
            # render it as a user-friendly message rather than a
            # SQLite-flavoured traceback.
            raise ValueError(f"invalid search query: {exc}") from exc
        rows = cur.fetchall()
        hits: list[FullTextHit] = []
        for r in rows:
            try:
                ch_num = int(r[5])
            except (TypeError, ValueError):
                ch_num = 0
            hits.append(FullTextHit(
                root=r[0],
                url=r[1],
                relpath=r[2],
                title=r[3],
                author=r[4],
                chapter_number=ch_num,
                chapter_title=r[6],
                snippet=r[7],
            ))
        return hits

    # ── Housekeeping ──────────────────────────────────────────────

    def stats(self) -> dict:
        """Return ``{"stories": N, "chapters": M, "roots": [..]}`` —
        enough for the CLI ``--populate-search`` summary without
        leaking SQLite specifics out of this module."""
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM chapters")
        chapters = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT COUNT(DISTINCT url) FROM chapters")
        stories = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT DISTINCT root FROM chapters ORDER BY root")
        roots = [r[0] for r in cur.fetchall()]
        return {"stories": stories, "chapters": chapters, "roots": roots}

    def drop_root(self, root: str) -> int:
        """Drop every row for ``root``. Used by the bootstrap when the
        caller re-indexes from scratch — keeps the DB from ballooning
        across repeated full rebuilds."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM chapters WHERE root = ?", (root,))
        deleted = cur.rowcount or 0
        self._conn.commit()
        return deleted

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "FullTextIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def populate_from_library(
    fti: FullTextIndex,
    root: Path,
    *,
    index_path: Path | None = None,
    progress=None,
) -> BootstrapReport:
    """Walk the library index's entries for ``root`` and index every
    readable story into ``fti``.

    Safe to re-run — each story's old rows are dropped before the
    fresh ones go in (see :meth:`FullTextIndex.index_story`). TXT
    exports fall in the unsupported bucket because
    :func:`ffn_dl.updater.read_chapters` can't recover their
    chapter boundaries without heavy guessing.

    ``progress`` is an optional callable accepting a status line;
    defaults to silent so test runs don't spam stdout. The CLI
    wrapper injects ``print`` for human output.
    """
    from ..updater import read_chapters
    from .hashes import ChapterHashUnavailable  # noqa: F401 — surface name
    from .index import LibraryIndex

    report = BootstrapReport()
    idx = LibraryIndex.load(index_path)
    root_resolved = Path(root).expanduser().resolve()
    root_key = str(root_resolved)

    # Wipe the root's existing rows up front so an entry that used to
    # exist but was deleted from the library index doesn't linger as
    # unreachable text rows. This matches ``--populate-hashes``'s
    # "rebuild from current index state" semantics.
    fti.drop_root(root_key)

    for url, entry in idx.stories_in(root_resolved):
        relpath = entry.get("relpath") or ""
        path = root_resolved / relpath
        if not relpath or not path.exists():
            report.skipped_missing += 1
            if progress is not None:
                progress(f"  [skip] {relpath or url}: file missing on disk")
            continue
        try:
            chapters = read_chapters(path)
        except Exception as exc:
            # read_chapters raises ChaptersNotReadableError for TXT
            # (always) and for unrecognised HTML/EPUB layouts. Split
            # them by exception message prefix — cheap and keeps the
            # summary useful without introducing a separate "kind of
            # failure" exception hierarchy.
            message = str(exc)
            if "TXT" in message or "Unsupported format" in message:
                report.skipped_unsupported += 1
                if progress is not None:
                    progress(f"  [skip] {relpath}: {message}")
            else:
                report.failed += 1
                if progress is not None:
                    progress(f"  [fail] {relpath}: {message}")
            continue
        indexed = fti.index_story(
            root=root_key,
            url=url,
            relpath=relpath,
            title=entry.get("title") or "",
            author=entry.get("author") or "",
            chapters=chapters,
        )
        report.indexed += 1
        report.chapters += indexed
        if progress is not None:
            progress(
                f"  [ok]  {relpath}: indexed {indexed} chapter"
                f"{'s' if indexed != 1 else ''}"
            )

    return report
