"""Index-driven update helpers.

Shared engine for ``--update-library`` (CLI) and the GUI's Check for
Updates button. Builds a probe_queue from the library index so the
existing ``cli._run_update_queue`` can run against it directly — same
concurrent probe + serial download + summary machinery used by
``--update-all``, just driven by the catalog rather than a directory
walk.
"""

from __future__ import annotations

import time
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..updater import count_chapters, extract_status
from .index import LibraryIndex


def _parse_iso_to_epoch(iso: str) -> float:
    """Convert an ISO-8601 UTC timestamp to an epoch float.

    Returns ``0.0`` on any parse failure — treated as "never probed"
    by the TTL check, which keeps a corrupt timestamp from silently
    blocking updates forever.
    """
    if not iso:
        return 0.0
    try:
        # fromisoformat tolerates both the ``Z`` suffix (Python 3.11+)
        # and ``+00:00`` offsets; normalise to the latter for older
        # Pythons so the test suite doesn't care about which shape
        # happened to land in the index.
        normalised = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(normalised).timestamp()
    except ValueError:
        return 0.0


def _human_duration(seconds: float) -> str:
    """Compact "5m ago" / "2h ago" / "3d ago" form for skip messages."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _cached_chapter_count(path: Path, entry: dict) -> int | None:
    """Return the index entry's cached chapter_count when we can trust it.

    The index records ``file_mtime`` and ``file_size`` alongside
    ``chapter_count``. If both match the file currently on disk, the
    zip parse inside :func:`ffn_dl.updater.count_chapters` can be
    skipped — that's the hot Phase 1 cost for any update-scan of a
    library with thousands of untouched EPUBs.

    Returns the cached int when the cache is valid; ``None`` when the
    caller has to re-read. Older indexes written before this field
    existed naturally fall through to ``None`` until the next scan
    re-records the entry with mtime/size populated.
    """
    cached_mtime = entry.get("file_mtime")
    cached_size = entry.get("file_size")
    cached_count = entry.get("chapter_count")
    if (
        not isinstance(cached_count, int)
        or cached_count <= 0
        or cached_mtime is None
        or cached_size is None
    ):
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    # Float compare with a small tolerance: filesystems round mtime to
    # microseconds, and JSON round-trip through floats can perturb the
    # last bit. 1 ms slack is smaller than the granularity any real
    # story-save operation produces and well under any realistic edit
    # window — a legitimate re-save bumps mtime by ≥1s.
    if abs(st.st_mtime - float(cached_mtime)) > 1e-3:
        return None
    if st.st_size != int(cached_size):
        return None
    return cached_count


SECONDS_PER_DAY = 86400
"""Used by the stale-complete gate so the CLI flag reads in days while
the internal comparison stays in epoch seconds."""


def _is_terminal_status(status: object) -> bool:
    """True if the entry's status means upstream has nothing more for us.

    Matches both ``Complete`` (FFN/AO3/Wattpad/RR normalised) and
    ``Completed`` (the older HTML-metadata files where the scanner
    parsed the literal ``Status: Completed`` line untouched). Also
    treats ``Abandoned`` as terminal — the user types this manually
    in a few cases, separately from the explicit ``abandoned_at``
    timestamp set by ``--mark-abandoned-after``.
    """
    if not isinstance(status, str):
        return False
    s = status.strip().lower()
    return s.startswith("complete") or s == "abandoned"


def build_refresh_queue(
    root: Path,
    *,
    index_path: Path | None = None,
    skip_complete: bool = True,
    recheck_interval_s: int = 0,
    skip_stale_complete_days: int = 0,
    skip_abandoned: bool = True,
    progress: Callable[[str], None] = print,
    now: Callable[[], float] = time.time,
) -> tuple[list[dict], list[str]]:
    """Build (probe_queue, skipped) from the library index for ``root``.

    Each queue entry has ``path``, ``rel`` (display name), ``url``,
    ``local`` — the same shape ``cli._run_update_queue`` expects.
    Chapter counts come from disk when we can read them; fall back
    to the index's recorded count so foreign-format files (where
    ``count_chapters`` returns 0 because the HTML markers don't
    match) still get compared against the remote.

    When ``recheck_interval_s`` is positive, stories whose index
    ``last_probed`` timestamp is newer than ``now - recheck_interval_s``
    are skipped with a clear message — the TTL makes a second
    ``--update-library`` run inside the window near-instant instead
    of re-hitting the network for every story. ``0`` (the CLI default)
    preserves the pre-TTL behaviour so scripted callers don't change
    behaviour without opting in.

    ``skip_complete`` (default on) drops any entry whose index
    ``status`` is Complete, Completed, or Abandoned. The check is
    a single dict lookup — no disk read — so adding it to a 1000-
    story refresh costs nothing while saving the upstream probe for
    every finished fic. ``--force-recheck`` (CLI) and the GUI's
    Force-recheck checkbox flip this off. Pending-resume entries
    (``remote_chapter_count > local``) bypass the gate so an owed
    download still lands.

    ``skip_stale_complete_days`` is a gentler companion to
    ``skip_complete``: it skips a story only when it's both marked
    Complete *and* its file has been untouched for that many days.
    Mostly redundant now that ``skip_complete`` is the default —
    kept for callers that explicitly opt out of the blanket skip
    but still want the stale-only behaviour. ``0`` disables the
    gate; a pending-resume entry bypasses it.

    ``skip_abandoned`` (default on) drops any entry carrying an
    ``abandoned_at`` timestamp — set by ``--mark-abandoned-after``
    or the programmatic :func:`mark_abandoned` helper — from the
    queue. Intent: once the user has declared a WIP dead, stop
    spending HTTP probes on it until they revive it explicitly.
    """
    root = Path(root).expanduser().resolve()
    idx = LibraryIndex.load(index_path)
    stories = list(idx.stories_in(root))

    stale_gate_active = skip_stale_complete_days > 0
    now_epoch = now() if (recheck_interval_s > 0 or stale_gate_active) else 0.0
    stale_cutoff_epoch = (
        now_epoch - skip_stale_complete_days * SECONDS_PER_DAY
        if stale_gate_active
        else 0.0
    )

    probe_queue: list[dict] = []
    skipped: list[str] = []
    for url, entry in stories:
        rel = entry.get("relpath") or ""
        path = root / rel
        display_rel = rel or str(path)

        if not path.exists():
            progress(f"  [skip] {display_rel}: file missing on disk")
            skipped.append(display_rel)
            continue

        if skip_abandoned:
            abandoned_at = entry.get("abandoned_at")
            if abandoned_at:
                # The date prefix (first 10 chars of the ISO string)
                # is what a reader cares about — surfaces "marked Jan
                # 2025" rather than a full second-resolution stamp.
                date_prefix = str(abandoned_at)[:10]
                progress(
                    f"  [skip] {display_rel}: marked abandoned "
                    f"({date_prefix}; --revive-abandoned to undo)"
                )
                skipped.append(display_rel)
                continue

        # Index-driven skip-complete: check before doing any disk
        # work. The pending-resume bypass below still runs first via
        # the order of the gates further down — but we have to look
        # up the pending count there, not here, so a Complete fic
        # with an unfinished download can still land.
        if skip_complete and _is_terminal_status(entry.get("status")):
            pending_remote = entry.get("remote_chapter_count")
            cached_count = entry.get("chapter_count")
            has_pending_download = (
                isinstance(pending_remote, int)
                and isinstance(cached_count, int)
                and pending_remote > cached_count
            )
            if not has_pending_download:
                status_label = str(entry.get("status") or "").strip() or "Complete"
                progress(
                    f"  [skip] {display_rel}: marked {status_label} "
                    "(--no-skip-complete / --force-recheck to override)"
                )
                skipped.append(display_rel)
                continue

        cached = _cached_chapter_count(path, entry)
        if cached is not None:
            local = cached
        else:
            try:
                local = count_chapters(path)
            except Exception as exc:
                progress(f"  [skip] {display_rel}: couldn't read ({exc})")
                skipped.append(display_rel)
                continue

        if local == 0:
            # count_chapters looks for ffn-dl's own chapter markers
            # (div.chapter, "--- Chapter ---", chapter_*.xhtml). A
            # FanFicFare/FicHub file uses different markers and comes
            # back with 0 chapters even when it actually has many.
            # The index stored the count at scan time, so fall back
            # to that — it's our best guess and only stale by one
            # update cycle.
            local = int(entry.get("chapter_count") or 0)
            if local == 0:
                progress(
                    f"  [skip] {display_rel}: chapter count unknown "
                    "(not an ffn-dl export and index has 0)"
                )
                skipped.append(display_rel)
                continue

        # Resume-without-reprobe: if a previous run recorded a remote
        # chapter count larger than ``local`` and the file hasn't
        # caught up yet, the story has a *pending* download waiting.
        # Queue it with ``remote`` pre-filled so Phase 2 (probing)
        # skips straight past it — this is what lets an interrupted
        # --update-library batch resume without re-hitting upstream
        # for every story that was already probed before the crash.
        pending_remote = entry.get("remote_chapter_count")
        if (
            isinstance(pending_remote, int)
            and pending_remote > local
        ):
            # The terminal-status gate above already lets pending
            # downloads through — anything that reaches here either
            # isn't Complete or has work owed, both of which we want
            # to finish.
            progress(
                f"  [resume] {display_rel}: {local} local / "
                f"{pending_remote} upstream — queued without re-probing"
            )
            probe_queue.append({
                "path": path,
                "rel": display_rel,
                "url": url,
                "local": local,
                "remote": pending_remote,
            })
            continue

        if recheck_interval_s > 0:
            last_probed_epoch = _parse_iso_to_epoch(
                entry.get("last_probed") or ""
            )
            if last_probed_epoch > 0:
                age = now_epoch - last_probed_epoch
                if age < recheck_interval_s:
                    progress(
                        f"  [skip] {display_rel}: checked "
                        f"{_human_duration(age)} ago "
                        "(use --force-recheck to override)"
                    )
                    skipped.append(display_rel)
                    continue

        if stale_gate_active:
            # Prefer the index status (free); fall back to the file
            # only when the index doesn't carry one (older indexes
            # written before the field existed).
            entry_status = entry.get("status")
            if isinstance(entry_status, str) and entry_status:
                status = entry_status
            else:
                try:
                    status = extract_status(path)
                except Exception:
                    status = ""
            if _is_terminal_status(status):
                try:
                    file_mtime = path.stat().st_mtime
                except OSError:
                    file_mtime = None
                if file_mtime is not None and file_mtime < stale_cutoff_epoch:
                    age_days = int((now_epoch - file_mtime) // SECONDS_PER_DAY)
                    progress(
                        f"  [skip] {display_rel}: Complete and untouched "
                        f"for {age_days}d "
                        "(use --force-recheck to override)"
                    )
                    skipped.append(display_rel)
                    continue

        probe_queue.append(
            {"path": path, "rel": display_rel, "url": url, "local": local}
        )

    return probe_queue, skipped


DEFAULT_GUI_RECHECK_INTERVAL_S = 6 * 60 * 60
"""TTL the GUI's Check for Updates flow passes by default.

Six hours rather than one because real-world usage clusters into
"open the dialog, poke around, close it, come back later today" —
one-hour expiry was short enough that users who clicked the button
again after lunch got a surprise full re-probe. Force Full Recheck
still bypasses the TTL when the user genuinely wants a fresh sweep
(e.g., suspecting the upstream site changed its chapter count
detection); the CLI's --recheck-interval flag accepts any value.
"""


def default_refresh_args(
    *,
    dry_run: bool = False,
    skip_complete: bool = True,
    workers: int = 5,
    recheck_interval_s: int = 0,
    force_recheck: bool = False,
    refetch_all: bool = False,
    skip_stale_complete_days: int = 0,
) -> Namespace:
    """Namespace with sensible defaults for callers that need to drive
    ``cli._build_scraper`` / ``cli._download_one`` without having gone
    through argparse. Used by the GUI's Check for Updates button.

    Anything ``_download_one`` reads off ``args`` has to be present on
    this Namespace — missing attributes raise ``AttributeError`` at
    download time, which the GUI would then surface as an opaque
    "Update failed" message. Read user-configurable fields (filename
    template, strip-notes / hr-as-stars flags) from :class:`Prefs` so
    the GUI-driven update path honours the same settings as the
    CLI, with the CLI's argparse defaults as a secondary fallback.
    """
    # Imported locally so this helper is safe to call from environments
    # where wxPython isn't installed (``Prefs`` gracefully no-ops when
    # wx is unavailable). Same rationale as the lazy import in cli.py.
    from ..exporters import DEFAULT_TEMPLATE
    from ..prefs import (
        KEY_HR_AS_STARS,
        KEY_NAME_TEMPLATE,
        KEY_STRIP_NOTES,
        Prefs,
    )

    prefs = Prefs()
    return Namespace(
        # Scraper tuning
        max_retries=5,
        no_cache=False,
        delay_min=None,
        delay_max=None,
        chunk_size=None,
        use_wayback=False,
        # Run options
        dry_run=dry_run,
        skip_complete=skip_complete,
        probe_workers=workers,
        recheck_interval=recheck_interval_s,
        force_recheck=force_recheck,
        refetch_all=refetch_all,
        skip_stale_complete=skip_stale_complete_days,
        # Export path knobs. ``name`` is the filename template; without
        # it, ``_download_one``'s export branch raises AttributeError.
        format=None,
        output=None,
        chapters=None,
        name=prefs.get(KEY_NAME_TEMPLATE) or DEFAULT_TEMPLATE,
        hr_as_stars=prefs.get_bool(KEY_HR_AS_STARS),
        strip_notes=prefs.get_bool(KEY_STRIP_NOTES),
        # Library updates never re-generate audiobooks, but the audio
        # branch still reads these — set plausible defaults so a
        # future code path that does hit them doesn't crash.
        speech_rate="0",
        attribution="builtin",
        attribution_model_size="",
        # Misc download-time flags accessed by _download_one.
        send_to_kindle=None,
        clean_cache=False,
    )
