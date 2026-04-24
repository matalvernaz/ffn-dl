"""Tests for the FTS5-backed library full-text search."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.library.fulltext import (
    BootstrapReport,
    FullTextIndex,
    chapter_text,
    populate_from_library,
)
from ffn_dl.library.scanner import scan
from ffn_dl.models import Chapter

from .library_fixtures import bare_txt_no_url, ffndl_epub


# ── chapter_text ─────────────────────────────────────────────────


def test_chapter_text_strips_tags_and_collapses_whitespace():
    html = (
        "<p>Hello <em>world</em>, meet the <strong>dragon</strong>.</p>\n\n"
        "<p>Another\tline   with   extra whitespace.</p>"
    )
    text = chapter_text(html)
    assert "<" not in text and ">" not in text
    # Paragraph break becomes a space after whitespace normalisation,
    # which is what FTS5 needs for word-level tokenisation.
    assert "dragon" in text
    assert "Another line with extra whitespace." in text


def test_chapter_text_empty_input_returns_empty_string():
    assert chapter_text(None) == ""
    assert chapter_text("") == ""


# ── FullTextIndex ────────────────────────────────────────────────


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "search.db"


def _ch(num: int, title: str, body: str) -> Chapter:
    return Chapter(
        number=num,
        title=title,
        html=f"<p>{body}</p>",
    )


def test_index_story_and_search_finds_matches(tmp_path: Path):
    fti = FullTextIndex(_db_path(tmp_path))
    fti.index_story(
        root="/lib/a",
        url="https://example.com/s/1",
        relpath="story.epub",
        title="The Dragon Tale",
        author="Test Author",
        chapters=[
            _ch(1, "Arrival", "The party arrives at the village."),
            _ch(2, "Encounter", "A dragon landed on the orphanage roof."),
        ],
    )
    hits = fti.search("orphanage")
    assert len(hits) == 1
    assert hits[0].chapter_number == 2
    assert hits[0].url == "https://example.com/s/1"
    assert hits[0].title == "The Dragon Tale"
    # Snippet comes back with FTS5 highlight brackets
    assert "[orphanage]" in hits[0].snippet


def test_search_respects_root_filter(tmp_path: Path):
    fti = FullTextIndex(_db_path(tmp_path))
    fti.index_story(
        root="/lib/a",
        url="https://example.com/s/1",
        relpath="a.epub",
        title="A",
        author="X",
        chapters=[_ch(1, "One", "The dragon roared.")],
    )
    fti.index_story(
        root="/lib/b",
        url="https://example.com/s/2",
        relpath="b.epub",
        title="B",
        author="Y",
        chapters=[_ch(1, "One", "The dragon roared.")],
    )
    assert len(fti.search("dragon")) == 2
    assert len(fti.search("dragon", root="/lib/a")) == 1
    assert fti.search("dragon", root="/lib/a")[0].url.endswith("/1")


def test_index_story_replaces_prior_rows(tmp_path: Path):
    """An author's silent rewrite that removes "dragon" must leave no
    trace of the old body in the index — otherwise a search would
    return a stale hit for a phrase the current text doesn't contain."""
    fti = FullTextIndex(_db_path(tmp_path))
    url = "https://example.com/s/1"
    fti.index_story(
        root="/lib",
        url=url,
        relpath="s.epub",
        title="Title",
        author="Author",
        chapters=[_ch(1, "One", "The dragon roared.")],
    )
    assert fti.search("dragon")
    fti.index_story(
        root="/lib",
        url=url,
        relpath="s.epub",
        title="Title",
        author="Author",
        chapters=[_ch(1, "One", "The wyvern roared.")],
    )
    assert fti.search("dragon") == []
    assert len(fti.search("wyvern")) == 1


def test_index_story_empty_chapter_list_drops_existing_rows(tmp_path: Path):
    fti = FullTextIndex(_db_path(tmp_path))
    url = "https://example.com/s/1"
    fti.index_story(
        root="/lib",
        url=url,
        relpath="s.epub",
        title="T",
        author="A",
        chapters=[_ch(1, "One", "body text.")],
    )
    fti.index_story(
        root="/lib",
        url=url,
        relpath="s.epub",
        title="T",
        author="A",
        chapters=[],
    )
    assert fti.stats() == {"stories": 0, "chapters": 0, "roots": []}


def test_drop_story_and_drop_root(tmp_path: Path):
    fti = FullTextIndex(_db_path(tmp_path))
    fti.index_story(
        root="/lib",
        url="https://example.com/s/1",
        relpath="a.epub",
        title="A", author="X",
        chapters=[_ch(1, "One", "alpha body")],
    )
    fti.index_story(
        root="/lib",
        url="https://example.com/s/2",
        relpath="b.epub",
        title="B", author="X",
        chapters=[_ch(1, "One", "beta body")],
    )
    fti.drop_story("/lib", "https://example.com/s/1")
    assert fti.stats()["stories"] == 1
    fti.drop_root("/lib")
    assert fti.stats() == {"stories": 0, "chapters": 0, "roots": []}


def test_search_empty_query_returns_empty(tmp_path: Path):
    fti = FullTextIndex(_db_path(tmp_path))
    fti.index_story(
        root="/lib",
        url="https://example.com/s/1",
        relpath="a.epub",
        title="A", author="X",
        chapters=[_ch(1, "One", "content")],
    )
    assert fti.search("") == []
    assert fti.search("   ") == []


def test_search_invalid_syntax_raises_value_error(tmp_path: Path):
    """FTS5 rejects malformed MATCH expressions with OperationalError;
    the wrapper surfaces them as ValueError so CLI callers render a
    user-facing message rather than a SQLite traceback."""
    fti = FullTextIndex(_db_path(tmp_path))
    fti.index_story(
        root="/lib",
        url="https://example.com/s/1",
        relpath="a.epub",
        title="A", author="X",
        chapters=[_ch(1, "One", "hello")],
    )
    # Unbalanced parenthesis is an FTS5 syntax error
    with pytest.raises(ValueError):
        fti.search("((dragon")


def test_stats_counts_distinct_stories(tmp_path: Path):
    fti = FullTextIndex(_db_path(tmp_path))
    fti.index_story(
        root="/lib/a",
        url="https://example.com/s/1",
        relpath="a.epub",
        title="A", author="X",
        chapters=[_ch(1, "One", "x"), _ch(2, "Two", "y")],
    )
    fti.index_story(
        root="/lib/b",
        url="https://example.com/s/2",
        relpath="b.epub",
        title="B", author="Y",
        chapters=[_ch(1, "One", "z")],
    )
    s = fti.stats()
    assert s["stories"] == 2
    assert s["chapters"] == 3
    assert s["roots"] == ["/lib/a", "/lib/b"]


# ── populate_from_library ───────────────────────────────────────


def _index(tmp_path: Path) -> Path:
    return tmp_path / "idx.json"


def test_populate_indexes_scanned_library(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Alpha",
        url="https://www.fanfiction.net/s/1/1/",
    )
    ffndl_epub(
        lib, title="Beta",
        url="https://archiveofourown.org/works/2",
    )
    scan(lib, index_path=_index(tmp_path))

    fti = FullTextIndex(_db_path(tmp_path))
    report = populate_from_library(
        fti, lib, index_path=_index(tmp_path),
    )
    assert report.indexed == 2
    assert report.failed == 0
    assert report.chapters == 4  # two chapters per fixture story

    # Chapter body "This is the text of chapter 2." is in both
    # fixtures' chapter 2; the search should return both, ordered by
    # FTS5 rank.
    hits = fti.search("text of chapter")
    urls = {h.url for h in hits}
    assert urls == {
        "https://www.fanfiction.net/s/1",
        "https://archiveofourown.org/works/2",
    }


def test_populate_skips_unsupported_formats(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bare_txt_no_url(lib)  # LOW confidence: ends up in untrackable, not stories
    scan(lib, index_path=_index(tmp_path))

    fti = FullTextIndex(_db_path(tmp_path))
    report = populate_from_library(fti, lib, index_path=_index(tmp_path))
    # The TXT was LOW-confidence and never landed in the story index,
    # so populate has nothing to skip — it quietly finishes empty.
    assert report.indexed == 0
    assert report.chapters == 0


def test_populate_drops_missing_files(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Vanishing",
        url="https://www.fanfiction.net/s/9/1/",
    )
    scan(lib, index_path=_index(tmp_path))
    path.unlink()

    fti = FullTextIndex(_db_path(tmp_path))
    report = populate_from_library(fti, lib, index_path=_index(tmp_path))
    assert report.indexed == 0
    assert report.skipped_missing == 1


def test_populate_rebuilds_cleanly(tmp_path: Path):
    """Re-running populate against the same root must not accumulate
    duplicates — drop_root + re-insert is the expected semantics."""
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Once",
        url="https://www.fanfiction.net/s/1/1/",
    )
    scan(lib, index_path=_index(tmp_path))

    fti = FullTextIndex(_db_path(tmp_path))
    populate_from_library(fti, lib, index_path=_index(tmp_path))
    populate_from_library(fti, lib, index_path=_index(tmp_path))
    assert fti.stats()["stories"] == 1
    assert fti.stats()["chapters"] == 2


def test_bootstrap_report_summary_text():
    r = BootstrapReport(
        indexed=3, skipped_missing=1, skipped_unsupported=2,
        failed=0, chapters=12,
    )
    s = r.summary()
    assert "Indexed 3 stories" in s
    assert "12 chapters" in s
    assert "1 skipped (file missing)" in s
    assert "2 skipped (unsupported format" in s
