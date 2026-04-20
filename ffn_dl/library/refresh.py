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


def build_refresh_queue(
    root: Path,
    *,
    index_path: Path | None = None,
    skip_complete: bool = False,
    recheck_interval_s: int = 0,
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
    """
    root = Path(root).expanduser().resolve()
    idx = LibraryIndex.load(index_path)
    stories = list(idx.stories_in(root))

    now_epoch = now() if recheck_interval_s > 0 else 0.0

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

        if skip_complete:
            try:
                status = extract_status(path)
            except Exception:
                status = ""
            if status.lower() == "complete":
                progress(
                    f"  [skip] {display_rel}: marked Complete ({local} chapters)"
                )
                skipped.append(display_rel)
                continue

        probe_queue.append(
            {"path": path, "rel": display_rel, "url": url, "local": local}
        )

    return probe_queue, skipped


DEFAULT_GUI_RECHECK_INTERVAL_S = 60 * 60
"""TTL the GUI's Check for Updates flow passes by default.

One hour balances two use cases: a user who just ran a check and
notices a missed story can retry without waiting, and a user who
reopens the dialog a few minutes later after adjusting settings
doesn't burn minutes re-probing everything they just probed. The
Force Full Recheck button bypasses it when the user genuinely wants
a fresh probe of the whole library.
"""


def default_refresh_args(
    *,
    dry_run: bool = False,
    skip_complete: bool = False,
    workers: int = 5,
    recheck_interval_s: int = 0,
    force_recheck: bool = False,
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
