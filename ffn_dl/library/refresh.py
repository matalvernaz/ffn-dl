"""Index-driven update helpers.

Shared engine for ``--update-library`` (CLI) and the GUI's Check for
Updates button. Builds a probe_queue from the library index so the
existing ``cli._run_update_queue`` can run against it directly — same
concurrent probe + serial download + summary machinery used by
``--update-all``, just driven by the catalog rather than a directory
walk.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Callable

from ..updater import count_chapters, extract_status
from .index import LibraryIndex


def build_refresh_queue(
    root: Path,
    *,
    index_path: Path | None = None,
    skip_complete: bool = False,
    progress: Callable[[str], None] = print,
) -> tuple[list[dict], list[str]]:
    """Build (probe_queue, skipped) from the library index for ``root``.

    Each queue entry has ``path``, ``rel`` (display name), ``url``,
    ``local`` — the same shape ``cli._run_update_queue`` expects.
    Chapter counts come from disk when we can read them; fall back
    to the index's recorded count so foreign-format files (where
    ``count_chapters`` returns 0 because the HTML markers don't
    match) still get compared against the remote.
    """
    root = Path(root).expanduser().resolve()
    idx = LibraryIndex.load(index_path)
    stories = list(idx.stories_in(root))

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


def default_refresh_args(
    *,
    dry_run: bool = False,
    skip_complete: bool = False,
    workers: int = 5,
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
