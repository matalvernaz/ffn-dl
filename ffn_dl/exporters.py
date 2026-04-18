"""Export a Story to EPUB, HTML, or plain text."""

import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import Story

DEFAULT_TEMPLATE = "{title} - {author}"


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


def _prepare_chapter_html(html: str, hr_as_stars: bool, strip_notes: bool) -> str:
    """Apply optional chapter-level transformations in the right order."""
    if strip_notes:
        html = strip_note_paragraphs(html)
    if hr_as_stars:
        html = _apply_hr_as_stars(html)
    return html


def export_txt(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,  # accepted for signature parity; TXT always renders hr as "* * *"
    strip_notes: bool = False,
) -> Path:
    filename = format_filename(story, template) + ".txt"
    path = Path(output_dir) / filename

    with open(path, "w", encoding="utf-8") as f:
        for label, value in _meta_fields(story):
            f.write(f"{label}: {value}\n")
        f.write("=" * 60 + "\n")

        for ch in story.chapters:
            f.write(f"\n\n--- {ch.title} ---\n\n")
            html = strip_note_paragraphs(ch.html) if strip_notes else ch.html
            f.write(html_to_text(html))

    return path


def export_html(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,
    strip_notes: bool = False,
) -> Path:
    filename = format_filename(story, template) + ".html"
    path = Path(output_dir) / filename

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

    with open(path, "w", encoding="utf-8") as f:
        f.write(
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
            f.write(f"{row}\n")
        f.write("</table>\n<hr>\n")

        # Table of Contents
        f.write('<nav id="toc">\n<h2>Table of Contents</h2>\n<ol>\n')
        for i, ch in enumerate(story.chapters, 1):
            f.write(f'<li><a href="#chapter-{i}">{escape(ch.title)}</a></li>\n')
        f.write("</ol>\n</nav>\n<hr>\n")

        for i, ch in enumerate(story.chapters, 1):
            ch_title = escape(ch.title)
            f.write(f'<div class="chapter" id="chapter-{i}"><h2>{ch_title}</h2>\n')
            chapter_html = _prepare_chapter_html(ch.html, hr_as_stars, strip_notes)
            f.write(chapter_html)
            f.write("\n</div><hr>\n")

        f.write("</body>\n</html>\n")

    return path


def _fetch_cover_image(cover_url):
    """Download a cover image, returning (content_bytes, media_type) or None."""
    try:
        from curl_cffi import requests as curl_requests

        resp = curl_requests.get(cover_url, impersonate="chrome", timeout=15)
        if resp.status_code == 200 and len(resp.content) > 500:
            ct = resp.headers.get("content-type", "image/jpeg")
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
    has_lower = any(c in "ox" for c in s)
    has_upper = any(c in "OX" for c in s)
    has_zero = "0" in s
    return (has_lower and has_upper) or has_zero


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
_AN_MARKER_RE = re.compile(
    r"""^\s*
        [\[\(]?\s*                             # optional opening bracket
        (?:
            a\s*/\s*n                          # A/N  A / N
            | a\.\s*n\.?                       # A.N. / A. N.
            | an(?=\s*[:\-—–])                 # "AN" when followed by a separator
            | author[’'`´]?s?\s+note            # Author's Note / Author Note
            | author[’'`´]?s?\s+n\.?            # Author's N. (rare)
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
)


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

    # Pass 2: top structural — needs divider + banner + note signal.
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

    return str(soup)


def export_epub(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,
    strip_notes: bool = False,
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
    for ch in story.chapters:
        ec = epub.EpubHtml(
            title=ch.title,
            file_name=f"chapter_{ch.number}.xhtml",
            lang="en",
        )
        heading = escape(ch.title)
        chapter_html = _prepare_chapter_html(ch.html, hr_as_stars, strip_notes)
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
    epub.write_epub(str(path), book)
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
