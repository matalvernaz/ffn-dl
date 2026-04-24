"""Mark, revive, and list abandoned-WIP stories in the library index.

A WIP fic (status != Complete) that hasn't had a new chapter in
years is, practically speaking, dead. ``--update-library`` was
still probing them on every run — one HTTP probe per story per run
to discover nothing has changed, which adds up fast on a library of
thousands of fics where a significant fraction are long-abandoned.

This module lets the user declare a WIP dead by stamping an
``abandoned_at`` ISO timestamp on its library-index entry.
``build_refresh_queue`` skips any entry with that field set, so
subsequent update runs stop spending probes on abandoned work.
Reviving (unmarking) one or all stories is a single CLI flag away
— there's no delete step, just a flag flip, so a silently-restored
author's update path stays painless.

Two signals determine abandonment in the auto-mark sweep:

1. **Status is NOT Complete** — finished fics are the
   ``--skip-stale-complete`` feature's territory. Abandonment is
   specifically about WIPs the author has walked away from.
2. **File mtime is older than ``days`` days.** The local file's
   last-written time is the best proxy for "when did a new chapter
   land" that we have without introducing a new tracked field.
   A fic we re-downloaded yesterday isn't abandoned even if its
   first chapter went up a decade ago.

Existing ``abandoned_at`` entries are not re-stamped; the mark is
sticky until ``revive_abandoned`` clears it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..updater import extract_status
from .index import LibraryIndex


SECONDS_PER_DAY = 86400


def _now_iso() -> str:
    """ISO-8601 UTC timestamp matching the shape the library index
    already uses for ``last_scan`` / ``last_probed``. Same format so a
    future reader can compare timestamps across fields with a single
    ``datetime.fromisoformat`` call."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class MarkReport:
    """Summary of a :func:`mark_abandoned` sweep.

    Kept as a dataclass (not a bare dict) so callers can rely on
    attribute access and pyright/mypy surface typos in summary-
    rendering code at review time rather than at runtime.
    """

    newly_marked: list[tuple[str, str]] = field(default_factory=list)
    """``(url, relpath)`` pairs marked on this run."""

    already_marked: int = 0
    """Entries that were already abandoned and left alone."""

    kept_complete: int = 0
    """Complete entries skipped — abandonment is a WIP concern."""

    kept_fresh: int = 0
    """WIPs whose file mtime is under the threshold — still alive."""

    kept_missing: int = 0
    """Index entries whose file is gone — mtime unavailable."""

    @property
    def newly_marked_count(self) -> int:
        return len(self.newly_marked)

    def summary(self) -> str:
        if not (self.newly_marked or self.already_marked):
            return "No stories matched the abandonment criteria."
        lines = [
            f"Marked {self.newly_marked_count} stor"
            f"{'y' if self.newly_marked_count == 1 else 'ies'} as abandoned.",
        ]
        if self.already_marked:
            lines.append(
                f"  • {self.already_marked} already-abandoned "
                "entr(y/ies) left alone."
            )
        if self.kept_complete:
            lines.append(
                f"  • {self.kept_complete} Complete stor"
                f"{'y' if self.kept_complete == 1 else 'ies'} ignored "
                "(not a WIP)."
            )
        if self.kept_fresh:
            lines.append(
                f"  • {self.kept_fresh} WIP(s) still within the "
                "threshold — not marked."
            )
        if self.kept_missing:
            lines.append(
                f"  • {self.kept_missing} entr(y/ies) with no file "
                "on disk — mtime unavailable, skipped."
            )
        return "\n".join(lines)


def mark_abandoned(
    index: LibraryIndex,
    root: Path,
    days: int,
    *,
    now_epoch: float | None = None,
) -> MarkReport:
    """Stamp ``abandoned_at`` on any WIP stor(y/ies) in ``root``
    whose file mtime is older than ``days`` days.

    The caller is responsible for calling :meth:`LibraryIndex.save`
    afterwards — same contract as :func:`store_hashes` — so a
    script that marks several roots can batch a single save.

    ``now_epoch`` is a test hook; production callers let it default
    to the current wall time.
    """
    import time as _time

    if days <= 0:
        raise ValueError("days must be positive")
    root = Path(root).expanduser().resolve()
    current = _time.time() if now_epoch is None else float(now_epoch)
    cutoff_epoch = current - days * SECONDS_PER_DAY
    stamp = _now_iso()
    report = MarkReport()

    for url, entry in index.stories_in(root):
        if entry.get("abandoned_at"):
            report.already_marked += 1
            continue

        rel = entry.get("relpath") or ""
        path = root / rel
        if not path.exists():
            report.kept_missing += 1
            continue

        try:
            status = extract_status(path) or entry.get("status") or ""
        except Exception:
            status = entry.get("status") or ""
        if status.lower() == "complete":
            report.kept_complete += 1
            continue

        try:
            file_mtime = path.stat().st_mtime
        except OSError:
            report.kept_missing += 1
            continue
        if file_mtime >= cutoff_epoch:
            report.kept_fresh += 1
            continue

        entry["abandoned_at"] = stamp
        report.newly_marked.append((url, rel))

    return report


@dataclass
class ReviveReport:
    """Summary of a :func:`revive_abandoned` call. Split from
    :class:`MarkReport` so callers can tell the two counter shapes
    apart at a glance and tests can pin the exact fields that
    should show up in the CLI summary."""

    revived: list[tuple[str, str]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        if self.revived:
            lines.append(
                f"Revived {len(self.revived)} stor"
                f"{'y' if len(self.revived) == 1 else 'ies'}."
            )
        if self.missing:
            lines.append(
                f"{len(self.missing)} URL(s) had no abandoned entry "
                "in any indexed library."
            )
        if not lines:
            return "Nothing to revive."
        return "\n".join(lines)


def revive_abandoned(
    index: LibraryIndex,
    urls: Iterable[str] | None = None,
    *,
    roots: Iterable[Path] | None = None,
) -> ReviveReport:
    """Clear ``abandoned_at`` on the matching stor(y/ies).

    Called with ``urls=None`` and ``roots=[root]`` (or ``None``),
    revives every abandoned story across those roots — the "I
    changed my mind, unmark everything" path. Called with specific
    URLs, revives only those — useful when the user learned one
    story updated again.

    URLs that don't correspond to an abandoned entry in any of the
    searched roots land in :attr:`ReviveReport.missing` so the CLI
    can surface typos or entries the user has already cleaned up.
    """
    report = ReviveReport()
    if roots is None:
        root_list = [Path(r) for r in index.library_roots()]
    else:
        root_list = [Path(r).expanduser().resolve() for r in roots]

    if urls is None:
        for root in root_list:
            for url, entry in index.stories_in(root):
                if entry.pop("abandoned_at", None):
                    report.revived.append((
                        url, entry.get("relpath") or "",
                    ))
        return report

    url_set = set(urls)
    remaining = set(url_set)
    for root in root_list:
        for url, entry in index.stories_in(root):
            if url in url_set and entry.pop("abandoned_at", None):
                report.revived.append((
                    url, entry.get("relpath") or "",
                ))
                remaining.discard(url)
    report.missing = sorted(remaining)
    return report


@dataclass
class AbandonedListing:
    """One entry in :func:`list_abandoned`'s output, carrying enough
    context for both the CLI text rendering and a future GUI list
    control without the caller reaching into the index internals."""

    root: Path
    url: str
    relpath: str
    title: str
    author: str
    abandoned_at: str


def list_abandoned(
    index: LibraryIndex,
    *,
    roots: Iterable[Path] | None = None,
) -> list[AbandonedListing]:
    """Return every currently-abandoned story across ``roots``.

    Ordering is ``(root, abandoned_at DESC, relpath)`` — most-
    recently-marked entries surface first so the user reviewing
    their abandonment list scans from "I just declared this dead"
    backwards through time.
    """
    if roots is None:
        root_list = [Path(r) for r in index.library_roots()]
    else:
        root_list = [Path(r).expanduser().resolve() for r in roots]

    rows: list[AbandonedListing] = []
    for root in root_list:
        root_rows: list[AbandonedListing] = []
        for url, entry in index.stories_in(root):
            stamp = entry.get("abandoned_at")
            if not stamp:
                continue
            root_rows.append(AbandonedListing(
                root=Path(root),
                url=url,
                relpath=entry.get("relpath") or "",
                title=entry.get("title") or "",
                author=entry.get("author") or "",
                abandoned_at=str(stamp),
            ))
        root_rows.sort(
            key=lambda r: (r.abandoned_at, r.relpath),
            reverse=True,
        )
        # Reverse-sort above puts newest first; .reverse() on the
        # relpath tiebreaker would be wrong, so the sort key does
        # both together with the outer reverse=True. Explicit
        # secondary sort on relpath alone handles ties cleanly.
        rows.extend(root_rows)
    return rows
