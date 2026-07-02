"""Scraper-cache doctor — report and prune stale cache entries.

The per-story chapter cache lives at ``~/.cache/ficary/<site>_<id>/``
and accumulates forever unless something explicitly cleans it. Over
time a library manager run from a few years' worth of downloads
builds up thousands of entries — including caches for stories that
have since been deleted from the library, renamed upstream, or that
were one-offs the user doesn't want anymore.

This module walks the cache dir and produces a report: total size,
per-site distribution, largest-N entries, and (optionally) entries
that don't appear in any known library index. The CLI wires this up
as ``--cache-doctor``; pass ``--prune`` to remove orphans.

The module never touches the library index or the files on disk in
the library — it's strictly a cache-hygiene tool. The per-run cache
corruption path in :class:`~ficary.scraper.BaseScraper` already
self-heals by deleting a bad cache entry on read; this doctor covers
the slower leak: orphans from deleted / renamed / dropped stories
that no read ever triggers.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .library.index import LibraryIndex


@dataclass
class CacheReport:
    """Snapshot of the scraper cache on disk for one cache root."""

    cache_root: Path
    total_entries: int = 0
    total_bytes: int = 0
    by_site: Counter[str] = field(default_factory=Counter)
    bytes_by_site: Counter[str] = field(default_factory=Counter)
    largest: list[tuple[Path, int]] = field(default_factory=list)

    orphan_entries: list[Path] = field(default_factory=list)
    """Entries whose ``<site>_<id>`` key doesn't match any story in any
    known library index. Only populated when ``check_cache`` is called
    with an index — otherwise an empty list, which shouldn't be read
    as "no orphans" but "didn't check"."""

    def summary(self) -> str:
        """Multi-line human-readable summary."""
        lines = [
            f"Scraper cache at {self.cache_root}",
            f"  Entries:  {self.total_entries}",
            f"  Size:     {_format_bytes(self.total_bytes)}",
        ]
        if self.by_site:
            lines.append("  By site:")
            for site, count in sorted(
                self.by_site.items(),
                key=lambda kv: (-self.bytes_by_site[kv[0]], kv[0]),
            ):
                size = _format_bytes(self.bytes_by_site[site])
                lines.append(
                    f"    {site:<14} {count:>5}  ({size})"
                )
        if self.largest:
            lines.append("  Largest entries:")
            for path, size in self.largest:
                lines.append(
                    f"    {_format_bytes(size):>9}  {path.name}"
                )
        if self.orphan_entries:
            lines.append(
                f"  Orphan entries (not in any library index): "
                f"{len(self.orphan_entries)}"
            )
        return "\n".join(lines)


_LARGEST_N = 10
"""How many largest-cache-entries to surface in the report. The top
ten catches the handful of outlier stories that dominate disk use
(usually 500-chapter RR fictions) without turning the summary into a
wall."""


def default_cache_root() -> Path:
    """Resolve the scraper cache root the same way
    :func:`~ficary.scraper._default_cache_dir` does at runtime, so
    the doctor always reports on the cache the scrapers actually use."""
    try:
        from . import portable
        if portable.is_frozen():
            return portable.cache_dir()
    except Exception:
        pass
    return Path.home() / ".cache" / "ficary"


def check_cache(
    cache_root: Path | None = None,
    index: LibraryIndex | None = None,
) -> CacheReport:
    """Walk the cache and summarise it.

    When ``index`` is provided, any ``<site>_<id>`` directory whose
    ``<site>/<id>`` pair doesn't appear in the tracked URLs of any
    library is flagged as an orphan. The site match key is derived
    from the URL's ``adapter`` (FFN → "ffn", AO3 → "ao3", …) so the
    caches' directory prefix matches naturally.
    """
    cache_root = Path(cache_root) if cache_root else default_cache_root()
    report = CacheReport(cache_root=cache_root)
    if not cache_root.exists():
        return report

    tracked_keys = (
        _tracked_cache_keys(index) if index is not None else None
    )

    entries_with_size: list[tuple[Path, int]] = []
    for entry in cache_root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        site = _site_prefix(name)
        if site is None:
            continue  # ignore foreign directories (covers/, logs/…)

        size = _dir_size(entry)
        entries_with_size.append((entry, size))
        report.total_entries += 1
        report.total_bytes += size
        report.by_site[site] += 1
        report.bytes_by_site[site] += size

        # ``tracked_keys`` is an empty set (not None) when the index
        # loaded zero stories — a moved, quarantined, or fresh index.
        # In that state EVERY cache dir looks orphaned, and heal_all
        # would prune the whole cache (hours of re-scrape at FFN's
        # rate-limit floor). Treat "no known stories" as "can't
        # determine orphans" and flag nothing.
        if tracked_keys and name not in tracked_keys:
            report.orphan_entries.append(entry)

    entries_with_size.sort(key=lambda t: t[1], reverse=True)
    report.largest = entries_with_size[:_LARGEST_N]
    return report


@dataclass
class PruneResult:
    pruned: int = 0
    bytes_freed: int = 0
    quarantine_dir: Path | None = None

    def summary(self) -> str:
        if not self.pruned:
            return "Nothing pruned."
        where = (
            f"; recoverable from {self.quarantine_dir} until the next sweep"
            if self.quarantine_dir else ""
        )
        return (
            f"Pruned {self.pruned} cache entr"
            f"{'y' if self.pruned == 1 else 'ies'} "
            f"({_format_bytes(self.bytes_freed)}{where})."
        )


_TRASH_DIRNAME = ".trash"
_TRASH_MAX_AGE_DAYS = 14


def prune(report: CacheReport) -> PruneResult:
    """Quarantine every cache directory flagged as an orphan in
    ``report``.

    Orphans are MOVED into ``<cache_root>/.trash/<stamp>/`` (same
    filesystem, so the rename is atomic) rather than deleted — the
    rounds-8/9 audits both found doctor paths destroying data on a
    misdiagnosis, and re-acquiring a pruned chapter cache costs hours
    at FFN's rate-limit floor. Quarantine batches older than
    ``_TRASH_MAX_AGE_DAYS`` are deleted on the next prune, so disk is
    eventually reclaimed. Safe to call with an empty orphan list.
    """
    import shutil
    import time
    result = PruneResult()
    if not report.orphan_entries:
        return result
    cache_root = report.orphan_entries[0].parent
    _sweep_old_trash(cache_root / _TRASH_DIRNAME)
    batch = cache_root / _TRASH_DIRNAME / time.strftime("%Y%m%d-%H%M%S")
    for path in report.orphan_entries:
        size = _dir_size(path)
        try:
            batch.mkdir(parents=True, exist_ok=True)
            os.replace(path, batch / path.name)
        except OSError:
            # Cross-device or locked — fall back to outright removal so
            # the orphan doesn't survive the prune it was flagged for.
            try:
                shutil.rmtree(path)
            except OSError:
                continue
        result.pruned += 1
        result.bytes_freed += size
    if batch.is_dir():
        result.quarantine_dir = batch
    return result


def _sweep_old_trash(trash_root: Path) -> None:
    import shutil
    import time
    if not trash_root.is_dir():
        return
    cutoff = time.time() - _TRASH_MAX_AGE_DAYS * 24 * 3600
    for batch in trash_root.iterdir():
        try:
            if batch.is_dir() and batch.stat().st_mtime < cutoff:
                shutil.rmtree(batch, ignore_errors=True)
        except OSError:
            continue


# ── Helpers ───────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    """Recursive byte count for ``path``. Skips symlinks and files the
    process can't stat — both are possible on weirdly-permissioned
    cache dirs but don't merit aborting the whole run."""
    total = 0
    for sub in path.rglob("*"):
        if sub.is_symlink():
            continue
        try:
            if sub.is_file():
                total += sub.stat().st_size
        except OSError:
            continue
    return total


_NON_STORY_CACHE_PREFIXES = frozenset({
    # Top-level subdirs of the cache root that aren't per-story caches.
    # Anything named exactly one of these — or, for ``chyoa_node``,
    # named ``chyoa_node_<id>`` — is excluded from the orphan check
    # because there's no library-index entry to match it against.
    # ``cf-cookies`` has a hyphen so ``_site_prefix`` wouldn't return
    # a matching string anyway, but listing it keeps the intent
    # readable and protects against future renames.
    "covers",
    "huggingface",
    "llm_an",
    "cf-cookies",
    # Quarantined prune batches — never re-reported as orphans.
    ".trash",
})

_NON_STORY_CACHE_NAME_PREFIXES = (
    # ``<site>_node_<id>`` — chyoa's per-node cache layer. These
    # outlive any single download (cross-tree branches share them)
    # and aren't tied to a library-index URL. ``_site_prefix`` would
    # otherwise return the bare site name and the orphan match would
    # never line up.
    "chyoa_node_",
)


def _site_prefix(entry_name: str) -> str | None:
    """The cache naming convention is ``<site>_<story_id>``. Extract
    the site prefix, or None if the directory doesn't match the
    expected pattern (e.g. ``covers/`` or user-created scratch dirs)."""
    if entry_name in _NON_STORY_CACHE_PREFIXES:
        return None
    for prefix in _NON_STORY_CACHE_NAME_PREFIXES:
        if entry_name.startswith(prefix):
            return None
    if "_" not in entry_name:
        return None
    return entry_name.split("_", 1)[0]


def _tracked_cache_keys(index: LibraryIndex) -> set[str]:
    """Every cache-dir name (``<site>_<id>``) referenced by any story
    in any library root of ``index``.

    We derive the name from each story's ``adapter`` plus the
    *cache key* the scraper actually wrote — which is what
    :meth:`~ficary.scraper.BaseScraper.cache_key_for_url` returns.
    For most sites that's the same as ``parse_story_id`` (an int);
    for Chyoa, Literotica, MCStories, Lushstories, and Nifty it's a
    hash of the slug/path because those scrapers can't use the
    parsed identifier as a directory name (tuples, slashes, mixed
    case). Without ``cache_key_for_url`` the orphan match would
    silently flag every cache entry on those sites for deletion.
    """
    keys: set[str] = set()
    for root in index.library_roots():
        for url, entry in index.stories_in(Path(root)):
            adapter = entry.get("adapter")
            if not adapter:
                continue
            scraper_cls = _scraper_for_adapter(adapter)
            if scraper_cls is None:
                continue
            try:
                sid = scraper_cls.cache_key_for_url(url)
            except Exception:
                continue
            keys.add(f"{scraper_cls.site_name}_{sid}")
    return keys


def _scraper_for_adapter(adapter: str):
    """Resolve an ``adapter`` string back to its scraper class.

    Imported lazily so ``cache_doctor`` can be loaded in contexts
    that don't want to pay the cost of every scraper's dependencies
    (curl_cffi, lxml) up front — e.g. a fast CLI ``--version`` call.
    """
    try:
        from . import sites
    except Exception:
        return None
    for cls in getattr(sites, "ALL_SCRAPERS", []):
        if getattr(cls, "site_name", None) == adapter:
            return cls
    return None


def _format_bytes(n: int) -> str:
    """Short human-readable size. 1.5 KB / 4.2 MB / 1.1 GB."""
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n_f = n / 1024
        if n_f < 1024 or unit == "TB":
            return f"{n_f:.1f} {unit}"
        n = n_f  # shift down
    return f"{n} B"
