"""Library-level glue for chapter content hashes.

The pure hashing primitives live in :mod:`ffn_dl.content_hash` — this
module ties them to the library index and to the on-disk story files
so the CLI's ``--populate-hashes`` and ``--scan-edits`` flows don't
need to know anything beyond "read hashes for this entry", "write
hashes for this entry".

Two entry points matter:

* :func:`compute_local_hashes` parses a local export file (EPUB or
  HTML) back into chapters and hashes each. Used by the bootstrap
  path to backfill hashes for stories downloaded before this feature
  shipped, and by the detection path as the local side of the
  comparison.
* :func:`store_hashes` writes a hash list onto a library-index entry
  without disturbing the rest of the entry's fields. ``index.save()``
  is the caller's responsibility — so a batch bootstrap can write
  once at the end instead of N times.
"""

from __future__ import annotations

from pathlib import Path

from ..content_hash import hash_chapters
from ..updater import ChaptersNotReadableError, read_chapters
from .index import LibraryIndex


class ChapterHashUnavailable(Exception):
    """Raised when a local file can't be parsed into chapters for
    hashing. Wraps :class:`ChaptersNotReadableError` with a
    library-level message so callers don't need to import the
    updater's error type."""


def compute_local_hashes(file_path: Path) -> list[str]:
    """Return the chapter-content hashes for a local story file.

    Parses the file back into :class:`~ffn_dl.models.Chapter` objects
    and hashes each. Raises :class:`ChapterHashUnavailable` for
    unsupported formats (TXT today) or for files whose content
    doesn't match the ffn-dl export shape — callers typically skip
    those stories rather than treat the failure as drift.
    """
    try:
        chapters = read_chapters(file_path)
    except ChaptersNotReadableError as exc:
        raise ChapterHashUnavailable(str(exc)) from exc
    return hash_chapters(chapters)


def store_hashes(
    index: LibraryIndex,
    root: Path,
    url: str,
    hashes: list[str],
) -> bool:
    """Write ``hashes`` onto the story entry keyed by ``url`` under
    ``root``.

    Returns True on hit, False when no entry matches (URL normalised
    differently or story removed between check and write). The
    caller decides whether to :meth:`LibraryIndex.save`.

    An empty ``hashes`` list removes the stored field entirely — the
    difference between "never hashed" and "hashed, had zero
    chapters" isn't meaningful; both produce the same comparison
    outcome later.
    """
    entry = index.lookup_by_url(Path(root), url)
    if entry is None:
        return False
    if hashes:
        entry["chapter_hashes"] = list(hashes)
    else:
        entry.pop("chapter_hashes", None)
    return True


def stored_hashes(entry: dict) -> list[str] | None:
    """Return the ``chapter_hashes`` list from an index entry, or
    ``None`` when none was ever stored.

    A thin wrapper that gives callers a consistent shape: "list"
    means "we have ground truth", "None" means "not yet populated,
    skip drift comparison", which the detection path can present
    as a separate category from "entry failed to compare"."""
    val = entry.get("chapter_hashes")
    if not isinstance(val, list):
        return None
    return [str(h) for h in val]
