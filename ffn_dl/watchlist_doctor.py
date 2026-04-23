"""Watchlist integrity check and self-heal.

The watchlist accumulates entries over months or years of use. Sites
drop supported status (rarely, but it happens), users paste URLs the
scraper doesn't recognise yet, and the JSON file's edit-by-hand
affordance means an entry can end up with garbage in ``type`` or
``site``. None of those cause a crash on load — the store quarantines
unreadable files but accepts individually-malformed entries — and
none of them surface at poll time beyond a quiet "scraper not
found" in the log that's easy to miss on a 200-entry watchlist.

This module produces a structured report of what's wrong, and an
opt-in heal that drops unrepairable entries. Mirrors the shape of
:mod:`ffn_dl.library.doctor` so anyone who's used the library
version can read this without friction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import sites
from .watchlist import (
    SEARCH_SUPPORTED_SITES,
    VALID_WATCH_TYPES,
    WATCH_TYPE_AUTHOR,
    WATCH_TYPE_SEARCH,
    WATCH_TYPE_STORY,
    Watch,
    WatchlistStore,
)


@dataclass
class WatchlistReport:
    """Drift report for a single :class:`WatchlistStore`."""

    invalid_type: list[Watch] = field(default_factory=list)
    """Entries whose ``type`` isn't one of :data:`VALID_WATCH_TYPES`.
    These never poll — the runner's ``if t == "story" elif ...``
    dispatch simply skips them."""

    empty_target: list[Watch] = field(default_factory=list)
    """Story / author watches with an empty ``target`` URL. The
    scraper lookup is a no-op; these waste poll cycles on the store
    but never produce a result."""

    unsupported_site: list[Watch] = field(default_factory=list)
    """Search watches whose ``site`` isn't in
    :data:`SEARCH_SUPPORTED_SITES`, so the search dispatcher has no
    function to call. Also, any watch whose ``site`` doesn't match
    any supported scraper — a holdover from a site that was once
    supported and has since been removed, or a typo in a
    hand-written entry."""

    unresolvable_url: list[Watch] = field(default_factory=list)
    """Story / author watches whose URL no scraper recognises. The
    user probably pasted the wrong tab; nothing the doctor can fix
    without a new URL."""

    duplicates: list[tuple[Watch, Watch]] = field(default_factory=list)
    """``(kept, duplicate)`` pairs — the duplicate would be removed
    by a heal. Two watches are considered duplicates when they have
    the same ``type`` and the same ``target`` URL after whitespace
    trimming. Search watches dedupe on ``(site, query, filters)``
    via a canonical key since the ``target`` is display-only."""

    def is_clean(self) -> bool:
        return not (
            self.invalid_type
            or self.empty_target
            or self.unsupported_site
            or self.unresolvable_url
            or self.duplicates
        )

    def summary(self) -> str:
        if self.is_clean():
            return "Watchlist is clean."
        lines = ["Watchlist:"]
        if self.invalid_type:
            lines.append(
                f"  • {len(self.invalid_type)} entr"
                f"{'y' if len(self.invalid_type) == 1 else 'ies'} "
                "with an invalid ``type``."
            )
        if self.empty_target:
            lines.append(
                f"  • {len(self.empty_target)} entr"
                f"{'y' if len(self.empty_target) == 1 else 'ies'} "
                "with an empty target URL."
            )
        if self.unsupported_site:
            lines.append(
                f"  • {len(self.unsupported_site)} entr"
                f"{'y' if len(self.unsupported_site) == 1 else 'ies'} "
                "target an unsupported site."
            )
        if self.unresolvable_url:
            lines.append(
                f"  • {len(self.unresolvable_url)} entr"
                f"{'y' if len(self.unresolvable_url) == 1 else 'ies'} "
                "have a URL no scraper recognises."
            )
        if self.duplicates:
            lines.append(
                f"  • {len(self.duplicates)} duplicate entr"
                f"{'y' if len(self.duplicates) == 1 else 'ies'}."
            )
        return "\n".join(lines)


@dataclass
class WatchlistHealResult:
    removed: int = 0

    def summary(self) -> str:
        if not self.removed:
            return "No watchlist changes."
        return f"Removed {self.removed} watchlist entr{'y' if self.removed == 1 else 'ies'}."


# ── Inspection ────────────────────────────────────────────────────

def check_watchlist(store: WatchlistStore) -> WatchlistReport:
    """Run every check against the in-memory watches of ``store``.

    Never mutates the store — the caller reviews the report and
    decides whether to call :func:`heal_watchlist`. Some categories
    overlap (a watch can be both ``empty_target`` and
    ``unresolvable_url``); each appears in every category it matches
    so the summary surfaces the full picture even if the user only
    wants to act on one.
    """
    report = WatchlistReport()
    supported_scraper_sites = _supported_scraper_site_keys()
    seen_keys: dict[tuple[str, str], Watch] = {}

    for watch in store.all():
        if watch.type not in VALID_WATCH_TYPES:
            report.invalid_type.append(watch)
            # No point running further checks on a watch that'll never
            # be dispatched anyway.
            continue

        if watch.type == WATCH_TYPE_SEARCH:
            if watch.site not in SEARCH_SUPPORTED_SITES:
                report.unsupported_site.append(watch)
        else:
            target = (watch.target or "").strip()
            if not target:
                report.empty_target.append(watch)
                continue
            if _scraper_for_url(target) is None:
                report.unresolvable_url.append(watch)
            if (
                watch.site
                and watch.site not in supported_scraper_sites
            ):
                report.unsupported_site.append(watch)

        # Duplicate detection — after the type gate so we don't pair
        # two invalid-type watches as "duplicates".
        key = _dedupe_key(watch)
        if key is not None:
            existing = seen_keys.get(key)
            if existing is not None:
                report.duplicates.append((existing, watch))
            else:
                seen_keys[key] = watch

    return report


# ── Mutation ──────────────────────────────────────────────────────

def heal_watchlist(
    store: WatchlistStore,
    report: WatchlistReport,
    *,
    drop_invalid_type: bool = False,
    drop_empty_target: bool = False,
    drop_unsupported_site: bool = False,
    drop_unresolvable_url: bool = False,
    drop_duplicates: bool = False,
) -> WatchlistHealResult:
    """Remove the categories the caller opts into.

    Each flag defaults False — removal is never automatic. The store
    persists after each removal (``WatchlistStore.remove`` auto-saves),
    so a partial heal interrupted mid-run leaves a consistent file on
    disk. Unlike the library doctor, there's no batching API here:
    the watchlist is small enough that per-entry saves don't matter.
    """
    result = WatchlistHealResult()
    doomed_ids: set[str] = set()

    if drop_invalid_type:
        for w in report.invalid_type:
            doomed_ids.add(w.id)
    if drop_empty_target:
        for w in report.empty_target:
            doomed_ids.add(w.id)
    if drop_unsupported_site:
        for w in report.unsupported_site:
            doomed_ids.add(w.id)
    if drop_unresolvable_url:
        for w in report.unresolvable_url:
            doomed_ids.add(w.id)
    if drop_duplicates:
        for _kept, dup in report.duplicates:
            doomed_ids.add(dup.id)

    if not doomed_ids:
        return result

    # Remove by id via the store's public API so any cached indices
    # stay consistent. ``remove`` returns True on success; we count
    # the survivors.
    for wid in list(doomed_ids):
        if store.remove(wid):
            result.removed += 1
    return result


# ── Helpers ───────────────────────────────────────────────────────

def _supported_scraper_site_keys() -> set[str]:
    """Every lowercase site key a watch's ``site`` field could
    legitimately hold. Built from the live ``sites.ALL_SCRAPERS``
    list so a new scraper is picked up automatically."""
    keys = set()
    for cls in getattr(sites, "ALL_SCRAPERS", []):
        site_name = getattr(cls, "site_name", None)
        if site_name:
            keys.add(str(site_name).lower())
    return keys


def _scraper_for_url(url: str):
    """Try every registered scraper's URL parser. Return the first
    that accepts, or ``None``."""
    for cls in getattr(sites, "ALL_SCRAPERS", []):
        try:
            cls.parse_story_id(url)
        except Exception:
            continue
        return cls
    # Author URLs are handled by a different staticmethod; fall through
    # to that path for completeness.
    for cls in getattr(sites, "ALL_SCRAPERS", []):
        try:
            if cls.is_author_url(url):
                return cls
        except Exception:
            continue
    return None


def _dedupe_key(watch: Watch) -> tuple[str, str] | None:
    """Canonical (type, identity) tuple for dedupe.

    Story / author watches key on the trimmed URL. Search watches key
    on a ``site|query|filters`` string so two saved searches with the
    same filter payload collapse even if the user typed slightly
    different display labels.
    """
    if watch.type in (WATCH_TYPE_STORY, WATCH_TYPE_AUTHOR):
        target = (watch.target or "").strip()
        if not target:
            return None
        return (watch.type, target)
    if watch.type == WATCH_TYPE_SEARCH:
        import json
        filters_blob = json.dumps(
            watch.filters or {}, sort_keys=True,
        )
        return (
            WATCH_TYPE_SEARCH,
            f"{watch.site}|{watch.query}|{filters_blob}",
        )
    return None
