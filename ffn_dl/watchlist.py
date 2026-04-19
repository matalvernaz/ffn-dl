"""Fanfiction watchlist with notifications.

Stores a list of things the user wants pushed to them when they change:

* **Story watches** — new chapter alerts on a specific work (uses each
  scraper's cheap :py:meth:`BaseScraper.get_chapter_count` probe).
* **Author watches** — new-work alerts for a specific author (uses
  :py:meth:`BaseScraper.scrape_author_works` on every supported site).
* **Search watches** — new-match alerts for a saved query on a specific
  site (uses the site-specific ``search.search_<site>`` function).

All three share the same polling loop, the same storage, and the same
notification dispatcher in :mod:`ffn_dl.notifications`.

Storage is a JSON file at ``<portable_root>/watchlist.json`` so the
user's entries survive auto-updates (the release zip contains only the
exe + ``_internal/`` and doesn't overwrite user data). The file is
versioned for forward compatibility; unknown fields on disk are
ignored, missing fields fall back to dataclass defaults, and a
malformed file is quarantined with a ``.corrupt-<timestamp>`` suffix so
the rest of the app keeps running.

The runner never crashes: per-watch scraper/network failures are
captured on the watch itself (``last_error``) and surfaced in the
returned :class:`PollResult` list so the caller — CLI or GUI — can
report them without losing the rest of the poll. Notification
failures (a broken Discord webhook, a Pushover rate-limit) are
likewise captured per-channel by :func:`notifications.dispatch` and
logged, so one misconfigured channel can't silence the others.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import portable, sites
from .notifications import Notification, dispatch as dispatch_notification
from .scraper import BaseScraper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema + limits. Hoisted so nothing below embeds a magic number.
# ---------------------------------------------------------------------------

# On-disk schema version. Bump when the watch dict layout changes in a
# way that can't be read by an older build. Newer files are loaded
# best-effort (with a warning) so a mid-sync downgrade doesn't hard-fail.
SCHEMA_VERSION = 1

# Valid `type` values for a :class:`Watch`. Kept as string constants
# rather than an Enum so the JSON file stays human-editable.
WATCH_TYPE_STORY = "story"
WATCH_TYPE_AUTHOR = "author"
WATCH_TYPE_SEARCH = "search"
VALID_WATCH_TYPES: tuple[str, ...] = (
    WATCH_TYPE_STORY,
    WATCH_TYPE_AUTHOR,
    WATCH_TYPE_SEARCH,
)

# Minimum sensible interval for the GUI's background-poll thread.
# Polling faster than this risks tripping FFN's per-IP captcha on a
# moderately sized watchlist, since FFN stays at concurrency=1 with a
# 6s delay floor.
MIN_POLL_INTERVAL_S = 5 * 60

# How many distinct work URLs a single search watch tracks. Diffing
# against more than this bloats the JSON file without adding value —
# a saved search on AO3 with tens of thousands of hits would dump its
# entire first page as "new" on creation otherwise.
SEARCH_WATCH_RESULT_CAP = 50

# After a notification fires for a given watch, skip further
# notifications from the same watch until this many seconds have
# elapsed. Guards against a transient scraper empty-response flake
# that could otherwise spam the user with "new work!" alerts.
NOTIFICATION_COOLDOWN_S = 10 * 60

# How many new-item titles to include in the notification preview
# before collapsing the rest into "(+N more)".
NOTIFICATION_PREVIEW_LIMIT = 5

# Filename, relative to ``portable_root()``, where the watchlist lives.
WATCHLIST_FILENAME = "watchlist.json"

# Lookup: scraper class name → lowercase site key. The site key is
# what the user sees on the CLI (``--watch-add-search ao3 ...``) and
# what ``search.py`` uses internally. Kept as a simple dict rather
# than poking attributes on the scraper classes so sites.py stays
# free of search-specific metadata.
_SCRAPER_CLASS_TO_SITE_KEY: dict[str, str] = {
    "FFNScraper": "ffn",
    "AO3Scraper": "ao3",
    "RoyalRoadScraper": "royalroad",
    "LiteroticaScraper": "literotica",
    "WattpadScraper": "wattpad",
    "FicWadScraper": "ficwad",
    "MediaMinerScraper": "mediaminer",
}

# Sites whose ``search.search_<key>`` function is implemented. Search
# watches on other sites are rejected at add-time with a clear error.
SEARCH_SUPPORTED_SITES: tuple[str, ...] = (
    "ffn", "ao3", "royalroad", "literotica", "wattpad",
)


# ---------------------------------------------------------------------------
# Small helpers for time handling. ISO-8601 UTC strings are used
# everywhere on disk; epoch floats are used for arithmetic in-memory.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp for the current moment."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_to_epoch(iso: str) -> float:
    """Parse an ISO-8601 string to a Unix epoch float.

    Returns ``0.0`` for empty or unparseable values so callers can
    treat "never" and "invalid" the same way without a try/except.
    """
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Watch:
    """A single watchlist entry.

    Attributes:
        id: UUID4 hex — stable identifier for CLI remove / GUI refs.
        type: One of :data:`VALID_WATCH_TYPES`.
        site: Lowercase site key — set for all watch types; used to
            drive search dispatch and to show a useful label in the GUI.
        target: For author/story watches, the URL being watched. For
            search watches, a freeform display string (the actual
            query lives in ``query``/``filters``).
        label: Optional human-friendly display name. Falls back to
            ``target`` in :meth:`display_label`.
        channels: List of notification channel identifiers (see
            :mod:`ffn_dl.notifications`).
        enabled: False to pause polling without deleting the entry.
        query: Search query string (search watches only).
        filters: Search filter dict (search watches only).
        last_seen: Site-dependent — for story watches this is an int
            chapter count; for author/search watches it's a list of
            work URLs already reported. ``None`` on a freshly-added
            watch so the first poll doesn't spuriously flag every
            existing work as "new".
        last_checked_at: ISO-8601 timestamp of the most recent poll
            attempt, successful or not. Empty string if never polled.
        last_error: Short error message from the last failed poll, or
            empty string if the last poll succeeded.
        cooldown_until: ISO-8601 timestamp; no notifications fire from
            this watch until that moment even if new items are
            detected. Used to suppress spam from scraper flakes.
        created_at: ISO-8601 timestamp when the watch was added.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    type: str = WATCH_TYPE_STORY
    site: str = ""
    target: str = ""
    label: str = ""
    channels: list[str] = field(default_factory=list)
    enabled: bool = True
    query: str = ""
    filters: dict = field(default_factory=dict)
    last_seen: Any = None
    last_checked_at: str = ""
    last_error: str = ""
    cooldown_until: str = ""
    created_at: str = field(default_factory=_now_iso)

    def display_label(self) -> str:
        """Return a name for logs and notifications.

        Prefers the user-supplied label, then the target URL, then a
        generic type-based fallback — guarantees something non-empty.
        """
        return self.label or self.target or f"{self.type} watch"


def _watch_to_dict(watch: Watch) -> dict:
    """Convert a :class:`Watch` to a plain dict for JSON serialisation."""
    return dataclasses.asdict(watch)


def _watch_from_dict(data: dict) -> Watch:
    """Build a :class:`Watch` from a dict, tolerating schema drift.

    Unknown fields on disk are ignored (so a downgraded build doesn't
    choke on fields a future build added); missing fields fall back
    to the dataclass defaults. This keeps the watchlist file format
    forward-compatible without a migration script on every upgrade.
    """
    known = {f.name for f in dataclasses.fields(Watch)}
    clean = {k: v for k, v in data.items() if k in known}
    return Watch(**clean)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class WatchlistStore:
    """JSON-backed persistence for watchlist entries.

    Follows the same corruption-tolerance pattern as the scraper cache
    (see :class:`ffn_dl.scraper.BaseScraper` cache loaders): a
    malformed file is logged, renamed aside with a ``.corrupt-<ts>``
    suffix, and replaced with an empty list so the rest of the app
    keeps working. A hard crash here would block every watch run
    until the user manually cleaned the file up — worse than losing
    the entries, because the user wouldn't know the file was bad.

    Writes are atomic: the new JSON is staged to ``<path>.tmp`` and
    then renamed over the target, so a crash or power cut can never
    leave an empty/half-written watchlist on disk.
    """

    def __init__(self, path: Path):
        self.path = path
        self._watches: list[Watch] = []

    # ---- Construction helpers ------------------------------------------

    @classmethod
    def default_path(cls) -> Path:
        """Return the on-disk path the watchlist lives at by default."""
        return portable.portable_root() / WATCHLIST_FILENAME

    @classmethod
    def load_default(cls) -> "WatchlistStore":
        """Build a store rooted at :meth:`default_path` and load it."""
        store = cls(cls.default_path())
        store.reload()
        return store

    # ---- I/O -----------------------------------------------------------

    def reload(self) -> None:
        """Re-read the watchlist file, quarantining it if it's corrupt."""
        if not self.path.exists():
            self._watches = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "Watchlist file at %s is unreadable (%s); quarantining and "
                "starting with an empty list.",
                self.path, exc,
            )
            self._quarantine_corrupt_file()
            self._watches = []
            return

        if not isinstance(raw, dict):
            logger.warning(
                "Watchlist file at %s has unexpected top-level shape "
                "(expected object); quarantining.",
                self.path,
            )
            self._quarantine_corrupt_file()
            self._watches = []
            return

        version = raw.get("version", 0)
        if isinstance(version, int) and version > SCHEMA_VERSION:
            logger.warning(
                "Watchlist file schema version %d is newer than this "
                "ffn-dl build supports (%d); loading best-effort.",
                version, SCHEMA_VERSION,
            )
        entries = raw.get("watches", [])
        if not isinstance(entries, list):
            entries = []
        self._watches = [
            _watch_from_dict(entry) for entry in entries if isinstance(entry, dict)
        ]

    def save(self) -> None:
        """Write the watchlist atomically.

        Stages JSON to ``<path>.tmp`` and then renames over ``path`` so
        a crash between open and write never leaves the file truncated.
        """
        payload = {
            "version": SCHEMA_VERSION,
            "watches": [_watch_to_dict(w) for w in self._watches],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def _quarantine_corrupt_file(self) -> None:
        """Rename the corrupt file aside so the next save starts clean."""
        try:
            quarantine = self.path.with_suffix(
                f".corrupt-{int(time.time())}{self.path.suffix}"
            )
            self.path.rename(quarantine)
        except OSError as exc:
            # If we can't even rename it (permissions, read-only FS),
            # log and move on — the overwrite on next save() will fix it.
            logger.debug(
                "Could not quarantine corrupt watchlist at %s: %s",
                self.path, exc,
            )

    # ---- CRUD ----------------------------------------------------------

    def all(self) -> list[Watch]:
        """Return a shallow copy of all watches — callers can iterate freely."""
        return list(self._watches)

    def get(self, watch_id: str) -> Optional[Watch]:
        """Find a watch by full id or unambiguous id prefix.

        Prefix lookup keeps CLI usage ergonomic — users only have to
        type the first few hex chars of the id shown by ``--watch-list``.
        Returns None if no watch matches or if the prefix matches more
        than one (to avoid silently removing the wrong entry).
        """
        exact = next((w for w in self._watches if w.id == watch_id), None)
        if exact is not None:
            return exact
        candidates = [w for w in self._watches if w.id.startswith(watch_id)]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def add(self, watch: Watch) -> None:
        """Append ``watch`` and persist."""
        self._watches.append(watch)
        self.save()

    def remove(self, watch_id: str) -> bool:
        """Remove a watch by id or unambiguous prefix. Returns True on hit."""
        target = self.get(watch_id)
        if target is None:
            return False
        self._watches.remove(target)
        self.save()
        return True

    def update(self, watch: Watch) -> None:
        """Replace the stored watch whose id matches ``watch.id``."""
        for i, existing in enumerate(self._watches):
            if existing.id == watch.id:
                self._watches[i] = watch
                self.save()
                return
        raise KeyError(watch.id)


# ---------------------------------------------------------------------------
# URL classification — drives `--watch-add URL` auto-detection.
# ---------------------------------------------------------------------------


def site_key_for_url(url: str) -> str:
    """Return the lowercase site key for ``url`` (``"ao3"``, ``"ffn"``, …).

    Delegates hostname matching to :func:`sites.detect_scraper` so this
    module never encodes its own URL regex set. Returns an empty string
    for unrecognised hosts.
    """
    scraper_cls = sites.detect_scraper(url)
    return _SCRAPER_CLASS_TO_SITE_KEY.get(scraper_cls.__name__, "")


def classify_target(url: str) -> Optional[str]:
    """Classify ``url`` as ``"author"``, ``"story"``, or ``None``.

    Used by the CLI ``--watch-add URL`` path so the user doesn't have
    to spell out the watch type — we can tell "https://archiveofourown.org/users/foo"
    is an author and "https://archiveofourown.org/works/123" is a story
    from the URL alone.
    """
    if sites.is_author_url(url):
        return WATCH_TYPE_AUTHOR
    if sites.extract_story_url(url) is not None:
        return WATCH_TYPE_STORY
    return None


# ---------------------------------------------------------------------------
# Poll runner
# ---------------------------------------------------------------------------

# A factory callable that maps a URL to a ready-to-use scraper instance.
# Injectable so tests can run the poll loop with fake scrapers instead
# of real HTTP.
ScraperFactory = Callable[[str], BaseScraper]


def default_scraper_factory(url: str) -> BaseScraper:
    """Build the scraper subclass that handles ``url``.

    Mirrors the CLI's normal scraper resolution path so ``--watch-run``
    exercises the same code the interactive downloads do — if a scraper
    works for a regular download, it works for the watch runner.
    """
    scraper_cls = sites.detect_scraper(url)
    return scraper_cls()


@dataclass
class PollResult:
    """Per-watch outcome from a single :func:`run_once` pass.

    Attributes:
        watch_id: The watch this result belongs to.
        ok: True if the scraper returned data (even if nothing new);
            False if a network/site error occurred.
        new_items: For author/search watches, the list of newly-seen
            work URLs. For story watches, contains the story URL iff
            new chapters were detected. Empty when nothing changed.
        chapter_delta: For story watches only — how many new chapters
            were detected since the last poll. ``None`` for other types.
        error: Short error string; empty when ``ok`` is True.
        notification: The :class:`Notification` that should be (or was)
            dispatched for this result, or ``None`` if nothing to say.
    """

    watch_id: str
    ok: bool
    new_items: list[str] = field(default_factory=list)
    chapter_delta: Optional[int] = None
    error: str = ""
    notification: Optional[Notification] = None


# Notifier signature: (channels, notification, prefs) -> (delivered, failures).
Notifier = Callable[..., tuple]


def run_once(
    store: WatchlistStore,
    prefs,
    *,
    scraper_factory: ScraperFactory = default_scraper_factory,
    notifier: Notifier = dispatch_notification,
    now: Callable[[], float] = time.time,
) -> list[PollResult]:
    """Poll every enabled watch once. Returns per-watch results.

    Contract:

    * Disabled watches are skipped (and absent from the returned list).
    * Each enabled watch's ``last_checked_at`` and ``last_error`` are
      always updated and persisted, even if polling raised.
    * Scraper exceptions are caught per-watch; one failing site never
      blocks updates from the rest.
    * Notifications only fire when :class:`PollResult`\\ .notification
      is non-None *and* the watch isn't in its cooldown window; the
      cooldown is advanced only when we actually dispatch.
    * Notification dispatch failures are logged (by ``notifier``) but
      never raised — a broken webhook cannot stop the poll loop.
    """
    results: list[PollResult] = []
    for watch in store.all():
        if not watch.enabled:
            continue

        try:
            result = _poll_one(watch, scraper_factory)
        except Exception as exc:  # noqa: BLE001 — runner must never crash
            # Log with traceback so file-logging users can diagnose site
            # breakage without having to reproduce interactively.
            logger.exception(
                "Unexpected error polling watch %s (%s)",
                watch.id, watch.display_label(),
            )
            watch.last_checked_at = _now_iso()
            watch.last_error = str(exc) or exc.__class__.__name__
            store.update(watch)
            results.append(PollResult(
                watch_id=watch.id, ok=False, error=watch.last_error,
            ))
            continue

        watch.last_checked_at = _now_iso()
        watch.last_error = result.error

        if result.ok and result.notification is not None:
            if _in_cooldown(watch, now()):
                logger.info(
                    "Suppressing notification for %s — still in cooldown "
                    "until %s", watch.display_label(), watch.cooldown_until,
                )
            else:
                # dispatch_notification never raises — it returns
                # (delivered, failures) and logs each failure itself.
                notifier(watch.channels, result.notification, prefs)
                watch.cooldown_until = datetime.fromtimestamp(
                    now() + NOTIFICATION_COOLDOWN_S, tz=timezone.utc,
                ).isoformat(timespec="seconds")

        store.update(watch)
        results.append(result)
    return results


def _in_cooldown(watch: Watch, now_epoch: float) -> bool:
    """Return True if ``watch`` is still within its notification cooldown."""
    return _iso_to_epoch(watch.cooldown_until) > now_epoch


def _poll_one(watch: Watch, scraper_factory: ScraperFactory) -> PollResult:
    """Dispatch to the type-specific poller for ``watch``."""
    if watch.type == WATCH_TYPE_STORY:
        return _poll_story(watch, scraper_factory)
    if watch.type == WATCH_TYPE_AUTHOR:
        return _poll_author(watch, scraper_factory)
    if watch.type == WATCH_TYPE_SEARCH:
        return _poll_search(watch)
    return PollResult(
        watch_id=watch.id,
        ok=False,
        error=f"Unknown watch type: {watch.type!r}",
    )


def _poll_story(watch: Watch, scraper_factory: ScraperFactory) -> PollResult:
    """Probe a story's chapter count and diff against the last-seen value."""
    scraper = scraper_factory(watch.target)
    count = scraper.get_chapter_count(watch.target)
    if not isinstance(count, int):
        return PollResult(
            watch_id=watch.id,
            ok=False,
            error=f"Scraper returned non-int chapter count: {count!r}",
        )

    previous = watch.last_seen if isinstance(watch.last_seen, int) else None
    watch.last_seen = count

    # First-poll case: establish the baseline without firing an alert.
    # Authors who post mid-story shouldn't page the user on the first
    # `--watch-run` after the watch is added.
    if previous is None:
        return PollResult(watch_id=watch.id, ok=True, chapter_delta=0)

    if count <= previous:
        return PollResult(
            watch_id=watch.id, ok=True, chapter_delta=count - previous,
        )

    delta = count - previous
    plural = "" if delta == 1 else "s"
    notification = Notification(
        title=f"{watch.display_label()} — {delta} new chapter{plural}",
        message=(
            f"{watch.display_label()} now has {count} chapter{'s' if count != 1 else ''} "
            f"(was {previous})."
        ),
        url=watch.target,
    )
    return PollResult(
        watch_id=watch.id,
        ok=True,
        new_items=[watch.target],
        chapter_delta=delta,
        notification=notification,
    )


def _poll_author(watch: Watch, scraper_factory: ScraperFactory) -> PollResult:
    """Diff the URL set on an author page against the last-seen list."""
    scraper = scraper_factory(watch.target)
    _author_name, works = scraper.scrape_author_works(watch.target)
    urls = [w.get("url") for w in works if w.get("url")]

    previous_urls = watch.last_seen if isinstance(watch.last_seen, list) else None

    if previous_urls is None:
        # First poll — baseline only, no alert. Same rationale as
        # _poll_story: don't spam the user with "new!" for every work
        # the author already had when the watch was created.
        watch.last_seen = urls
        return PollResult(watch_id=watch.id, ok=True)

    previous = set(previous_urls)
    new_urls = [u for u in urls if u not in previous]
    watch.last_seen = urls

    if not new_urls:
        return PollResult(watch_id=watch.id, ok=True)

    new_titles = [
        (work.get("title") or work["url"]) for work in works
        if work.get("url") in new_urls
    ]
    notification = Notification(
        title=_format_count_headline(
            watch.display_label(), len(new_urls), "new work", "new works",
        ),
        message=_format_preview(new_titles),
        url=watch.target,
    )
    return PollResult(
        watch_id=watch.id,
        ok=True,
        new_items=new_urls,
        notification=notification,
    )


def _poll_search(watch: Watch) -> PollResult:
    """Re-run a saved site search and diff its result-URL set."""
    # Imported lazily — search.py pulls in per-site HTTP stacks, which
    # CLI users who never run a search shouldn't pay for at import time.
    from . import search as search_module

    search_fn = getattr(search_module, f"search_{watch.site}", None)
    if search_fn is None:
        return PollResult(
            watch_id=watch.id,
            ok=False,
            error=f"No search support for site {watch.site!r}",
        )

    results = search_fn(watch.query, page=1, **(watch.filters or {}))
    # Cap the tracked URL set so a saved search with thousands of hits
    # can't bloat the watchlist file indefinitely.
    results = list(results)[:SEARCH_WATCH_RESULT_CAP]
    urls = [r.get("url") for r in results if r.get("url")]

    previous_urls = watch.last_seen if isinstance(watch.last_seen, list) else None
    if previous_urls is None:
        watch.last_seen = urls
        return PollResult(watch_id=watch.id, ok=True)

    previous = set(previous_urls)
    new_urls = [u for u in urls if u not in previous]
    watch.last_seen = urls

    if not new_urls:
        return PollResult(watch_id=watch.id, ok=True)

    new_titles = [
        (r.get("title") or r["url"]) for r in results if r.get("url") in new_urls
    ]
    notification = Notification(
        title=_format_count_headline(
            watch.display_label(), len(new_urls), "new match", "new matches",
        ),
        message=_format_preview(new_titles),
    )
    return PollResult(
        watch_id=watch.id,
        ok=True,
        new_items=new_urls,
        notification=notification,
    )


def _format_count_headline(label: str, count: int, singular: str, plural: str) -> str:
    """Produce ``"<label> — N <unit>"`` with English singular/plural agreement."""
    unit = singular if count == 1 else plural
    return f"{label} — {count} {unit}"


def _format_preview(titles: list[str]) -> str:
    """Join up to :data:`NOTIFICATION_PREVIEW_LIMIT` titles with "(+N more)"."""
    if not titles:
        return ""
    head = titles[:NOTIFICATION_PREVIEW_LIMIT]
    remainder = len(titles) - len(head)
    preview = "; ".join(head)
    if remainder > 0:
        preview = f"{preview} (+{remainder} more)"
    return preview
