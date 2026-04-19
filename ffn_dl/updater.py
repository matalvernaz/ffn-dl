"""Update mode — count chapters in existing files, detect new chapters."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class FileMetadata:
    """Metadata read from an existing story file.

    Populated on best-effort basis — readers return whatever they can
    find. Callers inspect individual fields and decide how to handle
    missing data. Format-agnostic: same shape for EPUB, HTML, TXT.
    """

    source_url: str | None = None
    title: str | None = None
    author: str | None = None
    fandoms: list[str] = field(default_factory=list)
    rating: str | None = None
    status: str | None = None
    chapter_count: int = 0
    format: str = ""


def count_chapters(filepath: Path | str) -> int:
    """Count chapters in an existing export file."""
    path = Path(filepath)
    suffix = path.suffix.lower()

    if suffix == ".html":
        text = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        return len(soup.find_all("div", class_="chapter"))

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        return len(re.findall(r"^--- .+ ---$", text, re.MULTILINE))

    if suffix == ".epub":
        try:
            from ebooklib import epub

            book = epub.read_epub(str(path))
            return sum(
                1
                for item in book.get_items()
                if hasattr(item, "file_name")
                and item.file_name.startswith("chapter_")
            )
        except Exception as exc:
            # ebooklib has a broad exception surface (EpubException,
            # zipfile errors, etc.); log and fall through so one bad
            # file doesn't kill a whole update-all run.
            logger.debug("count_chapters(%s) failed: %s", path, exc)
            return 0

    return 0


def extract_status(filepath: Path | str) -> str:
    """Return the story's completion status ('Complete' / 'In-Progress' / '')
    by reading the metadata block of an ffn-dl export. Empty string if not
    recognisable, so callers can treat unknown as "not complete."
    """
    path = Path(filepath)
    if not path.exists():
        return ""
    suffix = path.suffix.lower()

    if suffix == ".html":
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"<th>Status</th><td>([^<]+)</td>", text)
        if match:
            return match.group(1).strip()
        return ""

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^Status:\s*(.+)$", text, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    if suffix == ".epub":
        try:
            from ebooklib import epub
            book = epub.read_epub(str(path))
            # Status lands in the description block we render into the title
            # page. ebooklib exposes the first-chapter / title-page HTML via
            # get_items; scan the first HTML item's body.
            for item in book.get_items():
                if not hasattr(item, "file_name"):
                    continue
                if not item.file_name.startswith("title"):
                    continue
                body = item.content.decode("utf-8", errors="replace")
                match = re.search(r"<th>Status</th><td>([^<]+)</td>", body)
                if match:
                    return match.group(1).strip()
        except Exception as exc:
            logger.debug("extract_status(%s) failed: %s", path, exc)

    return ""


def extract_source_url(filepath: Path | str) -> str:
    """Read an existing export file and extract the source URL."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".html":
        match = re.search(
            r'<th>Source</th><td><a href="([^"]+)">', text
        )
        if match:
            return match.group(1)

    if suffix == ".txt":
        match = re.search(r"^Source:\s*(https?://\S+)", text, re.MULTILINE)
        if match:
            return match.group(1)

    if suffix == ".epub":
        try:
            from ebooklib import epub

            book = epub.read_epub(str(path))
            dc = book.metadata.get("http://purl.org/dc/elements/1.1/", {})
            sources = dc.get("source", [])
            if sources:
                return sources[0][0]
        except Exception as exc:
            logger.debug("extract_source_url(%s) epub read failed: %s", path, exc)

    # Fallback: look for any supported story URL anywhere in the body.
    from .sites import extract_story_url
    found = extract_story_url(text)
    if found:
        return found

    raise ValueError(
        f"Could not find a source URL in {path.name}. "
        "Is this a file exported by ffn-dl?"
    )


# ── Rich metadata extraction ──────────────────────────────────────
#
# extract_metadata() is the format-agnostic reader used by the library
# manager. It returns a FileMetadata with whatever can be parsed from
# any of: ffn-dl's own exports, FanFicFare output, FicHub output, or
# generic files where only a URL and filename are recoverable.


# Metadata-table labels that are NOT fandom/category. Used to separate
# fandom subjects from other tag-like fields when reading EPUB dc:subject
# entries (ffn-dl embeds genre/characters/rating/status there too).
_NON_FANDOM_LABELS = {
    "complete", "in-progress", "in progress", "incomplete", "abandoned",
    "ongoing", "hiatus",
}
_NON_FANDOM_PREFIXES = ("rated ", "rating:", "status:")


def _looks_like_fandom(subject: str) -> bool:
    """Heuristic: a dc:subject entry is a fandom unless it looks like
    genre/rating/status metadata or a relationship tag. FanFicFare
    mixes fandoms with relationships ("Harry/Hermione") in the same
    dc:subject field; the slash is a near-perfect discriminator since
    fandom names almost never contain one."""
    s = subject.strip()
    if not s:
        return False
    if "/" in s:
        # Relationship tags. Rare genuine crossovers like "Fandom A/B"
        # also get filtered, but those are inherently multi-fandom and
        # the Misc fallback handles them correctly anyway.
        return False
    lower = s.lower()
    if lower in _NON_FANDOM_LABELS:
        return False
    if any(lower.startswith(p) for p in _NON_FANDOM_PREFIXES):
        return False
    return True


# ---------------------------------------------------------------------------
# Label-table parsers for `_fill_from_html`.
#
# Different third-party downloaders embed fanfic metadata in different
# HTML shapes. We've observed the following in the wild:
#
#   * ffn-dl's own exports  — `<tr><th>Title</th><td>Value</td></tr>`
#   * FicLab (ficlab.com)   — same shape but lowercase labels
#   * AO3 native HTML       — `<dt>Label:</dt><dd>Value</dd>`
#   * Simple paragraph dump — `<p>Label: Value</p>`
#   * Bold-prefix dump      — `<b>Label:</b> Value<br/>`
#
# To keep lookups consistent across all of them, every parser normalises
# labels to lowercase-with-colon-stripped. `_fill_from_html` then looks
# up "title", "author", etc. (lowercase) regardless of source format.
# ---------------------------------------------------------------------------

# Regex for <a href=...>text</a> — callers strip the anchor wrapper from
# captured values to keep just the visible text (or, for a `source` row,
# the href itself, which `_extract_source_from_kv` handles separately).
_ANCHOR_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.DOTALL)

# Regex that strips every remaining tag after anchors have been unwrapped.
_TAG_STRIPPER_RE = re.compile(r"<[^>]+>")


def _normalise_label(label: str) -> str:
    """Return a lookup key for a metadata label.

    Lowercases and strips surrounding whitespace + trailing colons so
    ``"Title"`` and ``"title:"`` (AO3's `<dt>` shape) collapse to the
    same key. Used by every parser below so callers can do
    ``kv.get("title")`` without worrying about original casing.
    """
    return label.strip().rstrip(":").strip().lower()


def _clean_cell_value(raw: str) -> str:
    """Strip anchor wrappers and any other tags from a captured cell."""
    unwrapped = _ANCHOR_RE.sub(r"\1", raw)
    return _TAG_STRIPPER_RE.sub("", unwrapped).strip()


def _parse_kv_table(html: str) -> dict[str, str]:
    """Extract metadata rows from HTML into a lowercase-keyed dict.

    Handles three interchangeable shapes in a single pass so callers
    don't have to know which downloader produced the file:

    * ``<tr><th>Label</th><td>Value</td></tr>`` — ffn-dl and FicLab
    * ``<tr><td>Label</td><td>Value</td></tr>`` — some EPUB title pages
    * ``<dt>Label:</dt><dd>Value</dd>``         — AO3's native HTML export

    Returned keys are lowercase with trailing colons stripped, so
    ``"Title"``, ``"title"``, and ``"Title:"`` all yield ``"title"``.
    Values have anchor tags unwrapped to keep their text and have every
    other tag stripped so the consumer sees a clean string.
    """
    out: dict[str, str] = {}

    # <tr><th>...</th><td>...</td></tr> and the <tr><td>...</td><td>...</td>
    # variant (FicLab's EPUB title page uses the td/td form). Both start
    # from <tr>, so we merge them into one sweep with an alternation on
    # the label cell.
    table_row_re = re.compile(
        r"<tr[^>]*>\s*"
        r"(?:<th[^>]*>([^<]+)</th>|<td[^>]*>([^<]*)</td>)"
        r"\s*<td[^>]*>(.*?)</td>",
        re.DOTALL,
    )
    for match in table_row_re.finditer(html):
        label = match.group(1) or match.group(2) or ""
        value = _clean_cell_value(match.group(3))
        key = _normalise_label(label)
        if key and value:
            out[key] = value

    # <dt>Label:</dt><dd>Value</dd> — AO3's native HTML export structure.
    definition_re = re.compile(
        r"<dt[^>]*>([^<]+)</dt>\s*<dd[^>]*>(.*?)</dd>",
        re.DOTALL,
    )
    for match in definition_re.finditer(html):
        key = _normalise_label(match.group(1))
        value = _clean_cell_value(match.group(2))
        if key and value and key not in out:
            out[key] = value

    return out


# Metadata labels we expect in `<p>Label: value</p>` / `<b>Label:</b>
# value<br/>` dumps. Restricted to avoid picking up random "Note:" lines
# in chapter text as if they were metadata.
_PARAGRAPH_METADATA_LABELS = {
    "title", "author", "authorlink", "source", "sourcelink", "story", "storylink",
    "category", "categories", "fandom", "fandoms",
    "genre", "genres", "characters", "pairing", "pairings",
    "summary", "status", "rating", "chapters", "words",
    "updated", "published", "downloaded", "last updated",
    "tags", "language",
}


def _parse_paragraph_labels(html: str) -> dict[str, str]:
    """Extract ``<p>Label: value</p>`` / ``<b>Label:</b> value`` metadata.

    Covers the paragraph-dump output format used by several older
    browser-based FFN downloaders. Two passes:

    1. ``<p>Label: value</p>`` — look for a known label followed by a
       colon at the start of a paragraph. The rest of the paragraph is
       the value.
    2. ``<b>Label:</b> value`` — bold-prefixed labels, value runs until
       the next ``<br>`` (next line) or another ``<b>`` (next label).

    Labels are restricted to :data:`_PARAGRAPH_METADATA_LABELS` so
    chapter text that happens to start with a capitalised word + colon
    (common in dialogue tags) isn't mistaken for metadata.
    """
    out: dict[str, str] = {}

    # Alternation of recognised labels is built into the regex so we
    # can match in a single scan instead of O(N_paragraphs * N_labels).
    label_alternation = "|".join(sorted(_PARAGRAPH_METADATA_LABELS, key=len, reverse=True))

    paragraph_re = re.compile(
        rf"<p[^>]*>\s*(?:<(?:b|strong)[^>]*>)?\s*"
        rf"({label_alternation})\s*:\s*"
        rf"(?:</(?:b|strong)>)?\s*(.*?)</p>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in paragraph_re.finditer(html):
        key = _normalise_label(match.group(1))
        value = _clean_cell_value(match.group(2))
        if key and value and key not in out:
            out[key] = value

    # Bold-prefix dumps: `<b>Label:</b> value` where the value runs
    # until the next `<br>` or the next bolded label. ``re.DOTALL`` so
    # values can contain inline tags; the non-greedy ``.*?`` plus the
    # `<br>|<b>|</p>|\Z` stop set keeps a single paragraph from
    # absorbing the next one's content.
    bold_re = re.compile(
        rf"<(?:b|strong)[^>]*>\s*({label_alternation})\s*:\s*</(?:b|strong)>"
        rf"\s*(.*?)(?=<br|<(?:b|strong)[^>]*>|</p>|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    for match in bold_re.finditer(html):
        key = _normalise_label(match.group(1))
        value = _clean_cell_value(match.group(2))
        if key and value and key not in out:
            out[key] = value

    return out


# Labels (normalised, lowercase) that imply a fandom/category assignment.
# "tags" is intentionally excluded — FicLab dumps the entire FFN tag list
# (genres, characters, statuses) into a single `tags` row; picking a
# fandom out of that soup needs heuristics better left to Phase 4's
# review flow.
_FANDOM_LABELS = ("fandom", "fandoms", "category", "categories")

# Labels whose value is a chapter count we can trust as an integer.
_CHAPTER_COUNT_LABELS = ("chapters",)

# Labels (in order of preference) that carry a source URL when present.
# We prefer `source` over `storylink` because FicLab uses the former as
# the primary canonical URL; `storylink` shows up only in the bold-br
# paragraph dumps that also happen to have `source: FanFiction.net`
# (site name, not a URL) in a separate field.
_SOURCE_URL_LABELS = ("source", "storylink", "sourcelink")


def _parse_int(value: str) -> int:
    """Return an int from a possibly comma/whitespace-decorated value.

    Returns 0 on an unparseable value so callers can treat "no reliable
    count" and "zero" the same way without a try/except.
    """
    digits = re.sub(r"[^0-9]", "", value or "")
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


# Body-level chapter-count patterns. Used as a fallback when the
# structured metadata parsers don't produce a count.
#
# ``of\s+N\s+chapters`` matches phrases like
#   "Content: Chapter 1 to 50 of 50 chapters"   (bold-br dumps)
#   "... of 15 chapters (Complete)"             (FLAG / Simple-p variants)
# The match is anchored on the space-bounded ``of`` so chapter-body
# prose like "one of the chapters" can't collide with it.
_BODY_CHAPTERS_OF_RE = re.compile(
    r"\bof\s+(\d+)\s+chapters?\b", re.IGNORECASE,
)

# ``Chapters: 43/?`` / ``Chapters: 43/43`` — AO3's native HTML download
# format. The first number is the count of published chapters; the
# second is the planned total (``?`` when unknown). We only need the
# first.
_BODY_CHAPTERS_SLASH_RE = re.compile(
    r"\bChapters?:\s*(\d+)\s*/\s*(?:\d+|\?)", re.IGNORECASE,
)

# ``<a href="#chapter_N">`` — FLAG/flagfic.com's table-of-contents
# anchor convention. The highest N is the chapter count. Used as a
# last fallback for FLAG files that don't spell out "N chapters"
# anywhere in the body.
_TOC_CHAPTER_ANCHOR_RE = re.compile(
    r'<a\s+href="#chapter_(\d+)"', re.IGNORECASE,
)


def _fill_from_epub(path: Path, md: "FileMetadata") -> None:
    try:
        from ebooklib import epub
    except ImportError:
        logger.warning(
            "ebooklib not installed; EPUB metadata unavailable for %s", path
        )
        return

    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        logger.warning("Failed to read EPUB %s: %s", path, exc)
        return

    dc = book.metadata.get("http://purl.org/dc/elements/1.1/", {})
    titles = dc.get("title", [])
    creators = dc.get("creator", [])
    sources = dc.get("source", [])
    subjects = dc.get("subject", [])

    if titles:
        md.title = titles[0][0]
    if creators:
        md.author = creators[0][0]
    if sources:
        md.source_url = sources[0][0]

    md.fandoms = [s[0] for s in subjects if _looks_like_fandom(s[0])]

    # ffn-dl's own EPUBs embed genre/characters/rating/status as
    # dc:subject entries alongside (sometimes) real fandom tags.
    # When the title page has a structured Category field, treat it
    # as authoritative — it's what the originating scraper decided
    # to call the fandom — and drop the looser subject-derived list
    # for this file. Foreign EPUBs (FFF/FicHub) have no title page
    # in our format, so they keep the subject-derived fandoms.
    for item in book.get_items():
        if not hasattr(item, "file_name"):
            continue
        if not item.file_name.startswith("title"):
            continue
        body = item.content.decode("utf-8", errors="replace")
        kv = _parse_kv_table(body)
        # Labels are now normalised to lowercase (see _parse_kv_table).
        category = kv.get("category")
        if category:
            md.fandoms = [category]
        if not md.status:
            md.status = kv.get("status")
        if not md.rating:
            md.rating = kv.get("rating")
        break


def _merge_metadata_field(
    md: "FileMetadata", field_name: str, value: str | int | None,
) -> None:
    """Set ``md.<field_name>`` to ``value`` only if currently unset.

    Used so multiple format parsers (kv-table + paragraph) can contribute
    to the same :class:`FileMetadata` without the second parser clobbering
    a good value the first one already found.
    """
    if not value:
        return
    current = getattr(md, field_name, None)
    if current:
        return
    setattr(md, field_name, value)


def _fill_from_html(path: Path, md: "FileMetadata") -> None:
    """Populate ``md`` from an HTML file in any of the recognised formats.

    Tries every parser defined above and merges the first non-empty value
    per field. Labels are looked up lowercase (see :func:`_parse_kv_table`
    and :func:`_parse_paragraph_labels`) so ffn-dl's ``Title`` and FicLab's
    ``title`` both resolve.

    The caller leaves this function with ``md`` populated as best we can
    and falls back to :func:`extract_source_url` for the URL and
    :func:`count_chapters` for the chapter count if either is still
    missing.
    """
    text = path.read_text(encoding="utf-8", errors="replace")

    # Parse every supported HTML metadata shape into a single dict. Keys
    # are lowercase; first-wins precedence keeps a genuine <th>/<td>
    # row from being overwritten by a later paragraph-label match.
    kv = _parse_kv_table(text)
    paragraphs = _parse_paragraph_labels(text)
    merged: dict[str, str] = dict(paragraphs)
    merged.update(kv)  # kv has priority — more structured shape

    _merge_metadata_field(md, "title", merged.get("title") or merged.get("story"))
    _merge_metadata_field(md, "author", merged.get("author"))
    _merge_metadata_field(md, "status", merged.get("status"))
    _merge_metadata_field(md, "rating", merged.get("rating"))

    # Fandoms can live under any of several label aliases; take the
    # first populated one.
    for label in _FANDOM_LABELS:
        value = merged.get(label)
        if value and not md.fandoms:
            md.fandoms = [value]
            break

    # FicLab-style crossover recovery: FicLab has no dedicated fandom
    # field, but FFN's crossover tag format is stable — a single tag
    # reading ``"{FandomA} + {FandomB} Crossover"`` — and FicLab passes
    # it straight through in the ``tags`` row. Look for that exact
    # shape so crossovers in the user's ``misc/`` folder (where the
    # folder-fandom fallback won't fire) still get a meaningful fandom
    # string from the content itself.
    if not md.fandoms:
        tags_value = merged.get("tags", "")
        for raw_tag in tags_value.split(","):
            tag = raw_tag.strip()
            if tag.lower().endswith(" crossover"):
                md.fandoms = [tag]
                break

    # Chapter count from metadata when available — saves an expensive
    # DOM re-walk in count_chapters() and works on formats whose
    # chapter markup count_chapters can't parse.
    for label in _CHAPTER_COUNT_LABELS:
        value = merged.get(label)
        if value:
            count = _parse_int(value)
            if count > 0:
                _merge_metadata_field(md, "chapter_count", count)
                break

    # Body-level fallbacks for formats that don't carry a plain
    # ``Chapters: N`` field but still expose the count in prose:
    #   * bold-br dumps: ``Content: Chapter 1 to 50 of 50 chapters``.
    #     Also matches the FLAG/Simple-p variants that embed the same
    #     phrase elsewhere in the document.
    #   * AO3's native HTML export: ``Chapters: 43/?`` or ``43/43``
    #     inside the Stats block. The first number is the count of
    #     actually-published chapters.
    if not md.chapter_count:
        match = _BODY_CHAPTERS_OF_RE.search(text)
        if match:
            _merge_metadata_field(md, "chapter_count", int(match.group(1)))
    if not md.chapter_count:
        match = _BODY_CHAPTERS_SLASH_RE.search(text)
        if match:
            _merge_metadata_field(md, "chapter_count", int(match.group(1)))
    if not md.chapter_count:
        toc_nums = _TOC_CHAPTER_ANCHOR_RE.findall(text)
        if toc_nums:
            _merge_metadata_field(md, "chapter_count", max(int(n) for n in toc_nums))

    # Source URL: try the explicit `source`/`storylink` fields first
    # (structured), then fall through to extract_source_url() which
    # regex-matches any known URL pattern in the body.
    for label in _SOURCE_URL_LABELS:
        value = merged.get(label)
        if value and value.startswith(("http://", "https://")):
            _merge_metadata_field(md, "source_url", value)
            break

    # Last-resort title/author fallbacks. Covers three formats whose
    # metadata doesn't live in a kv-table or labelled paragraph:
    #
    #   * FLAG / flagfic.com: `<span id="crAuthor">Author</span>` and
    #     `<title>FLAG :: Title by Author</title>`.
    #   * AO3 native HTML download: title in the `<title>` tag as
    #     "Title - Author - Fandom" and bolded inside a `<p class="message">`.
    #   * span-class: `<span class="title">Title</span>` + author sibling.
    #
    # Only runs when the structured parsers above didn't already populate
    # the field — we never overwrite a trusted kv-table value with a
    # fallback guess.
    if not md.title or not md.author:
        _fill_title_author_fallbacks(text, md)


# Universal HTML metadata markers — present in most downloaders'
# output regardless of whether they use a kv-table, paragraph dump, or
# none of the above. Used as a final safety net so any unknown format
# still surfaces at least a title.
_META_AUTHOR_RE = re.compile(
    r'<meta\s+name="author"\s+content="([^"]+)"', re.IGNORECASE,
)
_META_OG_TITLE_RE = re.compile(
    r'<meta\s+property="og:title"\s+content="([^"]+)"', re.IGNORECASE,
)
_FIRST_H1_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.IGNORECASE | re.DOTALL)

# Titles that show up in these HTML elements often carry site-level
# branding like "Story — FanFiction" or "Title | Royal Road"; strip
# those suffixes so the user sees the actual story title.
_TITLE_BRANDING_SEPARATORS = (" | ", " — ", " – ", " :: ", " — ")
_TITLE_BRANDING_TRAILERS = (
    "fanfiction", "fanfiction.net", "archive of our own", "ao3",
    "royal road", "wattpad", "literotica", "ficwad", "mediaminer",
)


def _strip_title_branding(raw: str) -> str:
    """Drop trailing site-branding segments from a `<title>` tag value.

    E.g. ``"My Story | FanFiction"`` → ``"My Story"``. Preserves the
    full string if no separator matches so legitimate ``|`` characters
    in titles stay intact.
    """
    for separator in _TITLE_BRANDING_SEPARATORS:
        if separator not in raw:
            continue
        head, _, tail = raw.rpartition(separator)
        if tail.strip().lower() in _TITLE_BRANDING_TRAILERS:
            return head.strip()
    return raw.strip()


# <span id="crAuthor">Author Name</span> — used by the FLAG/flagfic.com
# downloader. Multiple crXXX spans exist per file (crAuthor, crTitle via
# the <h1>, crSource for URL), each uniquely identified by id.
_FLAG_AUTHOR_RE = re.compile(
    r'<span\s+id="crAuthor"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL,
)

# `<h1>Title by Author</h1>` — FLAG shows both title and author on the
# cover page in a single h1. Split on the last " by " to get the pieces.
_FLAG_COVER_H1_RE = re.compile(
    r'<h1[^>]*>(.*?)</h1>', re.IGNORECASE | re.DOTALL,
)

# <span class="title">...</span> / <span class="author">...</span>,
# observed in at least one third-party downloader.
_SPAN_CLASS_TITLE_RE = re.compile(
    r'<span\s+class="title"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL,
)
_SPAN_CLASS_AUTHOR_RE = re.compile(
    r'<span\s+class="author"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL,
)

# AO3 native HTML: <title>Title - Author - Fandom</title>. The chapter
# body often starts with a <p class="message"><b>Title</b>... blurb
# followed by author info.
_HTML_TITLE_TAG_RE = re.compile(
    r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL,
)
_AO3_NATIVE_SIGNATURE = "archiveofourown.org"


def _fill_title_author_fallbacks(html: str, md: "FileMetadata") -> None:
    """Best-effort title/author recovery from non-tabular HTML.

    Tries each fallback parser in order of specificity: FLAG
    (`<span id="crAuthor">` + `<h1>Title by Author</h1>`), span-class
    dumps, then AO3's native `<title>` tag. Only populates fields that
    are still empty on ``md`` — never overwrites a value the
    structured parsers already found.
    """
    # FLAG's `<span id="crAuthor">Name</span>` is a reliable author
    # marker. The cover <h1>Title by Author</h1> gives us the title.
    flag_author_match = _FLAG_AUTHOR_RE.search(html)
    if flag_author_match:
        _merge_metadata_field(md, "author", _clean_cell_value(flag_author_match.group(1)))
        # Find the cover h1 — skip "Copyright", "Summary", "Table of
        # Contents" etc. by requiring " by " to appear in the text.
        for h1_match in _FLAG_COVER_H1_RE.finditer(html):
            text = _clean_cell_value(h1_match.group(1))
            if " by " in text:
                candidate_title = text.rsplit(" by ", 1)[0].strip()
                if candidate_title:
                    _merge_metadata_field(md, "title", candidate_title)
                    break

    # `<span class="title">` / `<span class="author">` dumps.
    span_title = _SPAN_CLASS_TITLE_RE.search(html)
    if span_title:
        _merge_metadata_field(md, "title", _clean_cell_value(span_title.group(1)))
    span_author = _SPAN_CLASS_AUTHOR_RE.search(html)
    if span_author:
        _merge_metadata_field(md, "author", _clean_cell_value(span_author.group(1)))

    # AO3 native download. Only trigger if the body references AO3 so
    # we don't mis-parse a random other HTML file's <title> tag.
    if _AO3_NATIVE_SIGNATURE in html:
        title_tag_match = _HTML_TITLE_TAG_RE.search(html)
        if title_tag_match:
            raw = _clean_cell_value(title_tag_match.group(1))
            # AO3's pattern: "Title - Author - Fandom". Splitting on the
            # surrounding " - " gives us [title, author, fandom] when
            # present. If only one " - " exists we take the left side
            # as title and skip author — better to miss a field than
            # record the fandom as the author.
            parts = [p.strip() for p in raw.split(" - ")]
            if len(parts) >= 2 and parts[0]:
                _merge_metadata_field(md, "title", parts[0])
            if len(parts) >= 3 and parts[1]:
                _merge_metadata_field(md, "author", parts[1])

    # Universal fallbacks — run after every format-specific parser so
    # they only fill in what's still missing. <meta> tags and the first
    # <h1>/<title> are present in almost every HTML file, so these
    # catch unknown downloaders we haven't written explicit parsers for.
    if not md.author:
        author_meta = _META_AUTHOR_RE.search(html)
        if author_meta:
            _merge_metadata_field(md, "author", _clean_cell_value(author_meta.group(1)))

    if not md.title:
        og_title = _META_OG_TITLE_RE.search(html)
        if og_title:
            _merge_metadata_field(
                md, "title", _strip_title_branding(_clean_cell_value(og_title.group(1))),
            )

    if not md.title:
        h1_match = _FIRST_H1_RE.search(html)
        if h1_match:
            text = _clean_cell_value(h1_match.group(1))
            # Skip generic boilerplate headings common to downloader
            # cover pages — we want the story title, not the section.
            generic = {"copyright", "summary", "table of contents", "cover", "preface"}
            if text and text.lower().rstrip(":").strip() not in generic:
                _split_title_by_author(text, md)

    if not md.title:
        title_tag = _HTML_TITLE_TAG_RE.search(html)
        if title_tag:
            raw = _strip_title_branding(_clean_cell_value(title_tag.group(1)))
            _split_title_by_author(raw, md)


def _split_title_by_author(text: str, md: "FileMetadata") -> None:
    """Assign ``text`` to ``md.title``, splitting on ``" by "`` when present.

    Several downloaders expose both title and author in a single HTML
    element — ``<title>Story by Author</title>``, ``<h1>Story by Author</h1>``,
    or HPFFA's ``<div id="pagetitle"><a>Title</a> by <a>Author</a></div>``
    (which collapses to the same string after tag-stripping).

    We only split when the right-hand side looks like a plausible
    author name — short (≤60 chars), no newlines, no obvious sentence
    punctuation — so legitimate titles like ``"Life by the Seaside"``
    don't lose their tail. Author is only set when it was still empty;
    we never overwrite a value a structured parser already found.
    """
    if not text:
        return

    MAX_PLAUSIBLE_AUTHOR_LEN = 60

    # Use rsplit so a title like "A Day by the Sea by Jane Doe" splits
    # on the final " by " — the author is the trailing segment.
    if " by " in text:
        head, _, tail = text.rpartition(" by ")
        head = head.strip()
        tail = tail.strip()
        looks_like_author = (
            head
            and tail
            and "\n" not in tail
            and len(tail) <= MAX_PLAUSIBLE_AUTHOR_LEN
            # Plausible author names don't end in sentence punctuation.
            and tail[-1] not in ".!?"
        )
        if looks_like_author:
            _merge_metadata_field(md, "title", head)
            _merge_metadata_field(md, "author", tail)
            return

    _merge_metadata_field(md, "title", text)


def _fill_from_txt(path: Path, md: "FileMetadata") -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Header block runs until a ========== separator or the first blank
    # line before chapter content.
    header, _, _ = text.partition("=" * 60)
    if not header:
        header = text[:4000]

    for line in header.splitlines():
        m = re.match(r"^([A-Za-z][A-Za-z ]*?):\s*(.+)$", line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if label == "Title":
            md.title = value
        elif label == "Author":
            md.author = value
        elif label == "Category":
            md.fandoms.append(value)
        elif label == "Status":
            md.status = value
        elif label == "Rating":
            md.rating = value
        elif label == "Source" and value.startswith(("http://", "https://")):
            md.source_url = value


def extract_metadata(filepath: Path | str) -> "FileMetadata":
    """Read a story file and return whatever metadata can be recovered.

    Handles ffn-dl's own exports first-class. Reads structured metadata
    from FanFicFare and FicHub EPUBs (they embed dc:source and dc:subject
    the same way). Falls back to a URL-in-content regex if no structured
    source was found. Never raises on missing data — fields stay None
    or empty for callers to handle.
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    md = FileMetadata(format=suffix.lstrip("."))

    if suffix == ".epub":
        _fill_from_epub(path, md)
    elif suffix == ".html":
        _fill_from_html(path, md)
    elif suffix == ".txt":
        _fill_from_txt(path, md)

    if not md.source_url:
        try:
            md.source_url = extract_source_url(path)
        except (ValueError, FileNotFoundError):
            pass

    # Only count chapters from the DOM if the format parsers didn't
    # already extract a trustworthy count from the metadata — count_chapters
    # only understands ffn-dl's own `<div class="chapter">` markup, so
    # running it on a FicLab / paragraph-dump file returns 0 and would
    # overwrite the correct number we just parsed out of the metadata.
    if not md.chapter_count:
        md.chapter_count = count_chapters(path)
    return md
