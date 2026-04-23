"""Library statistics — counts, distributions, staleness.

Useful for library hygiene: "how many stories do I have? from which
sites? how many are stale? how many are incomplete?" — the kind of
questions a user on a 700-story library wants to answer without
exporting the whole index to a spreadsheet.

Read-only. The report is built from whatever's currently in the index
— running a fresh ``--scan-library`` or ``--update-library`` first
produces more accurate answers for "status" fields that are populated
by the scan, not by this module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .index import LibraryIndex


@dataclass
class LibraryStats:
    """Summary of a single library root's current state.

    All counters default to zero so a report for an empty library
    still renders cleanly. Fields are intentionally flat (no nested
    dicts in a dict) so the ``summary()`` method can format them
    consistently no matter which library is inspected.
    """

    root: Path
    total_stories: int = 0
    total_chapters: int = 0
    untrackable_files: int = 0
    duplicate_files: int = 0

    by_site: Counter[str] = field(default_factory=Counter)
    by_format: Counter[str] = field(default_factory=Counter)
    by_status: Counter[str] = field(default_factory=Counter)
    by_rating: Counter[str] = field(default_factory=Counter)
    top_fandoms: list[tuple[str, int]] = field(default_factory=list)

    never_probed: int = 0
    stale_probe: int = 0
    """Probed more than ``STALE_PROBE_DAYS`` ago."""

    pending_updates: int = 0
    """Entries where ``remote_chapter_count`` > local ``chapter_count``."""

    newest_checked: str | None = None
    oldest_checked: str | None = None

    def summary(self) -> str:
        """Human-readable multi-line summary suitable for the CLI."""
        lines = [
            f"Library: {self.root}",
            f"  Stories tracked:     {self.total_stories}",
            f"  Chapters total:      {self.total_chapters}",
        ]
        if self.untrackable_files:
            lines.append(
                f"  Untrackable files:   {self.untrackable_files}"
            )
        if self.duplicate_files:
            lines.append(
                f"  Duplicate copies:    {self.duplicate_files}"
            )

        if self.by_site:
            lines.append("  By source site:")
            for site, count in sorted(
                self.by_site.items(), key=lambda kv: (-kv[1], kv[0]),
            ):
                lines.append(f"    {site:<14} {count}")

        if self.by_status:
            lines.append("  By status:")
            for status, count in sorted(
                self.by_status.items(), key=lambda kv: (-kv[1], kv[0]),
            ):
                lines.append(f"    {status:<14} {count}")

        if self.by_format:
            lines.append("  By format:")
            for fmt, count in sorted(
                self.by_format.items(), key=lambda kv: (-kv[1], kv[0]),
            ):
                lines.append(f"    {fmt:<14} {count}")

        if self.top_fandoms:
            lines.append("  Top fandoms:")
            for fandom, count in self.top_fandoms:
                lines.append(f"    {count:>4}  {fandom}")

        if self.never_probed or self.stale_probe or self.pending_updates:
            lines.append("  Freshness:")
            if self.never_probed:
                lines.append(
                    f"    Never probed:      {self.never_probed}"
                )
            if self.stale_probe:
                lines.append(
                    f"    Probe >{STALE_PROBE_DAYS}d old:     {self.stale_probe}"
                )
            if self.pending_updates:
                lines.append(
                    f"    Pending updates:   {self.pending_updates}  "
                    "(remote has more chapters than local)"
                )

        if self.newest_checked or self.oldest_checked:
            lines.append("  Scan window:")
            if self.newest_checked:
                lines.append(
                    f"    Most recent:       {self.newest_checked}"
                )
            if self.oldest_checked:
                lines.append(
                    f"    Oldest:            {self.oldest_checked}"
                )
        return "\n".join(lines)


STALE_PROBE_DAYS = 30
"""Entries whose ``last_probed`` is older than this many days count
as "stale probe" in the freshness report. The default matches the
TTL the library-update path uses when deciding to re-probe, so the
stats answer "how many stories would a fresh update pass re-check?"
"""

_TOP_FANDOMS = 10
"""How many of the biggest fandoms to list. Long tails lose meaning
past the top dozen — anyone who wants the full list can iterate the
index directly."""


def compute_stats(root: Path, index: LibraryIndex) -> LibraryStats:
    """Build a :class:`LibraryStats` for one library root.

    Never raises — entries with missing or malformed fields contribute
    to ``unknown``/``other`` buckets rather than aborting the report.
    """
    root = Path(root).expanduser().resolve()
    stats = LibraryStats(root=root)

    now = datetime.now(tz=timezone.utc)
    stale_cutoff = STALE_PROBE_DAYS * 24 * 3600

    fandom_counts: Counter[str] = Counter()

    for _url, entry in index.stories_in(root):
        stats.total_stories += 1
        stats.total_chapters += int(entry.get("chapter_count") or 0)

        # Count duplicates — multiple copies of the same story in
        # different folders. These aren't untrackable; they're
        # alternate paths to a story we've already indexed.
        dupes = entry.get("duplicate_relpaths") or []
        stats.duplicate_files += len(dupes)

        stats.by_site[entry.get("adapter") or "unknown"] += 1
        stats.by_format[entry.get("format") or "unknown"] += 1
        stats.by_status[entry.get("status") or "unknown"] += 1
        stats.by_rating[entry.get("rating") or "unknown"] += 1

        for fandom in entry.get("fandoms") or []:
            if fandom:
                fandom_counts[fandom] += 1

        # Freshness — "has this story been probed? when last?"
        last_probed = entry.get("last_probed")
        if not last_probed:
            stats.never_probed += 1
        else:
            try:
                when = datetime.strptime(
                    last_probed, "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
                if (now - when).total_seconds() > stale_cutoff:
                    stats.stale_probe += 1
            except ValueError:
                # Malformed timestamp — treat as never probed so the
                # update pass re-covers it.
                stats.never_probed += 1

        remote = entry.get("remote_chapter_count")
        local = entry.get("chapter_count") or 0
        if remote is not None and int(remote) > int(local):
            stats.pending_updates += 1

        # Track the extrema of last_checked for "when was this library
        # last touched by a scan" at a glance.
        last_checked = entry.get("last_checked")
        if last_checked:
            if stats.newest_checked is None or last_checked > stats.newest_checked:
                stats.newest_checked = last_checked
            if stats.oldest_checked is None or last_checked < stats.oldest_checked:
                stats.oldest_checked = last_checked

    stats.untrackable_files = len(index.untrackable_in(root))
    stats.top_fandoms = fandom_counts.most_common(_TOP_FANDOMS)
    return stats
