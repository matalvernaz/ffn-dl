"""Export a Story to EPUB, HTML, or plain text."""

import re
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import Story

DEFAULT_TEMPLATE = "{title} - {author}"


def _safe_filename(name):
    """Strip characters that are illegal in filenames on Windows/macOS/Linux."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")


def format_filename(story: Story, template: str = DEFAULT_TEMPLATE) -> str:
    """Build a filename (no extension) from a template and story metadata.

    Supported placeholders:
        {title}  {author}  {id}  {words}  {status}  {rating}
        {language}  {chapters}
    """
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


# ── HTML → plain-text converter ───────────────────────────────────


def html_to_text(html: str) -> str:
    """Convert chapter HTML to readable plain text.

    Preserves paragraph breaks, scene breaks (<hr>), and line breaks (<br>).
    Inline tags (<em>, <strong>, etc.) stay inline instead of being split
    onto separate lines.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Replace <br> with newline markers before extracting text
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
                # get_text() correctly inlines <em>, <strong>, <span>, etc.
                text = child.get_text().strip()
                if text:
                    parts.append(text)

    return "\n\n".join(parts)


# ── Exporters ─────────────────────────────────────────────────────


def export_txt(
    story: Story, output_dir: str = ".", template: str = DEFAULT_TEMPLATE
) -> Path:
    filename = format_filename(story, template) + ".txt"
    path = Path(output_dir) / filename

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{story.title}\n")
        f.write(f"by {story.author}\n\n")
        if story.summary:
            f.write(f"Summary: {story.summary}\n")
        f.write(f"Source: {story.url}\n")
        f.write("=" * 60 + "\n")

        for ch in story.chapters:
            f.write(f"\n\n--- {ch.title} ---\n\n")
            f.write(html_to_text(ch.html))

    return path


def export_html(
    story: Story, output_dir: str = ".", template: str = DEFAULT_TEMPLATE
) -> Path:
    filename = format_filename(story, template) + ".html"
    path = Path(output_dir) / filename

    title_esc = escape(story.title)
    author_esc = escape(story.author)
    summary_esc = escape(story.summary)

    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            f'<meta charset="utf-8">\n'
            f"<title>{title_esc} by {author_esc}</title>\n"
            f"<style>\n"
            f"body{{max-width:800px;margin:2em auto;padding:0 1em;"
            f"font-family:Georgia,serif;line-height:1.6}}\n"
            f"h1{{text-align:center}}\n"
            f".author{{text-align:center;color:#555}}\n"
            f".summary{{font-style:italic;border-left:3px solid #ccc;"
            f"padding-left:1em;margin:1em 0}}\n"
            f".chapter{{margin:2em 0}}\n"
            f".chapter h2{{border-bottom:1px solid #ddd;padding-bottom:.3em}}\n"
            f"</style>\n</head>\n<body>\n"
            f"<h1>{title_esc}</h1>\n"
            f'<p class="author">by {author_esc}</p>\n'
        )
        if story.summary:
            f.write(f'<p class="summary">{summary_esc}</p>\n')
        f.write("<hr>\n")

        for ch in story.chapters:
            ch_title = escape(ch.title)
            f.write(f'<div class="chapter"><h2>{ch_title}</h2>\n')
            f.write(ch.html)
            f.write("\n</div><hr>\n")

        f.write("</body>\n</html>\n")

    return path


def export_epub(
    story: Story, output_dir: str = ".", template: str = DEFAULT_TEMPLATE
) -> Path:
    try:
        from ebooklib import epub
    except ImportError:
        raise ImportError(
            "EPUB export requires the 'ebooklib' package.\n"
            "Install it with: pip install 'ffn-dl[epub]'  (or pip install ebooklib)"
        )

    book = epub.EpubBook()
    book.set_identifier(f"ffn-{story.id}")
    book.set_title(story.title)
    book.set_language("en")
    book.add_author(story.author)
    book.add_metadata("DC", "description", story.summary)
    book.add_metadata("DC", "source", story.url)

    css = epub.EpubItem(
        uid="style",
        file_name="style/default.css",
        media_type="text/css",
        content=b"body{font-family:Georgia,serif;line-height:1.6}",
    )
    book.add_item(css)

    epub_chapters = []
    for ch in story.chapters:
        ec = epub.EpubHtml(
            title=ch.title,
            file_name=f"chapter_{ch.number}.xhtml",
            lang="en",
        )
        heading = escape(ch.title)
        ec.content = f"<h2>{heading}</h2>\n{ch.html}".encode("utf-8")
        ec.add_item(css)
        book.add_item(ec)
        epub_chapters.append(ec)

    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    filename = format_filename(story, template) + ".epub"
    path = Path(output_dir) / filename
    epub.write_epub(str(path), book)
    return path


EXPORTERS = {
    "txt": export_txt,
    "html": export_html,
    "epub": export_epub,
}
