"""Library integrity check and self-heal.

Over the life of a library — hundreds of stories, scan-then-update
cycles, the occasional manual file reorganisation — the index on disk
and the files on disk drift apart. Symptoms:

* A story is moved to a new folder; the index still points at the
  old relpath and subsequent library updates silently skip it.
* A file is deleted outside the app; the index still lists it and the
  next rescan has to rediscover that the story is gone.
* Someone drops an EPUB into the library folder by hand; the
  downloader doesn't know about it and won't update it.
* mtime/size cached on the entry is out of step with the real file,
  which defeats the refresh path's "skip unchanged" optimisation and
  makes every library update re-parse every story.

This module produces a structured report describing all four drift
types, and a small set of mutation helpers for fixing each one. The
reporter never mutates the index — callers see the full picture
before committing to a heal, and can pick which categories to act on.

Usage sketch::

    idx = LibraryIndex.load()
    report = check_integrity(root, idx)
    if not report.is_clean():
        print(report.summary())
        healed = heal(root, idx, report, scan_orphans=True,
                      drop_missing=True, refresh_drift=True)
        idx.save()
        print(healed.summary())
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .index import LibraryIndex

_EXTS = (".epub", ".html", ".txt")


@dataclass
class IntegrityReport:
    """What's drifted for a single library root.

    All fields are keyed by the index's stable identifiers — canonical
    URL for trackable stories, the untrackable list index for the
    others — so a heal can re-find them without re-walking.
    """

    root: Path
    orphan_files: list[Path] = field(default_factory=list)
    """Files on disk with a known extension that don't appear as any
    entry's primary ``relpath`` or ``duplicate_relpaths``. Candidates
    for a targeted re-scan."""

    missing_files: list[tuple[str, dict]] = field(default_factory=list)
    """``(url, entry)`` pairs whose ``relpath`` points at a file that
    no longer exists. Candidates for removal from the index — or
    for the user to restore from backup."""

    drifted_entries: list[tuple[str, dict]] = field(default_factory=list)
    """``(url, entry)`` whose stored ``file_mtime`` / ``file_size``
    disagrees with the current stat. The refresh path uses those two
    fields to skip re-parsing unchanged EPUBs; drift defeats the
    optimisation. Healing re-stats and updates the entry."""

    stale_untrackable: list[int] = field(default_factory=list)
    """Indices into the library's ``untrackable`` list whose files
    are gone. Cleanup candidates — untrackable entries don't
    self-prune and accumulate forever otherwise."""

    stale_duplicate_relpaths: dict[str, list[str]] = field(default_factory=dict)
    """``url -> [duplicate relpaths that no longer exist]``. The
    primary copy may still be fine; we only drop the missing siblings."""

    def is_clean(self) -> bool:
        """True when nothing needs healing."""
        return not (
            self.orphan_files
            or self.missing_files
            or self.drifted_entries
            or self.stale_untrackable
            or self.stale_duplicate_relpaths
        )

    def summary(self) -> str:
        """Human-readable one-paragraph summary suitable for the CLI
        or the GUI status pane. Empty categories are omitted so a
        mostly-clean library doesn't get a wall of zeros."""
        if self.is_clean():
            return f"Library {self.root} is clean."
        lines = [f"Library {self.root}:"]
        if self.missing_files:
            lines.append(
                f"  • {len(self.missing_files)} index entr"
                f"{'y' if len(self.missing_files) == 1 else 'ies'} "
                "point at missing file(s)."
            )
        if self.orphan_files:
            lines.append(
                f"  • {len(self.orphan_files)} file(s) on disk "
                "not tracked in the index."
            )
        if self.drifted_entries:
            lines.append(
                f"  • {len(self.drifted_entries)} entr"
                f"{'y' if len(self.drifted_entries) == 1 else 'ies'} "
                "have stale mtime/size cache."
            )
        if self.stale_untrackable:
            lines.append(
                f"  • {len(self.stale_untrackable)} untrackable "
                "record(s) reference deleted files."
            )
        if self.stale_duplicate_relpaths:
            total = sum(
                len(v) for v in self.stale_duplicate_relpaths.values()
            )
            lines.append(
                f"  • {total} duplicate sibling(s) across "
                f"{len(self.stale_duplicate_relpaths)} entr"
                f"{'y' if len(self.stale_duplicate_relpaths) == 1 else 'ies'} "
                "reference deleted files."
            )
        return "\n".join(lines)


@dataclass
class HealReport:
    """Outcome of a heal() run — counters per category so the caller
    can tell the user what actually happened."""

    removed_missing: int = 0
    removed_stale_untrackable: int = 0
    removed_stale_duplicates: int = 0
    refreshed_drift: int = 0
    scanned_orphans: int = 0

    def summary(self) -> str:
        parts = []
        if self.removed_missing:
            parts.append(
                f"dropped {self.removed_missing} missing-file entr"
                f"{'y' if self.removed_missing == 1 else 'ies'}"
            )
        if self.removed_stale_untrackable:
            parts.append(
                f"pruned {self.removed_stale_untrackable} "
                "stale untrackable record(s)"
            )
        if self.removed_stale_duplicates:
            parts.append(
                f"pruned {self.removed_stale_duplicates} stale duplicate(s)"
            )
        if self.refreshed_drift:
            parts.append(
                f"refreshed {self.refreshed_drift} stat cache(s)"
            )
        if self.scanned_orphans:
            parts.append(
                f"indexed {self.scanned_orphans} orphan file(s)"
            )
        if not parts:
            return "No changes."
        return "Healed: " + ", ".join(parts) + "."


# ── Inspection ────────────────────────────────────────────────────

def check_integrity(root: Path, index: LibraryIndex) -> IntegrityReport:
    """Produce a drift report for a library root without mutating
    anything. Callers decide whether to heal.

    Safe to call on a library being actively written — we stat each
    path at most once, and a file disappearing mid-check just shows
    up as a missing-file entry (which would have been detected on
    the next run anyway).
    """
    root = Path(root).expanduser().resolve()
    lib_state = index.library_state(root)

    tracked_relpaths: set[str] = set()
    report = IntegrityReport(root=root)

    # Pass 1 — check every tracked entry.
    for url, entry in index.stories_in(root):
        primary_rel = entry.get("relpath")
        if primary_rel:
            tracked_relpaths.add(primary_rel)
            primary_path = root / primary_rel
            if not primary_path.exists():
                report.missing_files.append((url, entry))
            else:
                # mtime/size drift — only check entries that cached
                # one (older entries may not have, and absent cache
                # isn't drift, just "no cache yet").
                cached_mtime = entry.get("file_mtime")
                cached_size = entry.get("file_size")
                if cached_mtime is not None and cached_size is not None:
                    try:
                        st = primary_path.stat()
                        if (
                            st.st_mtime != cached_mtime
                            or st.st_size != cached_size
                        ):
                            report.drifted_entries.append((url, entry))
                    except OSError:
                        # Disappeared between the exists() check and
                        # stat() — next run will catch it.
                        report.missing_files.append((url, entry))

        dupes = entry.get("duplicate_relpaths") or []
        stale_dupes = [
            rel for rel in dupes if not (root / rel).exists()
        ]
        if stale_dupes:
            report.stale_duplicate_relpaths[url] = stale_dupes
        for rel in dupes:
            tracked_relpaths.add(rel)

    # Pass 2 — untrackable list.
    for idx, record in enumerate(lib_state.get("untrackable", [])):
        rel = record.get("relpath")
        if rel:
            tracked_relpaths.add(rel)
            if not (root / rel).exists():
                report.stale_untrackable.append(idx)

    # Pass 3 — files on disk. Anything with a known extension that
    # isn't in tracked_relpaths is an orphan. We normalise each file's
    # relpath the same way index entries store theirs so the set
    # lookup lines up byte-for-byte.
    for path in _walk_ffn_files(root):
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            continue  # defensive — symlink outside root
        if rel not in tracked_relpaths:
            report.orphan_files.append(path)

    return report


# ── Mutation ──────────────────────────────────────────────────────

def heal(
    root: Path,
    index: LibraryIndex,
    report: IntegrityReport,
    *,
    drop_missing: bool = False,
    refresh_drift: bool = False,
    prune_untrackable: bool = False,
    prune_duplicates: bool = False,
    scan_orphans: bool = False,
) -> HealReport:
    """Apply the subset of heals the caller opts into.

    Each flag defaults to False — healing is never automatic, so a
    diagnostic run doesn't accidentally remove entries the user
    wanted to investigate. The caller is responsible for calling
    :meth:`LibraryIndex.save` after; we intentionally don't, to let
    the caller batch multiple operations into a single on-disk write.
    """
    result = HealReport()
    root = Path(root).expanduser().resolve()
    lib_state = index.library_state(root)

    if drop_missing and report.missing_files:
        stories = lib_state["stories"]
        for url, _entry in report.missing_files:
            if url in stories:
                del stories[url]
                result.removed_missing += 1

    if prune_duplicates and report.stale_duplicate_relpaths:
        stories = lib_state["stories"]
        for url, stale_list in report.stale_duplicate_relpaths.items():
            entry = stories.get(url)
            if entry is None:
                continue
            dupes = entry.get("duplicate_relpaths") or []
            cleaned = [rel for rel in dupes if rel not in stale_list]
            if cleaned:
                entry["duplicate_relpaths"] = cleaned
            else:
                entry.pop("duplicate_relpaths", None)
            result.removed_stale_duplicates += len(stale_list)

    if prune_untrackable and report.stale_untrackable:
        untrackable = lib_state.get("untrackable", [])
        # Delete from the tail so earlier indices stay valid.
        for idx in sorted(report.stale_untrackable, reverse=True):
            if 0 <= idx < len(untrackable):
                del untrackable[idx]
                result.removed_stale_untrackable += 1

    if refresh_drift and report.drifted_entries:
        for url, entry in report.drifted_entries:
            primary_rel = entry.get("relpath")
            if not primary_rel:
                continue
            try:
                st = (root / primary_rel).stat()
            except OSError:
                continue
            entry["file_mtime"] = st.st_mtime
            entry["file_size"] = st.st_size
            result.refreshed_drift += 1

    if scan_orphans and report.orphan_files:
        # Delegate to the real scanner so candidate identification
        # produces the same metadata quality as a full --scan-library
        # run. Only the orphan files are fed in, so the run is cheap.
        from ..updater import extract_metadata
        from .identifier import identify
        for path in report.orphan_files:
            try:
                md = extract_metadata(path)
                candidate = identify(path, md, root=root)
                if index.record(root, candidate):
                    result.scanned_orphans += 1
            except Exception:
                # Mirror scanner.py's behaviour — record_failure on
                # one file doesn't abort the run.
                continue

    return result


# ── Helpers ───────────────────────────────────────────────────────

def _walk_ffn_files(root: Path) -> Iterator[Path]:
    """Yield every fanfic-shaped file under ``root`` (.epub/.html/.txt),
    skipping symlinks so we don't follow convenience links into
    external directories and double-count.
    """
    for dirpath, _dirs, files in os.walk(str(root), followlinks=False):
        for fname in files:
            candidate = Path(dirpath) / fname
            if candidate.is_symlink():
                continue
            if candidate.suffix.lower() in _EXTS:
                yield candidate
