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
    if "words" in m:
        fields.append(("Words", m["words"]))
        try:
            total_words = int(m["words"].replace(",", ""))
            total_minutes = max(1, round(total_words / 250))
            if total_minutes >= 60:
                hours, minutes = divmod(total_minutes, 60)
                reading_time = f"{hours} hours {minutes} minutes"
            else:
                reading_time = f"{total_minutes} minutes"
            fields.append(("Reading Time", reading_time))
        except (ValueError, AttributeError):
            pass
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


def export_txt(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,  # accepted for signature parity; TXT always renders hr as "* * *"
) -> Path:
    filename = format_filename(story, template) + ".txt"
    path = Path(output_dir) / filename

    with open(path, "w", encoding="utf-8") as f:
        for label, value in _meta_fields(story):
            f.write(f"{label}: {value}\n")
        f.write("=" * 60 + "\n")

        for ch in story.chapters:
            f.write(f"\n\n--- {ch.title} ---\n\n")
            f.write(html_to_text(ch.html))

    return path


def export_html(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,
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
            chapter_html = _apply_hr_as_stars(ch.html) if hr_as_stars else ch.html
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
    return "ffn", "fanfiction.net"


_HR_RE = re.compile(r"<hr\s*/?>|<hr\s[^>]*/?>", re.IGNORECASE)
_HR_STARS_REPLACEMENT = (
    '<div class="scenebreak" '
    'style="text-align:center;margin:1em 0">* * *</div>'
)


def _apply_hr_as_stars(html: str) -> str:
    """Replace <hr> tags with a centred '* * *' divider for readers whose
    stylesheet renders rules as a thin line that's easy to miss."""
    return _HR_RE.sub(_HR_STARS_REPLACEMENT, html)


def export_epub(
    story: Story,
    output_dir: str = ".",
    template: str = DEFAULT_TEMPLATE,
    hr_as_stars: bool = False,
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
        content=b"body{font-family:Georgia,serif;line-height:1.6}"
        b"table{border-collapse:collapse;margin:1em 0}"
        b"th{text-align:right;padding:.25em 1em .25em 0;vertical-align:top;color:#555}"
        b"td{padding:.25em 0;vertical-align:top}"
        b"a{color:#36c}",
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
        chapter_html = _apply_hr_as_stars(ch.html) if hr_as_stars else ch.html
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
