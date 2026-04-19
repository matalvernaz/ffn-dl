"""Turn a read FileMetadata into an identified StoryCandidate.

Phase 1 is URL-only. If the file has an embedded source URL that
matches one of our adapters, confidence is HIGH and adapter_name is
filled in. Everything else is LOW — the file is still indexed, just
not auto-updatable until the review flow (Phase 4) resolves it.
"""

from __future__ import annotations

from pathlib import Path

from ..updater import FileMetadata
from .candidate import Confidence, StoryCandidate


# Site identifiers returned by identify(). These mirror the class names
# in cli._detect_site but as plain strings so the library package
# doesn't need to import every scraper class up front.
_URL_MARKERS = [
    ("ficwad.com", "ficwad"),
    ("archiveofourown.org", "ao3"),
    ("ao3.org", "ao3"),
    ("royalroad.com", "royalroad"),
    ("mediaminer.org", "mediaminer"),
    ("literotica.com", "literotica"),
    ("wattpad.com", "wattpad"),
    ("fanfiction.net", "ffn"),
]


def adapter_for_url(url: str) -> str | None:
    """Return the short adapter name for a story URL, or None if the
    URL doesn't match any supported site. Used for indexing; the
    actual scraper class lookup happens through cli._detect_site when
    we need to probe for updates."""
    if not url:
        return None
    lower = url.lower()
    for marker, name in _URL_MARKERS:
        if marker in lower:
            return name
    return None


def identify(path: Path, metadata: FileMetadata) -> StoryCandidate:
    """Assemble a StoryCandidate from a path + its read metadata."""
    candidate = StoryCandidate(path=path, metadata=metadata)

    if metadata.source_url:
        adapter = adapter_for_url(metadata.source_url)
        if adapter:
            candidate.adapter_name = adapter
            candidate.confidence = Confidence.HIGH
            return candidate
        candidate.notes.append(
            f"source URL {metadata.source_url!r} does not match any "
            "supported site; indexed but not trackable"
        )
        return candidate

    if not metadata.title and not metadata.author:
        candidate.notes.append(
            "no embedded URL, title, or author; filename is the only "
            "identifier — run --review-library to match it interactively"
        )
    else:
        candidate.notes.append(
            "no embedded URL; title/author present but fuzzy matching "
            "is deferred to --review-library"
        )
    return candidate
