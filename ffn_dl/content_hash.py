"""Chapter-content hashing for silent-edit detection.

Fanfiction authors revise chapters in place — fixing typos, tweaking
dialogue, sometimes rewriting whole scenes — without bumping the
chapter count. A count-based update check (``--update-library``'s
current default) can't see those edits, and the local copy drifts
from whatever's canon upstream. Over months this turns into "wait,
that paragraph was different" for readers with long libraries.

This module hashes each chapter's HTML body so a later comparison
can tell whether the upstream content has changed even when the
chapter count hasn't. The stored hash lives next to the other
per-story metadata in the library index; the scanner and refresh
paths read/write it the same way they read/write ``chapter_count``.

Design notes:

* **Light normalisation only.** We collapse whitespace runs and trim
  outer whitespace. We deliberately *don't* re-parse through
  BeautifulSoup or a DOM normaliser — that would couple the hash to
  whichever parser version ffn-dl ships, so upgrading ``lxml`` would
  invalidate every stored hash in every library on the next check.
  The whitespace pass catches the one real false-positive pattern:
  re-formatted HTML (pretty-printer output, tab↔space differences
  between exporters) that doesn't actually change the prose.

* **SHA-256.** Cryptographic-strength isn't the point — collision
  resistance against a motivated attacker doesn't matter for
  fanfiction — but SHA-256 is stdlib, fast enough on a chapter-sized
  input (microseconds), and the output is short enough (64 hex) to
  store comfortably in the library index JSON.

* **Order matters.** Hashes are always taken in chapter-number
  order, so a library entry's ``chapter_hashes[i]`` corresponds to
  ``chapter_count`` position ``i+1``. An out-of-order chapter list
  would otherwise produce different hashes on redownload even
  without any real edit.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

from .models import Chapter, Story


_WS_RE = re.compile(r"\s+")
_INTER_TAG_WS_RE = re.compile(r">\s+<")


def normalise_chapter_html(html: str | None) -> str:
    """Normalise a chapter body so cosmetic re-formatting doesn't
    shift the hash.

    Two passes:

    1. Collapse whitespace runs to a single space. Catches differences
       in indent, tab-vs-space, and line-ending style.
    2. Drop whitespace that sits *between* tag boundaries (``>\\s+<``
       → ``><``). That's where pretty-printers insert cosmetic newlines
       without actually changing the prose — the most common trigger
       for a false-positive silent-edit flag.

    We deliberately keep whitespace that borders text content so a
    real edit that inserts or removes a space (e.g. ``"its"`` →
    ``"it's"``) still shifts the hash. ``None`` is treated as empty.
    """
    if not html:
        return ""
    collapsed = _WS_RE.sub(" ", html).strip()
    return _INTER_TAG_WS_RE.sub("><", collapsed)


def hash_chapter(chapter_html: str | None) -> str:
    """Return the SHA-256 hex digest of a single chapter body.

    Empty or ``None`` input yields the canonical hash of the empty
    string — the caller can distinguish "chapter not fetched yet"
    from "chapter exists and is empty" by looking at
    :class:`ffn_dl.models.Chapter.html` directly if it matters.
    """
    return hashlib.sha256(
        normalise_chapter_html(chapter_html).encode("utf-8"),
    ).hexdigest()


def hash_chapters(chapters: Iterable[Chapter]) -> list[str]:
    """Hash every chapter in the iterable in chapter-number order.

    The caller doesn't have to pre-sort — we do. Hashes align with
    chapter numbers 1..N rather than insertion order, so a scraper
    that happened to populate ``story.chapters`` out of order still
    produces a stable, comparable hash list.
    """
    ordered = sorted(chapters, key=lambda c: c.number)
    return [hash_chapter(c.html) for c in ordered]


def story_chapter_hashes(story: Story) -> list[str]:
    """Convenience: hashes for every chapter of a downloaded Story,
    in chapter-number order."""
    return hash_chapters(story.chapters)


def diff_hashes(
    stored: list[str],
    fresh: list[str],
) -> list[int]:
    """Return the 1-indexed chapter numbers whose hashes changed.

    A length mismatch (``len(stored) != len(fresh)``) means chapters
    were added or removed — that's a *count* change, not a silent
    edit, and is outside the scope of this function. The caller is
    expected to short-circuit on count mismatch before diffing
    hashes. For defensive behaviour we diff the common prefix and
    report any mismatches within it.
    """
    common = min(len(stored), len(fresh))
    return [
        i + 1
        for i in range(common)
        if stored[i] != fresh[i]
    ]
