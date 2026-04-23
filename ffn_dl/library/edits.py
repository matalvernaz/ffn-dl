"""Silent-edit detection across a library.

A "silent edit" is an author revision that changes chapter content
without changing the chapter count — the update path's count-based
check can't see it, so the local copy drifts from canon. Given
stored hashes from the bootstrap path, this module compares them
against a fresh download's hashes and reports which stories (and
which chapters within each) have drifted.

Two passes are exposed:

* :func:`bootstrap_hashes` walks the index, hashes the local files,
  writes the hashes back. Read-only against the network; the cost
  is a per-file parse pass. Intended to be run once to seed every
  existing library, then occasionally to catch up newly-added
  stories whose hashes weren't populated at download time.
* :func:`scan_edits` probes the remote for each story, hashes the
  fresh download, and reports drift. Read-only against the library
  (no file writes); applying fixes is a separate step (``--refetch``
  on the reported stories).

Both return structured reports so the CLI can render them and a GUI
could present the same data in a table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..content_hash import diff_hashes, story_chapter_hashes
from ..scraper import BaseScraper, StoryNotFoundError
from ..sites import detect_scraper
from .hashes import ChapterHashUnavailable, compute_local_hashes, stored_hashes
from .index import LibraryIndex


# ── Bootstrap ─────────────────────────────────────────────────────

@dataclass
class BootstrapReport:
    """Outcome of a ``--populate-hashes`` run."""

    populated: list[str] = field(default_factory=list)
    """URLs that were successfully hashed and written."""

    already_hashed: list[str] = field(default_factory=list)
    """URLs whose entries already had a stored hash list — skipped
    without re-parsing unless the caller asked to force a refresh."""

    skipped_missing_file: list[str] = field(default_factory=list)
    skipped_unreadable: list[tuple[str, str]] = field(default_factory=list)
    """``(url, reason)`` pairs where parsing failed — TXT files or
    corrupt exports. Silent-edit detection skips these stories
    because there's no local baseline to compare against."""

    def summary(self) -> str:
        lines = [
            f"Populated hashes for {len(self.populated)} stor"
            f"{'y' if len(self.populated) == 1 else 'ies'}."
        ]
        if self.already_hashed:
            lines.append(
                f"  {len(self.already_hashed)} already had hashes "
                "(skipped)."
            )
        if self.skipped_missing_file:
            lines.append(
                f"  {len(self.skipped_missing_file)} missing on disk "
                "(skipped)."
            )
        if self.skipped_unreadable:
            lines.append(
                f"  {len(self.skipped_unreadable)} couldn't be parsed "
                "(skipped — TXT exports or corrupt files)."
            )
        return "\n".join(lines)


def bootstrap_hashes(
    root: Path,
    index: LibraryIndex,
    *,
    force: bool = False,
) -> BootstrapReport:
    """Walk every indexed story under ``root`` and populate hashes
    from the local file.

    Does not save the index — the caller batches that call so a
    KeyboardInterrupt mid-bootstrap doesn't leave the file
    half-updated. ``force=True`` re-hashes entries that already
    have a stored list; default False lets a repeat run cheaply
    cover newly-downloaded stories.
    """
    from .hashes import store_hashes

    report = BootstrapReport()
    root_resolved = Path(root).expanduser().resolve()

    for url, entry in list(index.stories_in(root_resolved)):
        if not force and stored_hashes(entry) is not None:
            report.already_hashed.append(url)
            continue

        rel = entry.get("relpath")
        if not rel:
            report.skipped_missing_file.append(url)
            continue
        file_path = root_resolved / rel
        if not file_path.exists():
            report.skipped_missing_file.append(url)
            continue

        try:
            hashes = compute_local_hashes(file_path)
        except ChapterHashUnavailable as exc:
            report.skipped_unreadable.append((url, str(exc)))
            continue

        if store_hashes(index, root_resolved, url, hashes):
            report.populated.append(url)

    return report


# ── Scan ──────────────────────────────────────────────────────────

@dataclass
class SilentEdit:
    """One story whose content has drifted under an unchanged
    chapter count."""

    url: str
    relpath: str
    changed_chapters: list[int]


@dataclass
class CountChange:
    """One story whose chapter count no longer matches what's stored.

    Reported separately from silent edits because the standard
    ``--update-library`` count-based check already handles these —
    they're surfaced here so the user sees the full picture of
    what's out of sync on one report rather than across two runs.
    """

    url: str
    relpath: str
    local_count: int
    remote_count: int


@dataclass
class ScanReport:
    root: Path
    scanned: int = 0
    unchanged: int = 0
    silent_edits: list[SilentEdit] = field(default_factory=list)
    count_changes: list[CountChange] = field(default_factory=list)
    skipped_no_baseline: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not (self.silent_edits or self.count_changes)

    def summary(self) -> str:
        lines = [
            f"Scanned {self.scanned} stor"
            f"{'y' if self.scanned == 1 else 'ies'} under {self.root}:",
            f"  Unchanged:     {self.unchanged}",
        ]
        if self.silent_edits:
            lines.append(
                f"  Silent edits:  {len(self.silent_edits)}"
            )
            for edit in self.silent_edits:
                chap_summary = (
                    ", ".join(str(n) for n in edit.changed_chapters[:5])
                    + (f" (+{len(edit.changed_chapters) - 5} more)"
                       if len(edit.changed_chapters) > 5 else "")
                )
                lines.append(
                    f"    {edit.relpath}"
                )
                lines.append(
                    f"      chapters changed: {chap_summary}"
                )
                lines.append(f"      {edit.url}")
        if self.count_changes:
            lines.append(
                f"  Count changes: {len(self.count_changes)} "
                "(handled by --update-library)"
            )
        if self.skipped_no_baseline:
            lines.append(
                f"  Skipped (no baseline hashes): "
                f"{len(self.skipped_no_baseline)}  "
                "— run --populate-hashes first."
            )
        if self.errors:
            lines.append(
                f"  Probe errors:  {len(self.errors)}"
            )
        return "\n".join(lines)


# Scrapers are expensive to spin up (curl_cffi session with
# browser impersonation). Cache one per site key for the duration
# of a scan so 700 stories don't produce 700 session handshakes.
_ScraperCache = dict[str, BaseScraper]


def _scraper_for_url(url: str, cache: _ScraperCache) -> BaseScraper | None:
    """Return a scraper instance for ``url``, reusing an existing
    one from ``cache`` when available."""
    cls = detect_scraper(url)
    key = getattr(cls, "site_name", cls.__name__)
    scraper = cache.get(key)
    if scraper is None:
        scraper = cls()
        cache[key] = scraper
    return scraper


def scan_edits(
    root: Path,
    index: LibraryIndex,
    *,
    progress: object = None,
    scraper_cache: _ScraperCache | None = None,
) -> ScanReport:
    """Probe every story with stored hashes and report drift.

    ``progress`` is a callable ``(n, total, url)`` invoked before each
    fetch — lets the CLI show "[3/200] https://..." while waiting on
    slow remote responses. ``None`` disables progress reporting.

    Expensive: each story triggers a full download from upstream
    (the only way to get fresh chapter bodies). Rate-limit handling
    rides on the scrapers' existing AIMD machinery, so running this
    across a big library is slow but safe — there's no parallel
    fetch since site-level rate limits would eat any speedup anyway.
    """
    if scraper_cache is None:
        scraper_cache = {}

    root_resolved = Path(root).expanduser().resolve()
    entries = list(index.stories_in(root_resolved))
    report = ScanReport(root=root_resolved)

    total = len(entries)
    for i, (url, entry) in enumerate(entries, 1):
        if progress is not None:
            try:
                progress(i, total, url)
            except Exception:
                # Progress callback failures must never abort the scan.
                pass

        baseline = stored_hashes(entry)
        if baseline is None:
            report.skipped_no_baseline.append(url)
            continue

        scraper = _scraper_for_url(url, scraper_cache)
        if scraper is None:
            report.errors.append((url, "no scraper for URL"))
            continue

        try:
            story = scraper.download(url)
        except StoryNotFoundError:
            # Story removed upstream — treat as a count change with
            # remote=0 so it surfaces in the "needs attention" column.
            report.count_changes.append(CountChange(
                url=url,
                relpath=entry.get("relpath") or "",
                local_count=int(entry.get("chapter_count") or 0),
                remote_count=0,
            ))
            report.scanned += 1
            continue
        except Exception as exc:  # noqa: BLE001
            report.errors.append((url, f"{type(exc).__name__}: {exc}"))
            continue

        fresh = story_chapter_hashes(story)
        report.scanned += 1

        if len(fresh) != len(baseline):
            report.count_changes.append(CountChange(
                url=url,
                relpath=entry.get("relpath") or "",
                local_count=len(baseline),
                remote_count=len(fresh),
            ))
            continue

        changed = diff_hashes(baseline, fresh)
        if changed:
            report.silent_edits.append(SilentEdit(
                url=url,
                relpath=entry.get("relpath") or "",
                changed_chapters=changed,
            ))
        else:
            report.unchanged += 1

    return report
