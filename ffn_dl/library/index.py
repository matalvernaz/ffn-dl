"""LibraryIndex — persistent JSON state for the library manager.

One JSON file lives with the ffn-dl program install (next to
settings.ini in portable mode, under ~/.ffn-dl/ in dev). The file
holds entries for every library root the user has scanned, so a
single ffn-dl install can manage multiple library directories
without collision.

Schema is versioned. v1:
    {
      "version": 1,
      "libraries": {
        "<absolute library root>": {
          "last_scan": "<ISO-8601 UTC>",
          "stories": {
            "<source URL>": {
              "relpath": "<path relative to library root>",
              "title": "...",
              "author": "...",
              "fandoms": [...],
              "adapter": "ffn|ao3|...",
              "format": "epub|html|txt",
              "confidence": "high|medium|low",
              "chapter_count": N,
              "last_checked": "<ISO-8601 UTC>"
            }
          },
          "untrackable": [
            {
              "relpath": "...",
              "format": "...",
              "reason": "..."
            }
          ]
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .candidate import Confidence, StoryCandidate


SCHEMA_VERSION = 1


def default_index_path() -> Path:
    """Resolve the index location when prefs don't override it."""
    from .. import portable
    return portable.portable_root() / "library-index.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_root(root: Path) -> str:
    """Absolute, resolved, string form — used as the dict key."""
    return str(Path(root).expanduser().resolve())


class LibraryIndex:
    """In-memory view of the on-disk library index, with save()."""

    def __init__(self, path: Path, data: dict):
        self._path = Path(path)
        self._data = data

    # ── Construction ────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path | None = None) -> "LibraryIndex":
        """Load the index from disk, or return an empty one if missing
        or malformed. Never raises on a bad file — stale data just gets
        replaced by an empty index on the next save()."""
        p = Path(path) if path else default_index_path()
        if not p.exists():
            return cls(p, _empty())
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls(p, _empty())
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            return cls(p, _empty())
        raw.setdefault("libraries", {})
        return cls(p, raw)

    def save(self) -> None:
        """Atomic write: temp file in the same dir, then rename."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".library-index-", suffix=".json", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ── Library-scoped accessors ────────────────────────────────

    def _library(self, root: Path) -> dict:
        key = _normalize_root(root)
        return self._data["libraries"].setdefault(
            key, {"last_scan": None, "stories": {}, "untrackable": []}
        )

    def record(self, root: Path, candidate: StoryCandidate) -> None:
        """Add or update an entry for this candidate under ``root``.

        Trackable candidates (HIGH/MEDIUM) are keyed by source URL so
        re-scanning an already-known story updates the existing entry
        rather than duplicating it. LOW-confidence candidates go into
        ``untrackable`` where --update-all knows to skip them."""
        lib = self._library(root)
        rel = str(candidate.path.relative_to(Path(root).expanduser().resolve()))
        md = candidate.metadata

        if candidate.is_trackable and md.source_url:
            lib["stories"][md.source_url] = {
                "relpath": rel,
                "title": md.title,
                "author": md.author,
                "fandoms": list(md.fandoms),
                "rating": md.rating,
                "status": md.status,
                "adapter": candidate.adapter_name,
                "format": md.format,
                "confidence": candidate.confidence.value,
                "chapter_count": md.chapter_count,
                "last_checked": _now_iso(),
            }
            return

        lib["untrackable"].append({
            "relpath": rel,
            "format": md.format,
            "title": md.title,
            "author": md.author,
            "reason": (
                "; ".join(candidate.notes)
                if candidate.notes
                else "no identification"
            ),
        })

    def mark_scan_complete(self, root: Path) -> None:
        self._library(root)["last_scan"] = _now_iso()

    def clear_library(self, root: Path) -> None:
        """Drop all entries for a library root. Used when the user
        re-scans from scratch and wants the index to reflect the
        current disk state only (e.g., after deleting files)."""
        lib = self._library(root)
        lib["stories"] = {}
        lib["untrackable"] = []

    def lookup_by_url(self, root: Path, url: str) -> dict | None:
        return self._library(root)["stories"].get(url)

    def stories_in(self, root: Path) -> Iterator[tuple[str, dict]]:
        for url, entry in self._library(root)["stories"].items():
            yield url, entry

    def untrackable_in(self, root: Path) -> list[dict]:
        return list(self._library(root)["untrackable"])

    def library_roots(self) -> list[str]:
        return list(self._data["libraries"].keys())

    @property
    def path(self) -> Path:
        return self._path


def _empty() -> dict:
    return {"version": SCHEMA_VERSION, "libraries": {}}
