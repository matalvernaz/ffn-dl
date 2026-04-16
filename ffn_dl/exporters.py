"""Export a Story to EPUB, HTML, or plain text."""

import re
from html import escape
from pathlib import Path

from .models import Story


def _safe_filename(name):
    """Strip characters that are illegal in filenames on Windows/macOS/Linux."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")


def export_txt(story: Story, output_dir: str = ".") -> Path:
    filename = f"{_safe_filename(story.title)} - {_safe_filename(story.author)}.txt"
    path = Path(output_dir) / filename

    lines = []
    lines.append(story.title)
    lines.append(f"by {story.author}")
    lines.append("")
    if story.summary:
        lines.append(f"Summary: {story.summary}")
    lines.append(f"Source: {story.url}")
    lines.append("")
    lines.append("=" * 60)

    for ch in story.chapters:
        lines.append("")
        lines.append(f"--- {ch.title} ---")
        lines.append("")
        lines.append(ch.text)

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def export_html(story: Story, output_dir: str = ".") -> Path:
    filename = f"{_safe_filename(story.title)} - {_safe_filename(story.author)}.html"
    path = Path(output_dir) / filename

    title_esc = escape(story.title)
    author_esc = escape(story.author)
    summary_esc = escape(story.summary)

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{title_esc} by {author_esc}</title>",
        "<style>",
        "body{max-width:800px;margin:2em auto;padding:0 1em;"
        "font-family:Georgia,serif;line-height:1.6}",
        "h1{text-align:center}",
        ".author{text-align:center;color:#555}",
        ".summary{font-style:italic;border-left:3px solid #ccc;"
        "padding-left:1em;margin:1em 0}",
        ".chapter{margin:2em 0}",
        ".chapter h2{border-bottom:1px solid #ddd;padding-bottom:.3em}",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{title_esc}</h1>",
        f'<p class="author">by {author_esc}</p>',
    ]
    if story.summary:
        parts.append(f'<p class="summary">{summary_esc}</p>')
    parts.append("<hr>")

    for ch in story.chapters:
        title = escape(ch.title)
        parts.append(f'<div class="chapter"><h2>{title}</h2>')
        parts.append(ch.html)
        parts.append("</div><hr>")

    parts.append("</body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def export_epub(story: Story, output_dir: str = ".") -> Path:
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

    filename = f"{_safe_filename(story.title)} - {_safe_filename(story.author)}.epub"
    path = Path(output_dir) / filename
    epub.write_epub(str(path), book)
    return path


EXPORTERS = {
    "txt": export_txt,
    "html": export_html,
    "epub": export_epub,
}
