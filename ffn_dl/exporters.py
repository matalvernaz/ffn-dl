"""Export a Story to EPUB, HTML, or plain text."""

import io
import logging
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup, NavigableString, Tag

from .atomic import atomic_path, atomic_write_text
from .models import Story

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE = "{title} - {author}"


def _emit(progress: Callable[[str], None] | None, line: str) -> None:
    """Send a status line both to the GUI / CLI ``progress`` callback
    and to the file logger.

    Without this mirror, anything the user sees in the GUI status pane
    (cache hits, per-chapter LLM calls, circuit-breaker trips, pull
    progress) is invisible to a postmortem of ``ffn-dl.log``. Routing
    through one helper keeps the two streams in sync — the on-screen
    text and the on-disk log say the same thing in the same order.

    When ``progress`` is set, the line is already on its way to the
    user (GUI status pane or CLI stdout). The ``ui_already_emitted``
    marker on the log record tells display-side handlers (the GUI's
    ``_WxLogHandler`` in particular) to skip it so it doesn't appear
    twice. File handlers ignore the marker and capture the line as
    normal.
    """
    if progress:
        progress(line)
        logger.info(
            "%s", line.lstrip(),
            extra={"ui_already_emitted": True},
        )
    else:
        logger.info("%s", line.lstrip())


def _safe_filename(name):
    """Strip characters that are illegal in filenames on Windows/macOS/Linux."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")


def format_filename(story: Story, template: str = DEFAULT_TEMPLATE) -> str:
    """Build a filename (no extension) from a template and story metadata."""
    fields = {
        "title": story.title,
        "author": story.author,
        "id": str(story.id),
        "words": story.metadata.get("words", "unknown"),
        "status": story.metadata.get("status", "unknown"),
        "rating": story.metadata.get("rating", "unknown"),
        "language": story.metadata.get("language", "unknown"),
        "chapters": str(len(story.chapters)),
    }
    try:
        raw = template.format_map(fields)
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder {exc} in --name template.\n"
            f"Available: {', '.join(f'{{{k}}}' for k in fields)}"
        ) from None
    return _safe_filename(raw)


# ── Metadata helpers ──────────────────────────────────────────────


def _format_epoch(ts):
    """Format an epoch timestamp as YYYY-MM-DD."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _count_story_words(story: Story) -> int:
    """Total word count across every downloaded chapter's rendered text.
    Used as a fallback when the source site doesn't expose a word count
    in its metadata. Counts runs of \\w+ characters after HTML strip.
    """
    total = 0
    for ch in story.chapters:
        if not ch.html:
            continue
        text = BeautifulSoup(ch.html, "html.parser").get_text(" ", strip=True)
        total += len(re.findall(r"\w+", text))
    return total


def _meta_fields(story: Story) -> list[tuple[str, str]]:
    """Return an ordered list of (label, value) pairs for the story header."""
    m = story.metadata
    fields = []
    fields.append(("Title", story.title))
    fields.append(("Author", story.author))
    if "category" in m:
        fields.append(("Category", m["category"]))
    if "genre" in m:
        fields.append(("Genre", m["genre"].replace(",", ", ")))
    if "characters" in m:
        fields.append(("Characters", m["characters"]))
    if story.summary:
        fields.append(("Summary", story.summary))
    if "status" in m:
        fields.append(("Status", m["status"]))
    if "rating" in m:
        fields.append(("Rating", m["rating"]))
    fields.append(("Chapters", str(len(story.chapters))))
    # Words: prefer the source site's count (accurate, includes anything
    # we didn't download like omakes or appendices); fall back to
    # counting our rendered chapter text so sites that don't expose one
    # (RR, MediaMiner, Literotica) still get a number in the header.
    total_words = None
    if "words" in m and m["words"]:
        words_display = m["words"]
        try:
            total_words = int(str(m["words"]).replace(",", ""))
        except (TypeError, ValueError):
            total_words = None
    else:
        counted = _count_story_words(story)
        if counted:
            words_display = f"{counted:,}"
            total_words = counted
        else:
            words_display = None
    if words_display:
        fields.append(("Words", words_display))
        if total_words:
            total_minutes = max(1, round(total_words / 250))
            if total_minutes >= 60:
                hours, minutes = divmod(total_minutes, 60)
                reading_time = f"{hours} hours {minutes} minutes"
            else:
                reading_time = f"{total_minutes} minutes"
            fields.append(("Reading Time", reading_time))
    if "date_updated" in m:
        fields.append(("Updated", _format_epoch(m["date_updated"])))
    elif "updated" in m:
        fields.append(("Updated", m["updated"]))
    if "date_published" in m:
        fields.append(("Published", _format_epoch(m["date_published"])))
    elif "published" in m:
        fields.append(("Published", m["published"]))
    fields.append(("Downloaded", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")))
    fields.append(("Source", story.url))
    return fields


# ── HTML → plain-text converter ───────────────────────────────────


def html_to_text(html: str) -> str:
    """Convert chapter HTML to readable plain text."""
    soup = BeautifulSoup(html, "html.parser")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    parts = []
    for child in soup.children:
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                parts.append(text)
        elif isinstance(child, Tag):
            if child.name == "hr":
                parts.append("* * *")
            else:
                text = child.get_text().strip()
                if text:
                    parts.append(text)

    return "\n\n".join(parts)


# ── Exporters ─────────────────────────────────────────────────────


def _prepare_chapter_html(
    html: str,
    hr_as_stars: bool,
    strip_notes: bool,
    *,
    llm_config: dict | None = None,
    site_name: str | None = None,
    story_id=None,
    chapter_number: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Apply optional chapter-level transformations in the right order.

    ``llm_config`` enables a second-pass A/N strip via
    :func:`strip_an_via_llm`. Only runs when ``strip_notes`` is also
    on — the LLM is a backstop for cases the regex misses, not a
    replacement for it. ``site_name`` / ``story_id`` /
    ``chapter_number`` are forwarded into the LLM helper's per-story
    disk cache so re-exports don't re-spend tokens.
    """
    if strip_notes:
        html = strip_note_paragraphs(html)
        if llm_config:
            html = strip_an_via_llm(
                html,
                llm_config=llm_config,
                site_name=site_name,
                story_id=story_id,
                chapter_number=chapter_number,
                progress=progress,
            )
    if hr_as_stars:
        html = _apply_hr_as_stars(html)
    return html


# Consecutive ``LLMTimeout``s before the chapter loop disables the
# LLM A/N pass for the remaining chapters. One timeout is treated as
# transient — a 14B local model can spend a few minutes on an unusually
# long chapter, but the model is fine for the next one. Three in a row
# means something is genuinely wrong (model hung, GPU starved, OOM
# killed) and we stop burning the per-chapter timeout budget.
_LLM_AN_MAX_CONSECUTIVE_TIMEOUTS = 3


def _prepare_chapter_html_with_llm_fallback(
    html: str,
    hr_as_stars: bool,
    strip_notes: bool,
    *,
    llm_config: dict | None,
    site_name: str | None,
    story_id,
    chapter_number: int | None,
    progress: Callable[[str], None] | None,
    consecutive_timeouts: int = 0,
) -> tuple[str, bool, int]:
    """Run the chapter pipeline; recover from LLM A/N failures.

    Returns ``(prepared_html, llm_disabled, consecutive_timeouts)``.

    Failure handling differentiates by exception type:

    * :class:`~ffn_dl.attribution.LLMUnavailable` (non-timeout) —
      connection refused / DNS / no-listener. Trips the breaker on
      the first hit: every remaining chapter would hit the same
      wall, so we re-run this chapter without the LLM and return
      ``llm_disabled=True``.
    * :class:`~ffn_dl.attribution.LLMTimeout` — endpoint accepted
      the connection but the model didn't reply in
      :func:`~ffn_dl.attribution._llm_request_timeout_s` seconds.
      A single timeout is transient; we re-run this chapter without
      the LLM but keep ``llm_disabled=False`` so the next chapter
      retries. Only after
      :data:`_LLM_AN_MAX_CONSECUTIVE_TIMEOUTS` consecutive timeouts
      do we trip the breaker.

    ``consecutive_timeouts`` is the running tally that the chapter
    loop threads back in on each call. Successful classifications
    reset it to zero (so a one-off slow chapter doesn't pollute the
    count for the rest of the export).
    """
    from .attribution import LLMTimeout, LLMUnavailable

    try:
        prepared = _prepare_chapter_html(
            html, hr_as_stars, strip_notes,
            llm_config=llm_config,
            site_name=site_name,
            story_id=story_id,
            chapter_number=chapter_number,
            progress=progress,
        )
        # Reset the timeout streak on any success — including
        # success after the LLM was already disabled, which
        # harmlessly returns 0.
        return prepared, False, 0
    except LLMTimeout as exc:
        new_streak = consecutive_timeouts + 1
        chapter_label = (
            f"chapter {chapter_number}"
            if chapter_number is not None
            else "chapter"
        )
        # Always re-run *this* chapter without the LLM so the
        # regex-stripped content still lands in the export.
        fallback = _prepare_chapter_html(
            html, hr_as_stars, strip_notes,
            llm_config=None,
            site_name=site_name,
            story_id=story_id,
            chapter_number=chapter_number,
            progress=progress,
        )
        if new_streak >= _LLM_AN_MAX_CONSECUTIVE_TIMEOUTS:
            # Streak crossed the threshold — treat as a genuine
            # outage and disable for the remainder of the export.
            logger.warning(
                "LLM A/N: %d consecutive timeouts (%s); "
                "disabled for remaining chapters in this run",
                new_streak, exc,
            )
            _emit(
                progress,
                f"  [llm-an] {chapter_label}: {new_streak} consecutive "
                f"timeouts ({exc}); skipping LLM for remaining chapters",
            )
            return fallback, True, new_streak
        logger.info(
            "LLM A/N: timeout on %s (%s); skipping LLM on this "
            "chapter, will retry on the next one (%d/%d)",
            chapter_label, exc, new_streak,
            _LLM_AN_MAX_CONSECUTIVE_TIMEOUTS,
        )
        _emit(
            progress,
            f"  [llm-an] {chapter_label}: timeout ({exc}); "
            f"skipping LLM on this chapter, retrying on the next "
            f"({new_streak}/{_LLM_AN_MAX_CONSECUTIVE_TIMEOUTS})",
        )
        return fallback, False, new_streak
    except LLMUnavailable as exc:
        # Endpoint truly down (connection refused / DNS / no
        # listener). Log loud (warning, not info) so a "why didn't
        # my A/N strip work?" postmortem on the file log surfaces
        # this in a single grep, even when the user is running with
        # the default INFO log level.
        logger.warning(
            "LLM A/N: endpoint unreachable (%s); disabled for remaining "
            "chapters in this run",
            exc,
        )
        _emit(
            progress,
            f"  [llm-an] endpoint unreachable ({exc}); "
            "skipping LLM for remaining chapters",
        )
        return (
            _prepare_chapter_html(
                html, hr_as_stars, strip_notes,
                llm_config=None,
                site_name=site_name,
                story_id=story_id,
                chapter_number=chapter_number,
                progress=progress,
            ),
            True,
            0,
        )


def export_txt(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,  # accepted for signature parity; TXT always renders hr as "* * *"
    strip_notes: bool = False,
    llm_config: dict | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    filename = format_filename(story, template) + ".txt"
    path = Path(output_dir) / filename

    site_name, _publisher = _site_info(story.url)

    # Assemble in memory and hand the finished payload to the atomic
    # writer so a crash mid-export can't leave a half-written file in
    # the library — the next library scan would treat the partial as
    # a valid story and skip re-downloading it.
    buf = io.StringIO()
    for label, value in _meta_fields(story):
        buf.write(f"{label}: {value}\n")
    buf.write("=" * 60 + "\n")
    consecutive_timeouts = 0
    for ch in story.chapters:
        buf.write(f"\n\n--- {ch.title} ---\n\n")
        html, llm_disabled, consecutive_timeouts = (
            _prepare_chapter_html_with_llm_fallback(
                ch.html, hr_as_stars=False, strip_notes=strip_notes,
                llm_config=llm_config,
                site_name=site_name, story_id=story.id,
                chapter_number=ch.number, progress=progress,
                consecutive_timeouts=consecutive_timeouts,
            )
        )
        if llm_disabled:
            llm_config = None
        buf.write(html_to_text(html))
    atomic_write_text(path, buf.getvalue())
    return path


def export_html(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,
    strip_notes: bool = False,
    llm_config: dict | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    filename = format_filename(story, template) + ".html"
    path = Path(output_dir) / filename

    site_name, _publisher = _site_info(story.url)

    title_esc = escape(story.title)
    author_esc = escape(story.author)

    # Build the metadata table rows — Author and Source are links
    meta_rows = []
    for label, value in _meta_fields(story):
        val_esc = escape(value)
        if label == "Author" and story.author_url:
            cell = f'<a href="{escape(story.author_url)}">{val_esc}</a>'
        elif label == "Source":
            cell = f'<a href="{escape(value)}">{val_esc}</a>'
        elif label == "Summary":
            cell = f'<em>{val_esc}</em>'
        else:
            cell = val_esc
        meta_rows.append(f"<tr><th>{label}</th><td>{cell}</td></tr>")

    # Build the full document in memory first, then atomic-write it.
    # See ``export_txt`` for why this shape matters.
    buf = io.StringIO()
    buf.write(
        f'<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        f'<meta charset="utf-8">\n'
        f"<title>{title_esc} by {author_esc}</title>\n"
        f"<style>\n"
        f"body{{max-width:800px;margin:2em auto;padding:0 1em;"
        f"font-family:Georgia,serif;line-height:1.6}}\n"
        f"h1{{text-align:center}}\n"
        f".meta-table{{border-collapse:collapse;margin:1em 0;width:100%}}\n"
        f".meta-table th{{text-align:right;padding:.25em 1em .25em 0;"
        f"vertical-align:top;white-space:nowrap;color:#555}}\n"
        f".meta-table td{{padding:.25em 0;vertical-align:top}}\n"
        f".chapter{{margin:2em 0}}\n"
        f".chapter h2{{border-bottom:1px solid #ddd;padding-bottom:.3em}}\n"
        f".chapter p{{margin:0 0 0.25em 0;text-indent:1.5em}}\n"
        f".chapter h2+p,.chapter hr+p,.chapter .scenebreak+p{{text-indent:0}}\n"
        f"blockquote{{margin:1em 2em;font-style:italic}}\n"
        f"blockquote p{{text-indent:0}}\n"
        f".scenebreak{{text-align:center;margin:1.5em 0;letter-spacing:.5em}}\n"
        f".center,[align=center]{{text-align:center}}\n"
        f"a{{color:#36c}}\n"
        f"</style>\n</head>\n<body>\n"
        f"<h1>{title_esc}</h1>\n"
        f'<table class="meta-table">\n'
    )
    for row in meta_rows:
        buf.write(f"{row}\n")
    buf.write("</table>\n<hr>\n")

    # Table of Contents
    buf.write('<nav id="toc">\n<h2>Table of Contents</h2>\n<ol>\n')
    for i, ch in enumerate(story.chapters, 1):
        buf.write(f'<li><a href="#chapter-{i}">{escape(ch.title)}</a></li>\n')
    buf.write("</ol>\n</nav>\n<hr>\n")

    consecutive_timeouts = 0
    for i, ch in enumerate(story.chapters, 1):
        ch_title = escape(ch.title)
        buf.write(f'<div class="chapter" id="chapter-{i}"><h2>{ch_title}</h2>\n')
        chapter_html, llm_disabled, consecutive_timeouts = (
            _prepare_chapter_html_with_llm_fallback(
                ch.html, hr_as_stars, strip_notes,
                llm_config=llm_config,
                site_name=site_name,
                story_id=story.id,
                chapter_number=ch.number,
                progress=progress,
                consecutive_timeouts=consecutive_timeouts,
            )
        )
        if llm_disabled:
            llm_config = None
        buf.write(chapter_html)
        buf.write("\n</div><hr>\n")

    buf.write("</body>\n</html>\n")
    atomic_write_text(path, buf.getvalue())
    return path


_COVER_CACHE_TTL_S = 7 * 24 * 3600
"""Cover images are near-immutable once the author uploads them —
AO3 re-hosts by content-hash, FFN and Wattpad serve from a CDN with
aggressive caching. A week's TTL is long enough that re-exporting
a story the next day doesn't re-fetch, and short enough that a
deliberately replaced cover makes it into the library within a
normal update cycle."""


def _cover_cache_path(cover_url: str):
    """Return the on-disk path for a cached cover image, or ``None``
    when the portable/cache bootstrap isn't available (e.g. during
    some tests). We key the cache on a hash of the URL — the URL
    itself is too long / contains characters illegal in filenames on
    Windows."""
    try:
        from . import portable
        cache_root = portable.cache_dir() / "covers"
    except Exception:
        return None
    cache_root.mkdir(parents=True, exist_ok=True)
    import hashlib
    digest = hashlib.sha256(cover_url.encode("utf-8")).hexdigest()[:24]
    return cache_root / digest


def _fetch_cover_image(cover_url, *, use_cache: bool = True):
    """Download a cover image, returning ``(content_bytes, media_type)``
    or ``None``.

    When ``use_cache`` is true (the default), the result is cached
    under the portable cache dir keyed on a hash of ``cover_url``.
    Subsequent exports of the same story — or of different stories
    with the same cover URL, which happens for anthology series —
    skip the network fetch entirely.

    The cache entry stores the content-type as a short header line
    so reads can reconstruct the ``(bytes, media_type)`` tuple
    without a second round-trip. Cached entries older than
    :data:`_COVER_CACHE_TTL_S` are re-fetched so updated covers
    propagate into re-exports within a normal update cycle.
    """
    import time
    cache_path = _cover_cache_path(cover_url) if use_cache else None
    if cache_path is not None and cache_path.exists():
        try:
            age = time.time() - cache_path.stat().st_mtime
        except OSError:
            age = _COVER_CACHE_TTL_S + 1  # force refetch
        if age < _COVER_CACHE_TTL_S:
            try:
                blob = cache_path.read_bytes()
                # Format: ``<media_type>\n<bytes>``. The media type
                # never contains a newline in practice, so a single
                # newline terminator is an unambiguous split.
                newline = blob.find(b"\n")
                if newline > 0:
                    media_type = blob[:newline].decode(
                        "ascii", errors="replace",
                    )
                    content = blob[newline + 1:]
                    if len(content) > 500:
                        return content, media_type
            except OSError:
                # Corrupt / unreadable cache entry — fall through to
                # the live fetch. The next successful fetch overwrites
                # the bad entry.
                pass

    try:
        from curl_cffi import requests as curl_requests

        resp = curl_requests.get(cover_url, impersonate="chrome", timeout=15)
        if resp.status_code == 200 and len(resp.content) > 500:
            ct = resp.headers.get("content-type", "image/jpeg")
            if cache_path is not None:
                try:
                    from .atomic import atomic_write_bytes
                    atomic_write_bytes(
                        cache_path,
                        ct.encode("ascii", errors="replace") + b"\n" + resp.content,
                    )
                except OSError:
                    # Cache is best-effort — a full disk or a
                    # permission issue shouldn't fail the export.
                    pass
            return resp.content, ct
    except Exception:
        pass
    return None


_LANG_CODES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "russian": "ru", "japanese": "ja",
    "chinese": "zh", "korean": "ko", "dutch": "nl", "polish": "pl",
    "indonesian": "id", "turkish": "tr", "arabic": "ar", "hindi": "hi",
}


def _site_info(url: str) -> tuple[str, str]:
    """Return (identifier_prefix, publisher) for a story URL."""
    text = (url or "").lower()
    if "archiveofourown.org" in text or "ao3.org" in text:
        return "ao3", "archiveofourown.org"
    if "ficwad.com" in text:
        return "ficwad", "ficwad.com"
    if "royalroad.com" in text:
        return "royalroad", "royalroad.com"
    if "mediaminer.org" in text:
        return "mediaminer", "mediaminer.org"
    if "literotica.com" in text:
        return "literotica", "literotica.com"
    return "ffn", "fanfiction.net"


_HR_RE = re.compile(r"<hr\s*/?>|<hr\s[^>]*/?>", re.IGNORECASE)
_HR_STARS_REPLACEMENT = (
    '<div class="scenebreak" '
    'style="text-align:center;margin:1em 0">* * *</div>'
)

# Characters that legitimately appear in a text scene-break line:
# dashes, equals, tildes, asterisks, hashes, plus, punctuation, whitespace,
# and the letter ornaments ``oOxX0`` that fanfic authors type between
# dashes (``-x-x-x-``, ``oOoOo``). Keep in sync with ``tts._SCENE_BREAK_DECO_CHARS``.
_SCENE_BREAK_DECO_CHARS = set(
    "-=_~*#+.,;:!?/\\|"
    " \t"
    "oOxX0"
    "•·×"
    "★☆♦♠♥♣♢♤♡♧"
    "‡†§❦❧✦✧❖⟡"
    "⋆⸺⸻—–‒"
)

_ELLIPSIS_ONLY_RE = re.compile(r"^[\.…\s]+$")


def _is_divider_text(text: str) -> bool:
    """Detect a paragraph whose visible text is purely a scene-break
    divider.

    Accepts both short classic forms (``---``, ``***``, ``* * *``,
    ``oOo``) and the long run forms common on FFN (``-x-x-x-x-...``
    of 30, 60, 80+ chars). Conservative on ornamental-letter lines
    (``oOo`` / ``xXx``) so short words like ``ox`` don't trip it.
    """
    s = (text or "").strip()
    if len(s) < 3:
        return False
    if _ELLIPSIS_ONLY_RE.match(s):
        return False
    if not all(c in _SCENE_BREAK_DECO_CHARS for c in s):
        return False
    # Line contains at least one non-letter deco char (``-``, ``=``, ``*``,
    # ``#``, ``~``, ``.``, ``•``, etc.) — unambiguously a divider no matter
    # how long; real prose can't consist only of these.
    if any(c not in "oOxX0 \t" for c in s):
        # Long but still meaningful: even a 200-char run of ``-x-x-x-`` is
        # obviously a divider — authors don't type 200 chars of symbols
        # as prose.
        return True
    # Pure ornamental-letter line (only oOxX0 + whitespace): cap length
    # and require distinctive patterning so we don't eat "oO" or "OxO"
    # mid-prose.
    if len(s) > 40:
        return False
    # Mixed case (``oOo``, ``xXx``), zero-bearing (``o0o``), or pure-
    # uppercase X runs (``XXX`` / ``XXXX`` / ``X X X``). ``OOO`` and
    # lowercase ``ooo`` / ``xxx`` stay excluded — rating labels and
    # prose affection markers — see ``tts._is_scene_break_line``.
    has_lower = any(c in "ox" for c in s)
    has_upper = any(c in "OX" for c in s)
    has_zero = "0" in s
    if (has_lower and has_upper) or has_zero:
        return True
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 3 and all(c == "X" for c in letters):
        return True
    return False


def _apply_hr_as_stars(html: str) -> str:
    """Replace scene-break dividers with a centred ``* * *`` divider so
    readers whose stylesheet renders rules as a thin line don't miss
    them. Covers both ``<hr>`` tags and paragraph-level text dividers
    like ``-x-x-x-...`` or ``***`` that authors type in lieu of an
    actual horizontal rule."""
    from bs4 import BeautifulSoup

    # First pass: plain ``<hr>`` tags via fast regex — bs4 is expensive
    # and many chapters have no text dividers at all.
    html = _HR_RE.sub(_HR_STARS_REPLACEMENT, html)
    # Second pass: text-divider paragraphs. Only parse with bs4 when the
    # chapter has at least one short-ish paragraph that might be a
    # divider, keeping the common case cheap.
    if "<p" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    replaced = False
    for tag in soup.find_all(["p", "div"]):
        text = tag.get_text(" ", strip=True)
        if not text or not _is_divider_text(text):
            continue
        new = BeautifulSoup(_HR_STARS_REPLACEMENT, "html.parser")
        tag.replace_with(new)
        replaced = True
    return str(soup) if replaced else html


# Phrases that start an author's note paragraph on FFN (where notes are
# mingled with story text in the #storytext container). Kept conservative
# so we don't strip in-story prose that happens to start with "Note".
#
# Each label is required to be followed by a separator (colon / dash /
# end-of-strong-tag-style boundary) — that's what keeps a story sentence
# starting with the literal word "Disclaimer" or "Quick Note" out of the
# strip set: only the labelled-paragraph form qualifies.
_AN_MARKER_RE = re.compile(
    r"""^\s*
        [\[\(]?\s*                             # optional opening bracket
        (?:
            a\s*/\s*n                          # A/N  A / N
            | a\.\s*n\.?                       # A.N. / A. N.
            | an(?=\s*[:\-—–])                 # "AN" when followed by a separator
            | author[’'`´]?s?\s+note            # Author's Note / Author Note
            | author[’'`´]?s?\s+n\.?            # Author's N. (rare)
            | author[’'`´]?s?\s+comment(?:ary|s)?(?=\s*[:\-—–])  # "Author's Commentary:" / "Author's Comments:"
            | author[’'`´]?s?\s+(?:ramble|rambles|rambling|ramblings)(?=\s*[:\-—–])  # "Author's Rambles:" / "Author's Ramblings:"
            | from\s+the\s+author(?=\s*[:\-—–])  # "From the Author:"
            | disclaimer(?=\s*[:\-—–])         # "Disclaimer:" — extremely common
                                                # FFN chapter prefix that the
                                                # structural passes used to miss
                                                # because the post-divider para
                                                # was prose, not a Chapter banner.
            | quick\s+notes?(?=\s*[:\-—–])     # "Quick Note:" / "Quick Notes:"
            | side[\s\-]?notes?(?=\s*[:\-—–])  # "Side Note:" / "Sidenote:"
            | foot[\s\-]?notes?(?=\s*[:\-—–])  # "Footnote:" / "Foot Note:"
            | end[\s\-]?notes?(?=\s*[:\-—–])   # "End Note:" / "Endnote:"
            | (?:                              # Chapter-Note labels — "Post Chapter Note:",
                                                # "Pre Chapter Note:", "End Chapter Note:",
                                                # "Chapter Note:" (with optional
                                                # post/pre/end/start/final/closing prefix
                                                # and optional hyphen). Common FFN style:
                                                # CharmedMilliE, Karry Master, etc. label
                                                # the tail-block "Post Chapter Note:" which
                                                # the bare A/N regex never caught.
                (?:post|pre|end|start|opening|closing|final|ending)
                [\s\-]+
              )?
              chapter[\s\-]+notes?(?=\s*[:\-—–])
            | announcement(?=\s*[:\-—–])       # "Announcement:"
            | p\.?\s*s\.?(?=\s*[:\-—–])        # "P.S.:" / "PS:" / "P. S.:"
            | p\.?\s*p\.?\s*s\.?(?=\s*[:\-—–]) # "P.P.S.:" / "PPS:"
            | edit(?:ed)?(?:\s+\S{1,15})?(?=\s*[:\-—–])   # "Edit:", "EDIT:", "Edited 9/29:"
            | eta(?=\s*[:\-—–])                # "ETA:" (Edited To Add — fanfic convention)
            | update(?=\s*[:\-—–])             # "Update:" — chapter-prefix update notice
            | beta[’'`´]?d?\s+by               # "Beta'd by Name" / "Betaed by"
            | beta(?=\s*[:\-—–])               # "Beta:"
            | warning[s]?(?=\s*[:\-—–])        # "Warning:" / "Warnings:"
            | trigger\s+warning[s]?(?=\s*[:\-—–])  # "Trigger Warning:" / "Trigger Warnings:"
            | summary(?=\s*[:\-—–])            # "Summary:" — AO3-style chapter summary
                                                # (which is a non-narrative author block
                                                # bundled into the body on cross-posts)
            | recap(?=\s*[:\-—–])              # "Recap:" — when present as a labelled
                                                # paragraph (vs. embedded recap prose)
        )
        [\s:\-—–)\]\.]*                        # trailing punctuation
    """,
    re.IGNORECASE | re.VERBOSE,
)


# A paragraph the author types as a redundant chapter title inside the
# story body — sits between the intro-note divider and the first line of
# prose, or between the last line of prose and the outro-note divider.
# We only use it as a *corroborating* signal, never to strip on its own.
_TOP_BANNER_RE = re.compile(
    r"""^\s*
        (?:
            chapter\s+\d+(?:\s*[-–—:.]\s*.{0,80})?   # "Chapter 1" / "Chapter 1 - Title"
            | ch(?:\.|apter)?\s*\d+                  # "Ch 1" / "Ch. 1"
            | prologue | epilogue
            | part\s+\d+
        )\s*[.!]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_END_BANNER_RE = re.compile(
    r"""^\s*[-–—\s]*
        (?:
            end\s*(?:of\s+)?(?:chapter|ch\.?|part|story)?
            | fin
            | the\s+end
            | to\s+be\s+continued | tbc
        )
        [-–—\s.!]*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Common-sense structural cutoffs.
#
# A *header* paragraph that names the chapter ("Chapter 1",
# "Chapter One", "Prologue", "Si Vis Pacem - Chapter Three:") is a
# reliable boundary: everything before it is fic-front-matter
# (disclaimers, "I own nothing", author intros). An *end* paragraph
# ("-End", "End of Chapter", "Fin", "TBC") is the mirror: everything
# from it onward is back-matter (rambles, "thanks for reading",
# Patreon plugs, sign-offs).
#
# Both regexes use ``re.search`` (not ``match``) and a length cap so
# a paragraph reading "He turned to chapter five of his book." can't
# masquerade as a banner. ``\d+|<spelled>`` covers FFN's mix of
# digits and word numerals — the user reported "Si Vis Pacem -
# Chapter One:" specifically, which the digit-only ``_TOP_BANNER_RE``
# above missed.
_CHAPTER_HEADER_RE = re.compile(
    r"""\b(?:
        chapter\s+
        (?:
            \d+
            | one | two | three | four | five | six | seven | eight | nine | ten
            | eleven | twelve | thirteen | fourteen | fifteen | sixteen
            | seventeen | eighteen | nineteen | twenty
            | twenty[-\s]?(?:one|two|three|four|five|six|seven|eight|nine)
            | thirty | forty | fifty
        )
        | ch\.?\s*\d+
        | prologue | epilogue
        | part\s+\d+
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Length caps for the standalone-marker check. A real chapter header
# rarely exceeds 100 chars; an end marker is even tighter — usually a
# few characters. The caps keep mid-prose sentences ("the end of his
# patience", "she ran into Chapter House") from triggering a strip.
_CHAPTER_HEADER_MAX_LEN = 100
_END_MARKER_MAX_LEN = 60


def _is_chapter_header_paragraph(text: str) -> bool:
    """``True`` when the paragraph's whole text is a chapter banner.

    Two corroborating signals are required: the paragraph matches
    the chapter-header regex AND it's short enough to be standalone
    (story prose containing the words ``chapter five`` is much
    longer than a banner). False positives here strip real prose, so
    the gate is intentionally conservative.
    """
    s = (text or "").strip()
    if not s or len(s) > _CHAPTER_HEADER_MAX_LEN:
        return False
    return bool(_CHAPTER_HEADER_RE.search(s))


def _is_end_marker_paragraph(text: str) -> bool:
    """``True`` when the paragraph's whole text is an end-of-chapter
    marker. Same two-signal logic as the chapter header check, with
    a tighter length cap because outros are usually a few characters
    (``-End``, ``Fin``, ``TBC``). Reuses ``_END_BANNER_RE`` so the
    existing bottom-structural pass and this new one stay in
    lockstep on what counts as an end signal."""
    s = (text or "").strip()
    if not s or len(s) > _END_MARKER_MAX_LEN:
        return False
    return bool(_END_BANNER_RE.match(s))

# Phrases that almost always appear in an author's note and virtually
# never appear in narrative prose. Multi-word where possible — single
# words would misfire (``patron`` shows up in fantasy prose, ``review``
# in board-meeting scenes). Kept lowercase; the checker lowercases the
# candidate text once per block.
_NOTE_KEYWORDS = (
    "patreon",
    "pat re on",          # the Kairomaru-style anti-linkify spelling
    "ko-fi",
    "kofi",
    "please review",
    "please favorite", "please favourite",
    "please follow",
    "leave a review",
    "leave a comment", "drop a comment",
    "review and", "favorite and", "favourite and",
    "thanks for reading", "thank you for reading",
    "hope you enjoyed", "hope you enjoy",
    "next chapter", "next update", "until next",
    "keep reading to find out",
    "let me know what you think",
    "check out my", "check out my profile", "on my profile",
    "subscribe", "subscribers",
    "author's note", "author note", "a/n",  # belt-and-braces: the
    # prefix pass catches these when they *start* the paragraph, this
    # list catches them when they're buried mid-paragraph.
    # Ownership disclaimers — virtually always part of an A/N block,
    # the wording is a near-template across fandom.
    "disclaimer",
    "i do not own", "i don't own", "i own nothing",
    "all rights belong", "rights belong to",
    "credit goes to", "credit to the author",
    # Beta/proofreader credits.
    "beta'd by", "beta-d by", "betaed by",
    "thank you to my beta", "thanks to my beta",
)


# Subset of ``_NOTE_KEYWORDS`` that's so strongly indicative of an
# author's note that the structural pass treats it as a *hard* signal —
# enough to drop a pre-divider all-bold block on its own without
# requiring a Chapter-banner paragraph after the divider. Keep this
# list ruthlessly tight: anything that can plausibly appear in narrative
# prose belongs in the soft list above, not here. (For example
# ``thanks for reading`` is real-prose-plausible — a character
# thanking another in-world; ``patreon`` and ``i do not own`` are not.)
_HARD_NOTE_KEYWORDS = (
    "patreon",
    "pat re on",
    "ko-fi", "kofi",
    "disclaimer",
    "i do not own", "i don't own", "i own nothing",
    "beta'd by", "beta-d by", "betaed by",
    "leave a review",
    "please review",
    "please favorite", "please favourite",
    "author's note", "author note", "a/n",
)


def _block_has_hard_note_keyword(items):
    """Stricter sibling of :func:`_block_has_note_keyword`. Returns True
    when any paragraph contains a phrase from ``_HARD_NOTE_KEYWORDS``.

    Used by the relaxed pre-divider pass: a single all-bold pre-divider
    block with a hard signal is enough to drop the block without the
    usual post-divider banner requirement, because the combination of
    "fully bold" + "patreon/disclaimer/I do not own" is virtually never
    real prose.
    """
    for kind, node in items:
        text = node.get_text(" ", strip=True) if kind == "tag" else str(node)
        if not text:
            continue
        lower = text.lower()
        if any(kw in lower for kw in _HARD_NOTE_KEYWORDS):
            return True
    return False


def _block_has_note_keyword(items):
    """Return True if any paragraph's text in ``items`` (a slice of the
    top_level list produced by ``strip_note_paragraphs``) contains a
    note keyword."""
    for kind, node in items:
        text = node.get_text(" ", strip=True) if kind == "tag" else str(node)
        if not text:
            continue
        lower = text.lower()
        if any(kw in lower for kw in _NOTE_KEYWORDS):
            return True
    return False


def _is_fully_bold(tag):
    """True if every visible text node inside ``tag`` has a ``<strong>``
    or ``<b>`` ancestor *within* the tag. Authors who fence their notes
    with dividers almost always bold the entire note for emphasis; real
    prose mixes bold words into plain text, so bare-text presence is a
    strong negative signal.
    """
    bold_names = {"strong", "b"}
    saw_text = False
    for text_node in tag.find_all(string=True):
        s = str(text_node).strip()
        if not s:
            continue
        saw_text = True
        parent = text_node.parent
        has_bold = False
        while parent is not None and parent is not tag:
            if getattr(parent, "name", None) in bold_names:
                has_bold = True
                break
            parent = parent.parent
        if not has_bold:
            return False
    return saw_text


def _block_is_all_bold(items):
    """True if every tag paragraph in ``items`` is fully bold. Bare
    NavigableString items count against (can't be bold)."""
    saw_tag = False
    for kind, node in items:
        if kind != "tag":
            return False
        saw_tag = True
        if not _is_fully_bold(node):
            return False
    return saw_tag


def strip_note_paragraphs(html: str) -> str:
    """Drop paragraph-level author's notes from chapter HTML.

    Three passes, each independent:

    1. **Prefix pass** (conservative): paragraphs whose visible text
       starts with ``A/N``, ``AN:``, ``Author's Note``, etc. Matches
       only when the author explicitly labelled the paragraph.
    2. **Top structural pass**: when the chapter has a scene-break
       divider *and* the paragraph immediately after it is a chapter-
       title banner (``Chapter 1 - Title``, ``Prologue``), treat the
       content before the divider as an author-note preamble. Only
       fires when the pre-divider block also shows a note signal —
       either every paragraph is fully bold, or at least one
       paragraph contains a note keyword (``patreon``, ``thanks for
       reading``, ``leave a review``, etc.). Two-signal gate keeps
       innocent openings (a fic that starts with a flashback ``<hr>``)
       intact.
    3. **Bottom structural pass**: when the last scene-break divider
       is followed by at least one paragraph *and* that trailing
       block contains a note keyword, drop the divider and everything
       after it. Also pulls in a preceding ``-End Chapter-`` style
       banner so the visible chapter doesn't end on one. Keyword-only
       gate because outros rarely have a banner analogous to the
       top's ``Chapter N`` signal.

    Chapters without any divider go through unchanged.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Pass 1: prefix-based stripping (safe, label-only).
    for tag in soup.find_all(["p", "div", "blockquote"]):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if _AN_MARKER_RE.match(text):
            tag.decompose()

    # Build a top-level view for the structural passes so we can index
    # into dividers without re-scanning after deletions.
    top_level = []
    for ch in list(soup.children):
        if isinstance(ch, NavigableString):
            if ch.strip():
                top_level.append(("text", ch))
            continue
        if isinstance(ch, Tag):
            top_level.append(("tag", ch))

    def _item_is_divider(item):
        kind, node = item
        if kind == "tag" and node.name == "hr":
            return True
        if kind == "tag":
            text = node.get_text(" ", strip=True)
            if text and _is_divider_text(text):
                return True
        return False

    divider_indexes = [i for i, it in enumerate(top_level) if _item_is_divider(it)]

    def _drop(item):
        kind, node = item
        if kind == "tag":
            node.decompose()
        else:
            node.extract()

    top_drop_end = -1  # last index the top pass consumed (-1 = untouched)

    # Pass 2-pre (head): chapter-banner cutoff. If the chapter has a
    # standalone "Chapter N" / "Chapter Three" / "Prologue" header
    # paragraph in its top half, everything before and including
    # that header is fic-front-matter (disclaimers, "I own nothing",
    # author intros). Strip it. Two-signal gate: header-regex match
    # AND positional (top half), so a flashback titled "Chapter Five"
    # mid-prose can't trigger a chapter-gutting strip.
    n_top = len(top_level)
    if n_top >= 4:
        head_limit = n_top // 2
        for i, item in enumerate(top_level[:head_limit]):
            kind, node = item
            if kind != "tag":
                continue
            text = node.get_text(" ", strip=True)
            if text and _is_chapter_header_paragraph(text):
                for it in top_level[: i + 1]:
                    _drop(it)
                top_drop_end = i
                break

    # Pass 2a: top structural — divider + Chapter banner + note signal.
    if divider_indexes:
        first = divider_indexes[0]
        banner_idx = None
        if first + 1 < len(top_level):
            kind, node = top_level[first + 1]
            if kind == "tag":
                banner_text = node.get_text(" ", strip=True)
                if banner_text and _TOP_BANNER_RE.match(banner_text):
                    banner_idx = first + 1

        if banner_idx is not None:
            pre = top_level[:first]
            if pre and (
                _block_is_all_bold(pre) or _block_has_note_keyword(pre)
            ):
                for item in top_level[: banner_idx + 1]:
                    _drop(item)
                top_drop_end = banner_idx

    # Pass 2b: top structural relaxed — divider + small all-bold pre-
    # block + hard note keyword, with no banner requirement after the
    # divider. Triggered by FFN's overwhelmingly common "<p><strong>
    # Disclaimer: I don't own X</strong></p><hr>story prose" shape,
    # which the banner-gated pass refuses because the post-divider
    # paragraph is plain story content, not a "Chapter N" line. Three
    # corroborating signals (≤3-paragraph pre-block + fully bold +
    # hard keyword) keep this from misfiring on a dramatic bold line
    # before a flashback divider.
    if divider_indexes and top_drop_end < 0:
        first = divider_indexes[0]
        pre = top_level[:first]
        # The cap stops us from swallowing several paragraphs of
        # story prose if a dramatic narrative beat happens to be
        # bolded — real disclaimers are 1–2 paragraphs at most.
        _MAX_PRE_DIVIDER_PARAGRAPHS = 3
        if (
            pre
            and len(pre) <= _MAX_PRE_DIVIDER_PARAGRAPHS
            and _block_is_all_bold(pre)
            and _block_has_hard_note_keyword(pre)
        ):
            # Drop the pre-block and the divider itself so the chapter
            # opens cleanly on the first prose paragraph.
            for item in top_level[: first + 1]:
                _drop(item)
            top_drop_end = first

    # Pass 3: bottom structural — needs divider + post-block note keyword.
    if divider_indexes:
        last = divider_indexes[-1]
        # Skip if the top pass already consumed (or overlaps) this divider.
        if last > top_drop_end:
            post = top_level[last + 1:]
            if post and _block_has_note_keyword(post):
                outro_start = last
                if last - 1 > top_drop_end:
                    kind, node = top_level[last - 1]
                    if kind == "tag":
                        banner_text = node.get_text(" ", strip=True)
                        if banner_text and _END_BANNER_RE.match(banner_text):
                            outro_start = last - 1
                for item in top_level[outro_start:]:
                    _drop(item)

    # Pass 4 (tail): end-marker cutoff. If the chapter has a
    # standalone "-End" / "Fin" / "TBC" paragraph past the first
    # 25%, everything from there onward is back-matter — outro
    # rambles, "thanks for reading", Patreon plugs, sign-offs.
    # Two-signal gate: end-marker regex match AND positional
    # (past the first quarter), so a sentence like "the end of an
    # era" near the start can't trigger.
    #
    # Forward walk so the *first* marker past the floor wins: the
    # marker is a structural separator the author placed before
    # their A/N, and the A/N itself can contain phrases the regex
    # would also match (e.g. "to be continued in the sequel").
    # Cutting at the first match keeps the entire post-marker A/N
    # block in the strip.
    #
    # 25% (vs. the chapter-header rule's 50%) because end markers
    # appear naturally in the trailing region of any chapter while
    # chapter headers MUST be near the top to be banners — looser
    # gate is safe for the tail, would over-fire on a leading
    # flashback labelled "Chapter Five".
    if n_top >= 4:
        bottom_floor = max(1, n_top // 4)
        for i in range(bottom_floor, n_top):
            kind, node = top_level[i]
            if kind != "tag":
                continue
            # Skip nodes already dropped by an earlier pass.
            if not getattr(node, "name", None):
                continue
            text = node.get_text(" ", strip=True)
            if text and _is_end_marker_paragraph(text):
                for it in top_level[i:]:
                    _drop(it)
                break

    return str(soup)


# ── LLM author's-note backstop (HTML pipeline) ────────────────────


# Threshold above which the LLM's flag set looks like a hallucination
# rather than an honest A/N pass — most chapters are >80% prose, so a
# classifier saying "drop more than two-fifths of this chapter" is
# almost always wrong about a chunk of it.
_LLM_AN_RUNAWAY_THRESHOLD = 0.40

# Re-classify cap during the verification round. Anything that survives
# this stricter pass is what we actually drop. Tighter than the first
# pass because the verification prompt asks for high confidence only.
_LLM_AN_VERIFY_THRESHOLD = 0.40

# Hard ceiling on the post-verification flag rate. The verification
# round normally trims a runaway first pass back to a sensible subset,
# but on rare inputs the model agrees with its own hallucination and
# returns the same flag set on both passes — a 95-paragraph chapter
# came back 95/95 true on every round during the diagnostic that
# produced this guard. When verification keeps more than this fraction
# of the chapter we treat the LLM as having failed entirely on this
# chapter and fall through to regex-only A/N stripping. Set higher
# than the first-pass runaway threshold because verification has
# already seen the suspect set once; if it still wants to drop more
# than this much, it isn't a verification round, it's a second
# rubber-stamp. Defense in depth on top of the per-batch chunking in
# ``classify_authors_notes_via_llm``.
_LLM_AN_VERIFY_KEEP_CEILING = 0.85

# Don't bother classifying chapters with too few paragraphs — there's
# nothing for a backstop to catch and the round-trip is pure latency.
_LLM_AN_MIN_PARAGRAPHS = 4


def _llm_an_cache_path(site_name: str, story_id) -> Path | None:
    """Return the on-disk cache file for LLM A/N classifications, or
    None if the cache directory can't be created."""
    try:
        from .scraper import _default_cache_dir
    except ImportError:  # pragma: no cover — defensive
        return None
    try:
        base = _default_cache_dir() / "llm_an"
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(story_id))
    return base / f"{site_name}_{safe_id}.json"


def _llm_an_cache_key(paragraphs: list[str], model: str) -> str:
    """Stable cache key for a chapter's paragraph list under a given
    model. Hashes the joined text so re-classifying re-runs only when
    the chapter content actually changes (the regex pre-pass is
    deterministic, so its output is stable for stable inputs)."""
    import hashlib

    h = hashlib.sha1(usedforsecurity=False)
    for p in paragraphs:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x1e")  # record separator so neighbouring paras don't blur
    h.update(b"\x1d")
    h.update((model or "").encode("utf-8", errors="replace"))
    return h.hexdigest()


def _llm_an_load_cache(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _llm_an_save_cache(path: Path | None, data: dict) -> None:
    if path is None:
        return
    try:
        import json
        path.write_text(
            json.dumps(data, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError:
        pass


_LLM_AN_VERIFY_PROMPT = (
    "You are an editor preparing fanfiction for clean reading. The "
    "user wants the actual story text, not author commentary. Earlier "
    "you flagged the paragraphs below as author's notes — but the "
    "flag rate was high enough that we want a second look before "
    "dropping that much content. Re-classify each numbered paragraph "
    "with HIGH CONFIDENCE only: mark `true` only if the paragraph "
    "is unambiguously author commentary (disclaimer / Patreon plug / "
    "thanks for reviews / update schedule / response to reader / "
    "translator note). When in doubt, mark `false` and let the "
    "paragraph stay in the story. Respond with ONLY a JSON object "
    'whose keys are paragraph numbers (as strings) and values are '
    'booleans. Example: {"1": true, "2": false}.'
)


def _llm_an_verify(
    paragraphs: list[str],
    flagged: set[int],
    *,
    llm_config: dict,
) -> set[int]:
    """Send the flagged subset back to the LLM with a stricter prompt.

    Used when the first-pass flag rate exceeds
    ``_LLM_AN_RUNAWAY_THRESHOLD`` — rather than discarding every flag
    on the assumption that the model misread the chapter, we ask it
    to re-decide on just the suspect paragraphs with explicit
    "high confidence only" instructions. The intersection wins:
    a paragraph has to fail both passes to be dropped.

    Falls back to ``set()`` (i.e., drop nothing) on any transport or
    parse failure, mirroring the regex-only behaviour that's the
    contract for a misconfigured backend.
    """
    if not flagged or not llm_config:
        return set()
    from . import attribution

    subset_indices = sorted(flagged)
    subset = [paragraphs[i] for i in subset_indices]

    # Re-use the existing classifier infrastructure for the round-trip
    # but with the stricter system prompt. The classifier accepts a
    # custom system prompt via the prompt_override hook so we don't
    # have to duplicate the JSON-parse / endpoint plumbing.
    second_flagged = attribution.classify_authors_notes_via_llm(
        subset,
        llm_config=llm_config,
        system_prompt_override=_LLM_AN_VERIFY_PROMPT,
    )
    # Map second-pass indices (which are 0-based on ``subset``) back
    # onto the original chapter-paragraph indices.
    return {subset_indices[i] for i in second_flagged}


def strip_an_via_llm(
    html: str,
    *,
    llm_config: dict | None,
    site_name: str | None = None,
    story_id=None,
    chapter_number: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> str:
    """Drop paragraph-level author's notes the regex pass missed.

    Runs after :func:`strip_note_paragraphs` so the cheap regex catches
    the easy 80% (explicit ``A/N:`` labels, structural pre/post-divider
    blocks) without burning LLM tokens. The LLM picks up the disguised
    cases — outros without keyword hits, mid-chapter shout-outs, the
    author posting a "edit: thanks for the corrections!" without any
    of the structural cues we look for.

    Behaviour:

    * No-op when ``llm_config`` is None or empty.
    * No-op for chapters with fewer than ``_LLM_AN_MIN_PARAGRAPHS``
      top-level paragraphs.
    * Two-round verification when the first pass flags more than
      ``_LLM_AN_RUNAWAY_THRESHOLD`` of the chapter — the second
      round uses a stricter "high confidence only" prompt and only
      paragraphs surviving both rounds get dropped. This is the
      "extra verification before declaring the chapter worthless"
      gate.
    * Per-story disk cache keyed by chapter content hash + model
      name. Re-exporting the same story doesn't re-spend tokens on
      unchanged chapters; bumping the model invalidates the cache
      naturally since the model name is part of the key.

    Network and parse failures degrade silently to "no change" — the
    LLM is purely additive on top of the regex, never load-bearing.
    """
    if not html or not llm_config:
        return html
    provider = llm_config.get("provider", "")
    model = llm_config.get("model", "")
    if not provider or not model:
        return html

    soup = BeautifulSoup(html, "html.parser")
    paragraph_tags: list = []
    paragraph_texts: list[str] = []
    for ch in list(soup.children):
        if not isinstance(ch, Tag):
            continue
        if ch.name not in {"p", "blockquote", "div"}:
            continue
        text = ch.get_text(" ", strip=True)
        if not text:
            continue
        paragraph_tags.append(ch)
        paragraph_texts.append(text)

    if len(paragraph_texts) < _LLM_AN_MIN_PARAGRAPHS:
        return html

    cache_path = (
        _llm_an_cache_path(site_name, story_id)
        if site_name and story_id is not None
        else None
    )
    cache = _llm_an_load_cache(cache_path)
    cache_key = _llm_an_cache_key(paragraph_texts, model)
    chapter_label = (
        f"chapter {chapter_number}" if chapter_number is not None else "chapter"
    )

    cached = cache.get(cache_key)
    flagged: set[int]
    # Track whether we already emitted a verification-round summary.
    # The verification message ("kept N/M flags" / "dropped every
    # flag") IS the outcome line for that path, so we don't follow
    # it with a redundant "stripped X paragraphs" message below.
    outcome_already_logged = False

    if isinstance(cached, list):
        flagged = {int(i) for i in cached if isinstance(i, int)}
        _emit(progress, f"  [llm-an] {chapter_label}: cache hit")
    else:
        from . import attribution
        suffix = ""
        timeout_s = attribution._llm_request_timeout_s()
        suffix += f" (timeout {timeout_s}s"
        if provider == "ollama":
            endpoint = attribution._llm_normalize_endpoint(
                provider, llm_config.get("endpoint"),
            )
            runtime = attribution._llm_ollama_runtime(endpoint, model)
            if runtime:
                suffix += f", {runtime}"
        suffix += ")"
        _emit(
            progress,
            f"  [llm-an] {chapter_label}: classifying via {provider}/{model}{suffix}",
        )
        first = attribution.classify_authors_notes_via_llm(
            paragraph_texts, llm_config=llm_config,
        )
        # Runaway guard: too many flags on a single chapter is the
        # classifier's failure mode (occasional models read a long
        # chapter as one giant author's note when the opening
        # paragraph is unusually meta). Verify before acting.
        ratio = len(first) / len(paragraph_texts) if paragraph_texts else 0.0
        if first and ratio > _LLM_AN_RUNAWAY_THRESHOLD:
            _emit(
                progress,
                f"  [llm-an] {chapter_label}: first pass flagged "
                f"{len(first)}/{len(paragraph_texts)} paragraphs "
                f"({ratio:.0%}); running verification round",
            )
            flagged = _llm_an_verify(
                paragraph_texts, first, llm_config=llm_config,
            )
            keep_ratio = (
                len(flagged) / len(paragraph_texts)
                if paragraph_texts else 0.0
            )
            if flagged and keep_ratio > _LLM_AN_VERIFY_KEEP_CEILING:
                _emit(
                    progress,
                    f"  [llm-an] {chapter_label}: verification kept "
                    f"{len(flagged)}/{len(paragraph_texts)} flag(s) "
                    f"({keep_ratio:.0%}); rejecting as runaway, "
                    "falling back to regex",
                )
                flagged = set()
            elif not flagged:
                _emit(
                    progress,
                    f"  [llm-an] {chapter_label}: verification dropped "
                    "every flag — keeping chapter intact",
                )
            else:
                _emit(
                    progress,
                    f"  [llm-an] {chapter_label}: verification kept "
                    f"{len(flagged)}/{len(first)} flag(s)",
                )
            outcome_already_logged = True
        else:
            flagged = first
        cache[cache_key] = sorted(flagged)
        _llm_an_save_cache(cache_path, cache)

    # Boundary-only constraint for providers whose A/N classifier we
    # don't trust mid-chapter (currently Ollama). Drops any LLM flag
    # outside the head/tail windows BEFORE expand_an_block runs, so
    # the expansion sweep can't anchor on a hallucinated mid-chapter
    # flag. See ``attribution.constrain_an_to_boundaries``.
    if flagged:
        from . import attribution
        if attribution.should_constrain_an_to_boundaries(provider):
            before = len(flagged)
            flagged = attribution.constrain_an_to_boundaries(
                flagged, len(paragraph_texts),
            )
            if len(flagged) < before:
                _emit(
                    progress,
                    f"  [llm-an] {chapter_label}: dropped "
                    f"{before - len(flagged)} mid-chapter LLM flag(s) "
                    f"({provider} boundary-only mode)",
                )

    # Block expansion: the LLM picks individual paragraphs but A/Ns
    # come in contiguous runs at chapter head/tail, so any flagged
    # paragraph in those regions anchors a sweep of its neighbours.
    # Bounded by a 50% cap so a runaway expansion can't gut a real
    # chapter. See ``attribution.expand_an_block`` for the gates.
    if flagged:
        from . import attribution
        before = len(flagged)
        flagged = attribution.expand_an_block(flagged, len(paragraph_texts))
        if len(flagged) > before:
            _emit(
                progress,
                f"  [llm-an] {chapter_label}: expanded "
                f"{before} LLM flag(s) into {len(flagged)} "
                "paragraph(s) covering the head/tail A/N block",
            )

    # Always emit the outcome so the user sees what the LLM actually
    # decided per chapter — the bare "classifying via …" line leaves
    # them wondering whether anything got stripped or the call was a
    # no-op. Skip when the verification path already said it.
    if not outcome_already_logged:
        if flagged:
            _emit(
                progress,
                f"  [llm-an] {chapter_label}: stripped "
                f"{len(flagged)}/{len(paragraph_texts)} paragraph(s) "
                "as A/N",
            )
        else:
            _emit(
                progress,
                f"  [llm-an] {chapter_label}: no A/N paragraphs found",
            )

    if not flagged:
        return html

    # Drop in original order using the captured tag references.
    for idx in sorted(flagged):
        if 0 <= idx < len(paragraph_tags):
            paragraph_tags[idx].decompose()

    return str(soup)


def export_epub(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,
    strip_notes: bool = False,
    llm_config: dict | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    try:
        from ebooklib import epub
    except ImportError:
        raise ImportError(
            "EPUB export requires the 'ebooklib' package.\n"
            "Install it with: pip install 'ffn-dl[epub]'  (or pip install ebooklib)"
        )

    meta = story.metadata
    book = epub.EpubBook()
    site_prefix, publisher = _site_info(story.url)
    book.set_identifier(f"{site_prefix}-{story.id}")
    book.set_title(story.title)
    book.add_author(story.author)
    book.add_metadata("DC", "description", story.summary)
    book.add_metadata("DC", "source", story.url)
    book.add_metadata("DC", "publisher", publisher)

    lang = meta.get("language", "English")
    book.set_language(_LANG_CODES.get(lang.lower(), "en"))

    if "date_published" in meta:
        book.add_metadata("DC", "date", _format_epoch(meta["date_published"]))
    if "date_updated" in meta:
        dt = datetime.fromtimestamp(meta["date_updated"], tz=timezone.utc)
        book.add_metadata("DC", "modified", dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    tags = []
    if "genre" in meta:
        tags.extend(g.strip() for g in re.split(r"[/,]", meta["genre"]))
    if "characters" in meta:
        tags.extend(c.strip() for c in meta["characters"].split(","))
    if "rating" in meta:
        tags.append(f"Rated {meta['rating']}")
    if "status" in meta:
        tags.append(meta["status"])
    for tag in tags:
        if tag:
            book.add_metadata("DC", "subject", tag)

    if len(story.chapters) > 1:
        book.add_metadata(
            None, "meta", "", {"name": "calibre:series", "content": story.title}
        )
        book.add_metadata(
            None, "meta", "", {"name": "calibre:series_index", "content": "1"}
        )

    cover_url = meta.get("cover_url")
    if cover_url:
        result = _fetch_cover_image(cover_url)
        if result:
            img_bytes, media_type = result
            ext = "jpg" if "jpeg" in media_type else media_type.split("/")[-1]
            book.set_cover(f"images/cover.{ext}", img_bytes)

    css = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=(
            # Readable defaults for most ebook readers
            b"body{font-family:Georgia,serif;line-height:1.6}"
            # Book-style paragraphs: small top-margin, first-line indent
            # Readers that ship their own CSS will override this.
            b"p{margin:0 0 0.25em 0;text-indent:1.5em}"
            # First paragraph after a heading or section break has no indent
            b"h1+p,h2+p,h3+p,h4+p,hr+p,.scenebreak+p,.first+p,p.first{text-indent:0}"
            # Block quotes (used for summaries) keep their own indent
            b"blockquote{margin:1em 2em;font-style:italic}"
            b"blockquote p{text-indent:0}"
            # Metadata tables
            b"table{border-collapse:collapse;margin:1em 0}"
            b"th{text-align:right;padding:.25em 1em .25em 0;vertical-align:top;color:#555}"
            b"td{padding:.25em 0;vertical-align:top}"
            b"a{color:#36c}"
            # Scene breaks
            b".scenebreak{text-align:center;margin:1.5em 0;letter-spacing:.5em}"
            # Centred bits authors style with text-align or align=center
            b".center,[align=center]{text-align:center}"
            # Preserve author emphasis
            b"em,i{font-style:italic}"
            b"strong,b{font-weight:bold}"
        ),
    )
    book.add_item(css)

    # Title page with metadata
    title_page = epub.EpubHtml(
        title="Title Page", file_name="title.xhtml", lang="en"
    )
    rows = []
    for label, value in _meta_fields(story):
        val_esc = escape(value)
        if label == "Author" and story.author_url:
            cell = f'<a href="{escape(story.author_url)}">{val_esc}</a>'
        elif label == "Source":
            cell = f'<a href="{escape(value)}">{val_esc}</a>'
        elif label == "Summary":
            cell = f"<em>{val_esc}</em>"
        else:
            cell = val_esc
        rows.append(f"<tr><th>{label}</th><td>{cell}</td></tr>")
    title_html = (
        f"<h1>{escape(story.title)}</h1>\n"
        f'<table>\n{"".join(rows)}\n</table>'
    )
    title_page.content = title_html.encode("utf-8")
    title_page.add_item(css)
    book.add_item(title_page)

    epub_chapters = []
    consecutive_timeouts = 0
    for ch in story.chapters:
        ec = epub.EpubHtml(
            title=ch.title,
            file_name=f"chapter_{ch.number}.xhtml",
            lang="en",
        )
        heading = escape(ch.title)
        chapter_html, llm_disabled, consecutive_timeouts = (
            _prepare_chapter_html_with_llm_fallback(
                ch.html, hr_as_stars, strip_notes,
                llm_config=llm_config,
                site_name=site_prefix,
                story_id=story.id,
                chapter_number=ch.number,
                progress=progress,
                consecutive_timeouts=consecutive_timeouts,
            )
        )
        if llm_disabled:
            llm_config = None
        ec.content = f"<h2>{heading}</h2>\n{chapter_html}".encode("utf-8")
        ec.add_item(css)
        book.add_item(ec)
        epub_chapters.append(ec)

    book.toc = [title_page] + epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", title_page] + epub_chapters

    filename = format_filename(story, template) + ".epub"
    path = Path(output_dir) / filename
    # ``ebooklib`` insists on writing via a filesystem path rather than
    # a stream, so we hand it a temp path inside ``atomic_path`` and let
    # the context manager commit the rename on a clean exit. A crash or
    # exception from inside ``write_epub`` leaves the existing file (if
    # any) untouched instead of corrupting it.
    with atomic_path(path) as tmp:
        epub.write_epub(str(tmp), book)
    return path


EXPORTERS = {
    "txt": export_txt,
    "html": export_html,
    "epub": export_epub,
}


def check_format_deps(fmt: str) -> None:
    """Raise ImportError with an install hint if the exporter for `fmt`
    needs an optional dependency that isn't installed. Cheap to call —
    meant as a pre-flight check before a long download."""
    if fmt == "epub":
        try:
            import ebooklib  # noqa: F401
        except ImportError:
            raise ImportError(
                "EPUB export requires the 'ebooklib' package.\n"
                "Install it with: pip install 'ffn-dl[epub]'  (or pip install ebooklib)"
            )
    elif fmt == "audio":
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            raise ImportError(
                "Audiobook export requires the 'edge-tts' package.\n"
                "Install it with: pip install 'ffn-dl[audio]'  (or pip install edge-tts)"
            )
