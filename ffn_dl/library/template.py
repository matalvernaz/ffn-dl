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

# Windows reserves these device names — a file called "CON.epub" fails
# to create on NTFS. Comparison is case-insensitive and covers the
# base name before any dot.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# Per-component cap. Most filesystems allow 255 bytes; leaving headroom
# keeps us safe with multibyte UTF-8 characters and lets the extension
# survive when we have to truncate. Full-path limits (Windows' 260-char
# default) are the user's concern once they pick a deep library root.
_MAX_SEGMENT_LEN = 200


def _safe(value: str) -> str:
    cleaned = _UNSAFE.sub("_", value).strip(". ")
    return cleaned or "_"


def _final_segment(s: str) -> str:
    """Post-split sanitizer applied to every path component.

    Handles two concerns _safe() can't, because they're properties of
    the whole segment rather than any one substituted value:

    * Length cap (preserves the file extension where it can)
    * Windows reserved device names (CON, PRN, etc.) — prefix an
      underscore so the final name isn't e.g. "CON.epub"
    """
    if len(s) > _MAX_SEGMENT_LEN:
        base, sep, ext = s.rpartition(".")
        # Only treat the trailing part as an extension if it looks
        # like one: short, alphanumeric, and non-empty. Otherwise
        # truncate the raw string so we don't cleave a title at a
        # coincidental dot.
        if sep and base and 1 <= len(ext) <= 10 and ext.isalnum():
            budget = max(1, _MAX_SEGMENT_LEN - len(sep) - len(ext))
            s = base[:budget] + sep + ext
        else:
            s = s[:_MAX_SEGMENT_LEN]
    base_for_reserved = s.split(".", 1)[0].lower()
    if base_for_reserved in _WINDOWS_RESERVED:
        s = "_" + s
    return s


def _pick_fandom(fandoms: list[str], misc_folder: str) -> str:
    """One-fandom stories get their fandom; multi-fandom and
    no-fandom stories go into the misc bucket. Matches the decision
    Matt made — no primary-fandom-first-tag heuristic."""
    if len(fandoms) == 1:
        return fandoms[0]
    return misc_folder


def parse_category(category: str | None) -> list[str]:
    """Split a site's ``category`` metadata string into a list of
    fandom names, stripped of site-specific decoration.

    Different scrapers hand us different shapes for the same field:

    * **FFN** builds a breadcrumb: ``"Books > Harry Potter"``. The
      leading meta-category ("Books", "Anime/Manga", "Movies", etc.)
      is inherent to the site's browsing taxonomy, not part of the
      fandom name — the user's folder should be ``Harry Potter``,
      not ``Books _ Harry Potter``.
    * **AO3** joins crossovers with `` / ``:
      ``"Harry Potter - J. K. Rowling / Naruto"``. Each ``/``-piece
      is a distinct fandom, so a crossover lands as multi-fandom
      (and :func:`_pick_fandom` routes it to the misc bucket).
    * **FicWad / MediaMiner / others** hand us a plain string with
      no special separator. That goes through untouched.

    The ordering of the splits matters. We strip the FFN breadcrumb
    first (take the last ``>``-delimited segment), then split the
    result on `` / `` for AO3 crossovers, then split each piece on
    commas as the legacy fallback. A string containing none of the
    separators returns as a single-element list with its original
    value — the "clean site" case Matt asked for.
    """
    if not category:
        return []
    # FFN breadcrumb: take only the tail after the last " > ".
    text = category.rsplit(" > ", 1)[-1].strip()
    # AO3 crossover join — splits into N fandoms.
    pieces = [p.strip() for p in text.split(" / ")]
    # Legacy comma fallback, applied after " / " so an AO3 fandom
    # that happens to have a comma inside one name still splits
    # predictably.
    fandoms: list[str] = []
    for piece in pieces:
        for sub in piece.split(","):
            s = sub.strip()
            if s:
                fandoms.append(s)
    return fandoms


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

    # Drop empty, "." and ".." segments. Empty handles a leading "/";
    # "." and ".." stop a poorly-templated or hostile metadata value
    # from escaping the library root when the caller joins the result
    # onto the root path.
    parts = [
        _final_segment(p)
        for p in rendered.split("/")
        if p and p not in (".", "..")
    ]
    return Path(*parts) if parts else Path("_")
