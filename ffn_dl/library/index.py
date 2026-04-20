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
              "file_mtime": <float>,   # stat().st_mtime, for cache-invalidate
              "file_size": <int>,      # stat().st_size,  for cache-invalidate
              "last_checked": "<ISO-8601 UTC>",
              "last_probed": "<ISO-8601 UTC>"  # optional
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
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .candidate import Confidence, StoryCandidate

logger = logging.getLogger(__name__)


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
        _migrate_non_canonical_keys(raw)
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

    def library_state(self, root: Path) -> dict:
        """Mutable in-place dict for a library root: ``stories``,
        ``untrackable``, and ``last_scan``. Callers that need to
        promote/demote entries reach in through this; single-read
        consumers stick to the stories_in / untrackable_in helpers."""
        return self._library(root)

    def record(self, root: Path, candidate: StoryCandidate) -> bool:
        """Add or update an entry for this candidate under ``root``.

        Trackable candidates (HIGH/MEDIUM) are keyed by *canonical*
        source URL (see :func:`ffn_dl.sites.canonical_url`) so the same
        story embedded at two paths with slightly different URL forms
        (``/s/N`` vs ``/s/N/1/``) collapses to a single entry.

        When a second file maps to an already-recorded URL, the new
        relpath is appended to ``duplicate_relpaths`` on the existing
        entry rather than overwriting the primary ``relpath``. This
        preserves the original without silently losing information
        about the other copy. The scanner uses the return value to
        count duplicates for its scan summary: True if a new entry was
        created, False if this was a duplicate of an existing one.
        LOW-confidence candidates always append to ``untrackable`` and
        return True.
        """
        from ..sites import canonical_url

        lib = self._library(root)
        rel = str(candidate.path.relative_to(Path(root).expanduser().resolve()))
        md = candidate.metadata

        if candidate.is_trackable and md.source_url:
            key = canonical_url(md.source_url) or md.source_url
            existing = lib["stories"].get(key)
            if existing is not None and existing.get("relpath") != rel:
                # Duplicate copy of a story we've already indexed. Keep
                # the primary entry's relpath stable (so reorganise /
                # update-library keep pointing at the same file) and
                # record the second path as a sibling. Deduplicate in
                # case the same path turns up twice in a re-scan.
                dupes = existing.setdefault("duplicate_relpaths", [])
                if rel not in dupes and rel != existing.get("relpath"):
                    dupes.append(rel)
                return False

            # Preserve fields the scanner doesn't rewrite (last_probed,
            # duplicate_relpaths) so a re-scan never forgets that the
            # update path already hit the remote for this URL. Without
            # this merge, rescan_library() after --update-library would
            # wipe last_probed and defeat the TTL skip on the next run.
            existing_preserved = {}
            if existing is not None:
                for k in ("last_probed", "duplicate_relpaths"):
                    if k in existing:
                        existing_preserved[k] = existing[k]

            entry_record = {
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
            # Stamp mtime/size so build_refresh_queue can skip the
            # ebooklib re-parse on unchanged files. Stat can race
            # (file removed between walk and record) — fall back to
            # leaving the fields absent, which forces a fresh read on
            # the next probe. Better a slow probe than a wrong cache.
            try:
                st = candidate.path.stat()
                entry_record["file_mtime"] = st.st_mtime
                entry_record["file_size"] = st.st_size
            except OSError:
                pass
            entry_record.update(existing_preserved)
            lib["stories"][key] = entry_record
            return True

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
        return True

    def mark_scan_complete(self, root: Path) -> None:
        self._library(root)["last_scan"] = _now_iso()

    def mark_probed(
        self, root: Path, urls: list[str], *, timestamp: str | None = None,
    ) -> int:
        """Stamp ``last_probed`` for every URL in ``urls``.

        Returns how many entries were actually updated — URLs absent
        from the index (e.g. a story that got removed between probe
        and stamp) are silently skipped. Saves once at the end so a
        library-update pass does a single disk write rather than N.
        """
        stamp = timestamp or _now_iso()
        stories = self._library(root)["stories"]
        touched = 0
        missed: list[str] = []
        for url in urls:
            entry = stories.get(url)
            if entry is None:
                missed.append(url)
                continue
            entry["last_probed"] = stamp
            touched += 1
        if touched:
            self.save()
        # Observability hook. If touched < len(urls), the caller sent
        # URLs that don't match any stored key — most often a path-
        # normalisation mismatch between the probe's root and the
        # stored library root, which silently drains stamps.
        logger.info(
            "mark_probed: stamped %d/%d under %r",
            touched, len(urls), _normalize_root(root),
        )
        if missed:
            logger.warning(
                "mark_probed: %d URL(s) had no matching index entry: %r",
                len(missed), missed[:5],
            )
        return touched

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


def _migrate_non_canonical_keys(raw: dict) -> None:
    """Re-key each library's ``stories`` dict by canonical URL.

    Indexes written by 1.20.x and earlier keyed entries by whatever
    source URL the parser pulled out of the file, including the
    ``/s/N/`` and ``/s/N/1/`` variants FFN uses. When two files for
    the same story happened to carry different URL shapes, both landed
    as separate entries — silently doubling up. ``canonical_url`` now
    collapses them, so on load we re-key the stored entries in place
    and merge any collisions into a primary + ``duplicate_relpaths``
    pair. The net effect is that upgrading to this build recovers the
    missing duplicates from an existing index without forcing a
    full re-scan, while a future scan produces the same layout.
    """
    # Imported locally because sites.py imports scraper modules that
    # pull in heavy dependencies; keeping the import out of module
    # scope avoids paying that cost on every library-tool invocation.
    from ..sites import canonical_url

    for lib in raw.get("libraries", {}).values():
        stories = lib.get("stories", {})
        if not isinstance(stories, dict):
            continue
        rekeyed: dict[str, dict] = {}
        for old_key, entry in stories.items():
            new_key = canonical_url(old_key) or old_key
            existing = rekeyed.get(new_key)
            if existing is None:
                rekeyed[new_key] = entry
                continue
            # Collision — merge ``entry`` into ``existing`` as a
            # duplicate. Keep the entry with more populated metadata
            # as the primary so the richer record wins.
            primary, secondary = _pick_primary_entry(existing, entry)
            dupes = primary.setdefault("duplicate_relpaths", [])
            candidate_rel = secondary.get("relpath")
            if candidate_rel and candidate_rel not in dupes and candidate_rel != primary.get("relpath"):
                dupes.append(candidate_rel)
            rekeyed[new_key] = primary
        lib["stories"] = rekeyed


def _entry_completeness_score(entry: dict) -> int:
    """Score a story entry by how much metadata it has.

    Used to decide which of two colliding entries keeps the primary
    ``relpath`` when merging — prefer the one with more fields
    populated rather than defaulting to whichever was scanned first.
    """
    fields = ("title", "author", "chapter_count", "rating", "status")
    return sum(1 for f in fields if entry.get(f))


def _pick_primary_entry(a: dict, b: dict) -> tuple[dict, dict]:
    """Return ``(primary, secondary)`` for two entries that collided.

    Higher-completeness wins; ties go to ``a`` (which was inserted
    first in the walk) so the merge is deterministic.
    """
    if _entry_completeness_score(b) > _entry_completeness_score(a):
        return b, a
    return a, b
