"""Render a library-relative path from a file's metadata.

Pure function — no I/O, no file moves. The caller decides whether
to write to this path or just record it.

Template placeholders:
    {fandom}   — single fandom name, or the misc folder for multi-
                 fandom and no-fandom stories
    {title}
    {author}
    {ext}      — "epub" | "html" | "txt"
    {rating}   — "Unrated" when absent
    {status}   — "Unknown" when absent

Forward slash in the template separates path components. Slashes that
appear inside a placeholder value (e.g. a title literally containing
"/") are scrubbed out before substitution so they can't accidentally
split a field across directories.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..updater import FileMetadata


DEFAULT_TEMPLATE = "{fandom}/{title} - {author}.{ext}"
DEFAULT_MISC_FOLDER = "Misc"


# `/` is included so a title or author containing a slash can't
# hijack the template's path structure. We strip it before substitution
# then split on `/` to recover the template-intended separators.
_UNSAFE = re.compile(r'[/<>:"\\|?*\x00-\x1f]')


def _safe(value: str) -> str:
    cleaned = _UNSAFE.sub("_", value).strip(". ")
    return cleaned or "_"


def _pick_fandom(fandoms: list[str], misc_folder: str) -> str:
    """One-fandom stories get their fandom; multi-fandom and
    no-fandom stories go into the misc bucket. Matches the decision
    Matt made — no primary-fandom-first-tag heuristic."""
    if len(fandoms) == 1:
        return fandoms[0]
    return misc_folder


def render(
    metadata: FileMetadata,
    template: str = DEFAULT_TEMPLATE,
    misc_folder: str = DEFAULT_MISC_FOLDER,
) -> Path:
    """Return a library-relative path for this file, per template."""
    fields = {
        "fandom": _safe(_pick_fandom(metadata.fandoms, misc_folder)),
        "title": _safe(metadata.title or "Unknown Title"),
        "author": _safe(metadata.author or "Unknown Author"),
        "ext": _safe(metadata.format or "bin"),
        "rating": _safe(metadata.rating or "Unrated"),
        "status": _safe(metadata.status or "Unknown"),
    }
    try:
        rendered = template.format_map(fields)
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder {exc} in library path template. "
            f"Available: {', '.join('{' + k + '}' for k in fields)}"
        ) from None

    parts = [p for p in rendered.split("/") if p]
    return Path(*parts) if parts else Path("_")
