"""Update mode — count chapters in existing files, detect new chapters."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup


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


def count_chapters(filepath):
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
        except Exception:
            return 0

    return 0


def extract_status(filepath) -> str:
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
        except Exception:
            pass

    return ""


def extract_source_url(filepath):
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
        except Exception:
            pass

    # Fallback: look for any supported story URL
    match = re.search(
        r"https?://(?:www\.)?("
        r"fanfiction\.net/s/\d+"
        r"|ficwad\.com/story/\d+"
        r"|(?:archiveofourown\.org|ao3\.org)/works/\d+"
        r"|royalroad\.com/fiction/\d+"
        r"|mediaminer\.org/fanfic/(?:view_st\.php/\d+|s/[^?#\s]+?/\d+)"
        r"|literotica\.com/s/[a-z0-9-]+"
        r")",
        text,
    )
    if match:
        return match.group(0)

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
    genre/rating/status metadata. Keeps ffn-dl-written rating/status
    subjects out of the fandom list without dropping real fandoms
    named things like 'Complete Works'."""
    s = subject.strip()
    if not s:
        return False
    lower = s.lower()
    if lower in _NON_FANDOM_LABELS:
        return False
    if any(lower.startswith(p) for p in _NON_FANDOM_PREFIXES):
        return False
    return True


def _parse_kv_table(html: str) -> dict[str, str]:
    """Extract <th>Label</th><td>Value</td> rows into a flat dict.
    Used for both ffn-dl HTML exports and ffn-dl EPUB title pages."""
    out = {}
    for m in re.finditer(
        r"<tr[^>]*>\s*<th[^>]*>([^<]+)</th>\s*<td[^>]*>(.*?)</td>",
        html,
        re.DOTALL,
    ):
        label = m.group(1).strip()
        # Strip anchor tags from the value while keeping href text
        value = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", m.group(2), flags=re.DOTALL)
        value = re.sub(r"<[^>]+>", "", value).strip()
        if label and value:
            out[label] = value
    return out


def _fill_from_epub(path: Path, md: "FileMetadata") -> None:
    try:
        from ebooklib import epub
    except ImportError:
        return

    try:
        book = epub.read_epub(str(path))
    except Exception:
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

    # ffn-dl's own EPUBs don't embed fandom as dc:subject — they embed
    # it in the title page's metadata table as "Category". Read it back.
    for item in book.get_items():
        if not hasattr(item, "file_name"):
            continue
        if not item.file_name.startswith("title"):
            continue
        body = item.content.decode("utf-8", errors="replace")
        kv = _parse_kv_table(body)
        category = kv.get("Category")
        if category and category not in md.fandoms:
            md.fandoms.insert(0, category)
        if not md.status:
            md.status = kv.get("Status")
        if not md.rating:
            md.rating = kv.get("Rating")
        break


def _fill_from_html(path: Path, md: "FileMetadata") -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    kv = _parse_kv_table(text)

    md.title = kv.get("Title")
    md.author = kv.get("Author")
    md.status = kv.get("Status")
    md.rating = kv.get("Rating")

    category = kv.get("Category")
    if category:
        md.fandoms.append(category)

    # Source link appears as <a href="URL">URL</a> — _parse_kv_table
    # has stripped the anchor tag and left the href text as the value,
    # so Source here is already the URL string.
    source = kv.get("Source")
    if source and source.startswith(("http://", "https://")):
        md.source_url = source


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


def extract_metadata(filepath) -> "FileMetadata":
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

    md.chapter_count = count_chapters(path)
    return md
