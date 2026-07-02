"""Resolve a downloaded story to ordered, clean per-chapter text.

Two sources, both producing the same :class:`ReaderChapter` shape:

* the on-disk chapter cache the scraper already writes
  (``<cache>/<site>_<id>/ch_NNNN.json`` = ``{"title", "html"}``), read
  directly so the reader never re-fetches; and
* an exported EPUB/HTML file, via :func:`ficary.updater.read_chapters`, for
  library entries whose cache was cleared.

Chapter text comes from :func:`ficary.exporters.html_to_text`, which keeps
paragraph breaks as blank lines — the structure both the screen-reader view
and the Phase 2 TTS chunker rely on. Chapters load lazily and are memoized.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..exporters import html_to_text
from ..models import Chapter, format_chapter_heading
from .. import sites

logger = logging.getLogger(__name__)

_CHAPTER_STEM_PREFIX = "ch_"


class ReaderSourceError(Exception):
    """The requested story can't be opened for reading."""


@dataclass
class ReaderChapter:
    number: int
    heading: str  # display heading via format_chapter_heading
    text: str     # clean, paragraph-preserving plain text


class StorySource:
    """Ordered chapters for one story, loaded lazily and memoized.

    ``loader`` is a ``Callable[[int], Chapter]`` taking a 1-based chapter
    number and returning a raw :class:`ficary.models.Chapter` (title + html);
    :meth:`load_chapter` converts it to display text.
    """

    def __init__(self, *, title: str, author: str, story_key: str,
                 chapter_count: int, loader: Callable[[int], Chapter]):
        self.title = title
        self.author = author
        self.story_key = story_key
        self._count = chapter_count
        self._loader = loader
        self._cache: dict[int, ReaderChapter] = {}

    def chapter_count(self) -> int:
        return self._count

    def load_chapter(self, number: int) -> ReaderChapter:
        cached = self._cache.get(number)
        if cached is not None:
            return cached
        chapter = self._loader(number)
        rc = ReaderChapter(
            number=number,
            heading=format_chapter_heading(number, chapter.title),
            text=html_to_text(chapter.html),
        )
        self._cache[number] = rc
        return rc

    # ── constructors ──────────────────────────────────────────────
    @classmethod
    def from_cache_dir(cls, cache_dir, url: str, *, title: str = "",
                       author: str = "") -> "StorySource":
        """Build from the scraper's on-disk chapter cache directory."""
        cache_dir = Path(cache_dir)
        numbers = _cached_chapter_numbers(cache_dir)
        if not numbers:
            raise ReaderSourceError(f"No cached chapters in {cache_dir}")
        meta = _read_meta(cache_dir)
        count = max(numbers)

        def loader(n: int) -> Chapter:
            ch = _read_cached_chapter(cache_dir, n)
            if ch is None:
                raise ReaderSourceError(f"Chapter {n} missing from cache {cache_dir}")
            return ch

        return cls(
            title=title or meta.get("title") or "Untitled",
            author=author or meta.get("author") or "Unknown",
            story_key=sites.canonical_url(url) or url,
            chapter_count=count,
            loader=loader,
        )

    @classmethod
    def from_file(cls, path, *, url: str = "", title: str = "",
                  author: str = "") -> "StorySource":
        """Build from an exported EPUB/HTML file (a library entry)."""
        from ..updater import read_chapters
        path = Path(path)
        try:
            chapters = read_chapters(path)
        except Exception as exc:
            # read_chapters raises ChaptersNotReadableError (a sibling of
            # ReaderSourceError, not a subclass) for third-party EPUBs,
            # corrupt zips, and unsupported suffixes — translate so the
            # GUI's single except clause catches everything.
            raise ReaderSourceError(f"Can't read {path.name}: {exc}") from exc
        if not chapters:
            raise ReaderSourceError(f"No chapters found in {path}")
        by_number = {c.number: c for c in chapters}

        def loader(n: int) -> Chapter:
            try:
                return by_number[n]
            except KeyError:
                raise ReaderSourceError(f"Chapter {n} not present in {path}")

        return cls(
            title=title or path.stem or "Untitled",
            author=author or "Unknown",
            story_key=(sites.canonical_url(url) or url) if url else str(path.resolve()),
            chapter_count=max(by_number),
            loader=loader,
        )


def _cached_chapter_numbers(cache_dir: Path) -> set[int]:
    """Chapter numbers present in the cache dir, across .json and legacy
    .html chapter files."""
    if not cache_dir.exists():
        return set()
    nums: set[int] = set()
    for suffix in (".json", ".html"):
        for p in cache_dir.glob(f"{_CHAPTER_STEM_PREFIX}[0-9]*{suffix}"):
            try:
                nums.add(int(p.stem[len(_CHAPTER_STEM_PREFIX):]))
            except ValueError:
                continue
    return nums


def _read_meta(cache_dir: Path) -> dict:
    path = cache_dir / "meta.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_cached_chapter(cache_dir: Path, n: int) -> Optional[Chapter]:
    path = cache_dir / f"{_CHAPTER_STEM_PREFIX}{n:04d}.json"
    if not path.exists():
        legacy = cache_dir / f"{_CHAPTER_STEM_PREFIX}{n:04d}.html"
        if not legacy.exists():
            return None
        path = legacy
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return Chapter(number=n, title=data.get("title", ""), html=data.get("html", ""))
    except (OSError, ValueError):
        return None
