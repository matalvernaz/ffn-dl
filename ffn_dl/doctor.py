"""Integrated health check across every hygiene surface.

The individual doctors (library, watchlist, cache) each report on
one surface. Power users with big libraries want a single "is
everything OK?" pass. This module orchestrates all three:

* One :func:`check_all` call produces a :class:`FullReport` covering
  every known library root + the watchlist + the scraper cache.
* :func:`heal_all` applies the same opt-in heal across each surface,
  so the integrated CLI flag can be a single ``--doctor --heal``
  instead of asking the user to run each sub-doctor separately.

The unified report is intentionally human-oriented — each surface's
own doctor already exposes structured types for programmatic callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .cache_doctor import CacheReport, check_cache, prune as prune_cache
from .library import HealReport as LibraryHealReport
from .library import IntegrityReport as LibraryIntegrityReport
from .library import check_integrity, heal as heal_library
from .library.backup import backup as backup_index
from .library.index import LibraryIndex
from .watchlist import WatchlistStore
from .watchlist_doctor import (
    WatchlistHealResult,
    WatchlistReport,
    check_watchlist,
    heal_watchlist,
)


@dataclass
class FullReport:
    """Aggregate health report across every hygiene surface.

    ``library_reports`` maps each library root to its integrity
    report — a library may contribute zero reports if no roots are
    known to the index yet."""

    library_reports: dict[Path, LibraryIntegrityReport] = field(default_factory=dict)
    watchlist_report: WatchlistReport | None = None
    cache_report: CacheReport | None = None

    def is_clean(self) -> bool:
        return (
            all(r.is_clean() for r in self.library_reports.values())
            and (
                self.watchlist_report is None
                or self.watchlist_report.is_clean()
            )
            and (
                self.cache_report is None
                or not self.cache_report.orphan_entries
            )
        )

    def summary(self) -> str:
        """Multi-section human-readable render.

        Each section has its own doctor's :meth:`summary` output so
        the messaging stays consistent with individual-doctor runs.
        An empty section (no libraries known, watchlist file missing)
        is reported once as "Not applicable" rather than a stack of
        zeros.
        """
        sections: list[str] = []
        if self.library_reports:
            sections.append("== Library ==")
            for _root, rep in self.library_reports.items():
                sections.append(rep.summary())
        else:
            sections.append("== Library ==")
            sections.append(
                "  Not applicable (no library roots in the index)."
            )

        sections.append("== Watchlist ==")
        if self.watchlist_report is None:
            sections.append(
                "  Not applicable (no watchlist file)."
            )
        else:
            sections.append(self.watchlist_report.summary())

        sections.append("== Scraper cache ==")
        if self.cache_report is None:
            sections.append(
                "  Not applicable (cache directory missing)."
            )
        else:
            sections.append(self.cache_report.summary())

        return "\n".join(sections)


@dataclass
class FullHealResult:
    """What the integrated heal actually changed."""

    library_heals: dict[Path, LibraryHealReport] = field(default_factory=dict)
    watchlist_heal: WatchlistHealResult | None = None
    cache_pruned: int = 0
    cache_bytes_freed: int = 0
    index_backups: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        """One-paragraph summary of the cross-surface heal.

        Collapses zero-change surfaces into a single "no changes"
        line so a mostly-clean system doesn't produce a page of
        "removed 0" noise."""
        lines: list[str] = []
        for root, hr in self.library_heals.items():
            s = hr.summary()
            if s != "No changes.":
                lines.append(f"Library {root}: {s.removeprefix('Healed: ').rstrip('.')}")
        if self.watchlist_heal is not None and self.watchlist_heal.removed:
            lines.append(
                f"Watchlist: removed {self.watchlist_heal.removed} entr"
                f"{'y' if self.watchlist_heal.removed == 1 else 'ies'}."
            )
        if self.cache_pruned:
            from .cache_doctor import _format_bytes
            lines.append(
                f"Scraper cache: pruned {self.cache_pruned} "
                f"orphan entr{'y' if self.cache_pruned == 1 else 'ies'} "
                f"({_format_bytes(self.cache_bytes_freed)})."
            )
        if self.index_backups:
            lines.append(
                f"Library index backed up to "
                f"{len(self.index_backups)} snapshot(s) before heal."
            )
        if not lines:
            return "No changes."
        return "\n".join(lines)


# ── Inspection ────────────────────────────────────────────────────

def check_all(
    *,
    index: LibraryIndex | None = None,
    watchlist: WatchlistStore | None = None,
    cache_root: Path | None = None,
) -> FullReport:
    """Run every doctor without mutating anything.

    ``index`` / ``watchlist`` default to :meth:`LibraryIndex.load` and
    :meth:`WatchlistStore.load_default` — the CLI passes None. Tests
    inject their own so they don't touch the real on-disk state.
    ``cache_root`` likewise defaults to the live cache path; tests
    point it at a tmpdir so they don't trip over the developer's
    actual ``~/.cache/ffn-dl``.
    """
    if index is None:
        index = LibraryIndex.load()
    if watchlist is None:
        try:
            watchlist = WatchlistStore.load_default()
        except Exception:
            watchlist = None

    report = FullReport()
    for root_str in index.library_roots():
        root = Path(root_str)
        report.library_reports[root] = check_integrity(root, index)

    if watchlist is not None:
        report.watchlist_report = check_watchlist(watchlist)

    # Cache check is index-aware so orphans are flagged when a library
    # is known. Missing cache dir reports an empty tally rather than
    # raising.
    report.cache_report = check_cache(cache_root=cache_root, index=index)
    return report


# ── Mutation ──────────────────────────────────────────────────────

def heal_all(
    report: FullReport,
    *,
    index: LibraryIndex | None = None,
    watchlist: WatchlistStore | None = None,
    auto_backup: bool = True,
) -> FullHealResult:
    """Apply a safe cross-surface heal.

    "Safe" here means: every sub-doctor's full set of heal flags is
    enabled, because the individual sub-doctor CLI entry points
    already gate the destructive ones behind ``--heal``. By the time
    the caller opts into :func:`heal_all`, they've already committed
    to the full repair.

    A pre-heal backup of the library index is taken by default so a
    misdiagnosed heal can be rolled back with ``--restore-index``.
    """
    if index is None:
        index = LibraryIndex.load()
    if watchlist is None:
        try:
            watchlist = WatchlistStore.load_default()
        except Exception:
            watchlist = None

    result = FullHealResult()

    # Back up the index before any library mutation touches it. The
    # backup is cheap (tiny file) and silent.
    if auto_backup:
        bkp = backup_index(index.path)
        if bkp is not None:
            result.index_backups.append(bkp)

    for root, lib_report in report.library_reports.items():
        if lib_report.is_clean():
            continue
        result.library_heals[root] = heal_library(
            root, index, lib_report,
            drop_missing=True,
            refresh_drift=True,
            prune_untrackable=True,
            prune_duplicates=True,
            scan_orphans=True,
        )

    # If any library heal actually did something, persist.
    if any(
        (hr.removed_missing
         or hr.refreshed_drift
         or hr.removed_stale_untrackable
         or hr.removed_stale_duplicates
         or hr.scanned_orphans)
        for hr in result.library_heals.values()
    ):
        index.save()

    if watchlist is not None and report.watchlist_report is not None:
        result.watchlist_heal = heal_watchlist(
            watchlist, report.watchlist_report,
            drop_invalid_type=True,
            drop_empty_target=True,
            drop_unsupported_site=True,
            drop_unresolvable_url=True,
            drop_duplicates=True,
        )

    if report.cache_report is not None and report.cache_report.orphan_entries:
        prune_result = prune_cache(report.cache_report)
        result.cache_pruned = prune_result.pruned
        result.cache_bytes_freed = prune_result.bytes_freed

    return result
