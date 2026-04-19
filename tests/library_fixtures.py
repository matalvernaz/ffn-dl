"""Factories that build library-manager test fixtures on demand.

Kept out of conftest.py because they're only used by the library tests
and several of them write files to a pytest tmp_path the caller owns.
"""

from __future__ import annotations

from pathlib import Path

from ffn_dl.exporters import export_epub, export_html, export_txt
from ffn_dl.models import Chapter, Story


def _story(
    *,
    title: str = "The Sample Fic",
    author: str = "Test Author",
    url: str = "https://www.fanfiction.net/s/12345/1/",
    summary: str = "A story used only for testing.",
    fandom: str = "Harry Potter",
    rating: str = "T",
    status: str = "In-Progress",
    chapters: int = 2,
    story_id: int = 12345,
) -> Story:
    """Build a Story with predictable metadata. Category is what ffn-dl
    uses as its fandom field in the metadata header."""
    s = Story(
        id=story_id,
        title=title,
        author=author,
        summary=summary,
        url=url,
    )
    s.metadata = {
        "category": fandom,
        "rating": rating,
        "status": status,
        "genre": "Drama",
        "characters": "Harry, Hermione",
    }
    for i in range(1, chapters + 1):
        s.chapters.append(
            Chapter(
                number=i,
                title=f"Chapter {i}",
                html=f"<p>This is the text of chapter {i}.</p>",
            )
        )
    return s


def ffndl_epub(tmp_path: Path, **kwargs) -> Path:
    """ffn-dl's own EPUB export: dc:source + title-page Category table."""
    return export_epub(_story(**kwargs), str(tmp_path))


def ffndl_html(tmp_path: Path, **kwargs) -> Path:
    return export_html(_story(**kwargs), str(tmp_path))


def ffndl_txt(tmp_path: Path, **kwargs) -> Path:
    return export_txt(_story(**kwargs), str(tmp_path))


def fanficfare_epub(
    tmp_path: Path,
    *,
    title: str = "Foreign Fic",
    author: str = "Other Author",
    url: str = "https://archiveofourown.org/works/9876543",
    fandoms: tuple[str, ...] = ("Harry Potter",),
    extra_subjects: tuple[str, ...] = ("Harry/Hermione", "Rated T", "Complete"),
) -> Path:
    """FanFicFare-style EPUB: fandoms live in dc:subject alongside
    ratings and relationships. No ffn-dl-style title-page table."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("fff-test-id")
    book.set_title(title)
    book.add_author(author)
    book.add_metadata("DC", "source", url)
    for fandom in fandoms:
        book.add_metadata("DC", "subject", fandom)
    for tag in extra_subjects:
        book.add_metadata("DC", "subject", tag)

    ch1 = epub.EpubHtml(title="Chapter 1", file_name="chapter_1.xhtml")
    ch1.content = b"<h2>Chapter 1</h2><p>FFF body.</p>"
    book.add_item(ch1)
    book.toc = [ch1]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch1]

    path = tmp_path / f"{title}.epub"
    epub.write_epub(str(path), book)
    return path


def fichub_epub(
    tmp_path: Path,
    *,
    title: str = "FicHub Story",
    author: str = "FicHub Author",
    url: str = "https://www.royalroad.com/fiction/55555",
    fandom: str | None = None,
) -> Path:
    """FicHub-style EPUB: sparser metadata — source + title + author,
    sometimes no fandom at all."""
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("fichub-test-id")
    book.set_title(title)
    book.add_author(author)
    book.add_metadata("DC", "source", url)
    if fandom:
        book.add_metadata("DC", "subject", fandom)

    ch1 = epub.EpubHtml(title="Chapter 1", file_name="chapter_1.xhtml")
    ch1.content = b"<h2>Chapter 1</h2><p>FicHub body.</p>"
    book.add_item(ch1)
    book.toc = [ch1]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch1]

    path = tmp_path / f"{title}.epub"
    epub.write_epub(str(path), book)
    return path


def bare_html_with_url(tmp_path: Path, url: str) -> Path:
    """Minimal HTML with the URL anywhere in the body — exercises the
    fallback URL regex in extract_source_url."""
    path = tmp_path / "bare.html"
    path.write_text(
        f"<html><body><p>See {url} for the original.</p></body></html>",
        encoding="utf-8",
    )
    return path


def bare_txt_with_url(tmp_path: Path, url: str) -> Path:
    path = tmp_path / "bare_with_url.txt"
    path.write_text(
        f"Here is some content.\n\nSource: {url}\n\nMore content.\n",
        encoding="utf-8",
    )
    return path


def bare_txt_no_url(tmp_path: Path) -> Path:
    """TXT with only title/author hint — no URL anywhere. Used to
    verify the LOW-confidence indexed-but-not-trackable path."""
    path = tmp_path / "Some Title - Unknown Author.txt"
    path.write_text(
        "Chapter 1\n\nOnce upon a time, there was no metadata.\n",
        encoding="utf-8",
    )
    return path
