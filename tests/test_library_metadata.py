"""Metadata-extraction and identifier tests for the library manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.library.candidate import Confidence
from ffn_dl.library.identifier import adapter_for_url, identify
from ffn_dl.updater import extract_metadata

from .library_fixtures import (
    bare_html_with_url,
    bare_txt_no_url,
    bare_txt_with_url,
    fanficfare_epub,
    ffndl_epub,
    ffndl_html,
    ffndl_txt,
    fichub_epub,
)


# ── Metadata extraction — ffn-dl's own exports ─────────────────


def test_extract_metadata_ffndl_epub(tmp_path: Path):
    path = ffndl_epub(tmp_path)
    md = extract_metadata(path)
    assert md.format == "epub"
    assert md.source_url == "https://www.fanfiction.net/s/12345/1/"
    assert md.title == "The Sample Fic"
    assert md.author == "Test Author"
    # ffn-dl puts Category in the title-page table, not dc:subject
    assert "Harry Potter" in md.fandoms
    assert md.status == "In-Progress"
    assert md.rating == "T"
    assert md.chapter_count == 2


def test_extract_metadata_ffndl_html(tmp_path: Path):
    path = ffndl_html(tmp_path)
    md = extract_metadata(path)
    assert md.format == "html"
    assert md.source_url == "https://www.fanfiction.net/s/12345/1/"
    assert md.title == "The Sample Fic"
    assert md.author == "Test Author"
    assert "Harry Potter" in md.fandoms
    assert md.chapter_count == 2


def test_extract_metadata_ffndl_txt(tmp_path: Path):
    path = ffndl_txt(tmp_path)
    md = extract_metadata(path)
    assert md.format == "txt"
    assert md.source_url == "https://www.fanfiction.net/s/12345/1/"
    assert md.title == "The Sample Fic"
    assert md.author == "Test Author"
    assert "Harry Potter" in md.fandoms
    assert md.chapter_count == 2


# ── Metadata extraction — foreign provenance ────────────────────


def test_extract_metadata_fanficfare_epub(tmp_path: Path):
    path = fanficfare_epub(tmp_path, fandoms=("Harry Potter",))
    md = extract_metadata(path)
    assert md.source_url == "https://archiveofourown.org/works/9876543"
    assert md.title == "Foreign Fic"
    assert md.author == "Other Author"
    # FFF embeds fandom as a dc:subject — should be picked up
    assert "Harry Potter" in md.fandoms
    # Genre/rating/status tags should NOT leak into fandoms
    assert not any("Rated" in f for f in md.fandoms)
    assert "Complete" not in md.fandoms


def test_extract_metadata_fanficfare_multi_fandom(tmp_path: Path):
    path = fanficfare_epub(
        tmp_path,
        fandoms=("Harry Potter", "The Hobbit"),
    )
    md = extract_metadata(path)
    assert set(md.fandoms) >= {"Harry Potter", "The Hobbit"}


def test_extract_metadata_fichub_epub(tmp_path: Path):
    path = fichub_epub(tmp_path)
    md = extract_metadata(path)
    assert md.source_url == "https://www.royalroad.com/fiction/55555"
    assert md.title == "FicHub Story"
    assert md.fandoms == []  # No fandom metadata in this fixture


def test_extract_metadata_bare_html_url_only(tmp_path: Path):
    url = "https://www.fanfiction.net/s/99999/1/"
    path = bare_html_with_url(tmp_path, url)
    md = extract_metadata(path)
    # URL-in-content regex captures the canonical story base; the
    # trailing "/1/" chapter component is optional in the pattern
    assert md.source_url is not None
    assert md.source_url.startswith("https://www.fanfiction.net/s/99999")


def test_extract_metadata_bare_txt_with_url(tmp_path: Path):
    url = "https://archiveofourown.org/works/11223"
    path = bare_txt_with_url(tmp_path, url)
    md = extract_metadata(path)
    assert md.source_url == url


def test_extract_metadata_bare_txt_no_url(tmp_path: Path):
    path = bare_txt_no_url(tmp_path)
    md = extract_metadata(path)
    assert md.source_url is None
    assert md.title is None  # No metadata anywhere to recover


# ── Identifier ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.fanfiction.net/s/123/1/", "ffn"),
        ("https://archiveofourown.org/works/9", "ao3"),
        ("https://ao3.org/works/9", "ao3"),
        ("https://www.royalroad.com/fiction/1", "royalroad"),
        ("https://www.ficwad.com/story/1", "ficwad"),
        ("https://www.literotica.com/s/foo", "literotica"),
        ("https://www.wattpad.com/story/1", "wattpad"),
        ("https://mediaminer.org/fanfic/s/foo/1", "mediaminer"),
        ("https://example.com/nothing", None),
        ("", None),
    ],
)
def test_adapter_for_url(url: str, expected: str | None):
    assert adapter_for_url(url) == expected


def test_identify_high_confidence_from_url(tmp_path: Path):
    path = ffndl_epub(tmp_path)
    md = extract_metadata(path)
    candidate = identify(path, md)
    assert candidate.confidence == Confidence.HIGH
    assert candidate.adapter_name == "ffn"
    assert candidate.is_trackable


def test_identify_high_confidence_fanficfare(tmp_path: Path):
    path = fanficfare_epub(tmp_path)
    md = extract_metadata(path)
    candidate = identify(path, md)
    assert candidate.confidence == Confidence.HIGH
    assert candidate.adapter_name == "ao3"
    assert candidate.is_trackable


def test_identify_low_confidence_no_url(tmp_path: Path):
    path = bare_txt_no_url(tmp_path)
    md = extract_metadata(path)
    candidate = identify(path, md)
    assert candidate.confidence == Confidence.LOW
    assert candidate.adapter_name is None
    assert not candidate.is_trackable
    assert candidate.notes  # Explanatory note present


def test_identify_low_confidence_unknown_site(tmp_path: Path):
    # A URL that doesn't match any supported adapter — indexed but
    # not trackable, with a clear note
    from ffn_dl.library.candidate import StoryCandidate
    from ffn_dl.updater import FileMetadata

    md = FileMetadata(
        source_url="https://example.com/story/1",
        title="X",
        author="Y",
        format="epub",
    )
    candidate = identify(tmp_path / "x.epub", md)
    assert candidate.confidence == Confidence.LOW
    assert candidate.adapter_name is None
    assert any("does not match" in n for n in candidate.notes)
