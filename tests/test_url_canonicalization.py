"""Tests for sites.canonical_url and its effect on the library index.

Two files on disk for the same story can embed slightly different URL
forms — ``/s/N`` vs ``/s/N/1/`` on FFN, http vs https on AO3, etc. The
library index keys entries by canonical URL so those variants collapse
to a single entry, and duplicate detection fires correctly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffn_dl.library.index import LibraryIndex, SCHEMA_VERSION
from ffn_dl.sites import canonical_url


# ---------------------------------------------------------------------------
# canonical_url itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # FFN: http/https, trailing slash, chapter suffix, slug suffix.
        ("https://www.fanfiction.net/s/12345", "https://www.fanfiction.net/s/12345"),
        ("http://www.fanfiction.net/s/12345", "https://www.fanfiction.net/s/12345"),
        ("https://www.fanfiction.net/s/12345/", "https://www.fanfiction.net/s/12345"),
        ("https://www.fanfiction.net/s/12345/1/", "https://www.fanfiction.net/s/12345"),
        (
            "https://www.fanfiction.net/s/12345/1/Some-Title-Slug",
            "https://www.fanfiction.net/s/12345",
        ),
        # AO3: http vs https, short-host form.
        ("http://archiveofourown.org/works/7", "https://archiveofourown.org/works/7"),
        ("https://archiveofourown.org/works/7", "https://archiveofourown.org/works/7"),
        ("https://ao3.org/works/7", "https://archiveofourown.org/works/7"),
        # Royal Road, FicWad — straightforward.
        ("https://royalroad.com/fiction/42", "https://www.royalroad.com/fiction/42"),
        ("http://ficwad.com/story/100", "https://ficwad.com/story/100"),
        # Literotica: slug-based IDs.
        (
            "https://www.literotica.com/s/my-fic-ch-02",
            "https://www.literotica.com/s/my-fic-ch-02",
        ),
        # Wattpad: /story/<id> and /<id>-<slug> both collapse to /story/<id>.
        (
            "https://www.wattpad.com/story/42",
            "https://www.wattpad.com/story/42",
        ),
        (
            "https://www.wattpad.com/42-some-title",
            "https://www.wattpad.com/story/42",
        ),
    ],
)
def test_canonical_url_collapses_known_variants(raw, expected):
    assert canonical_url(raw) == expected


def test_canonical_url_returns_empty_for_empty():
    assert canonical_url("") == ""


def test_canonical_url_unknown_host_is_still_normalised():
    """Non-supported hosts still get scheme + trailing-slash normalised
    so two variants of a hand-typed URL don't masquerade as distinct."""
    result = canonical_url("HTTP://Example.COM/path/")
    assert result == "https://example.com/path"


# ---------------------------------------------------------------------------
# Duplicate detection inside LibraryIndex.record
# ---------------------------------------------------------------------------


class _FakeMeta:
    """Minimal FileMetadata stand-in for ``record`` tests."""

    def __init__(self, url):
        self.title = "T"
        self.author = "A"
        self.source_url = url
        self.fandoms = []
        self.rating = None
        self.status = None
        self.chapter_count = 1
        self.format = "html"


class _FakeCandidate:
    def __init__(self, path, url, confidence_value="high"):
        from ffn_dl.library.candidate import Confidence

        self.path = path
        self.metadata = _FakeMeta(url)
        self.adapter_name = "ffn"
        self.confidence = Confidence.HIGH if confidence_value == "high" else Confidence.LOW
        self.is_trackable = True
        self.notes = []


def _tmp_library(tmp_path: Path, filename: str) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    sub = filename.split("/")[0] if "/" in filename else ""
    if sub:
        (lib / sub).mkdir(exist_ok=True)
    path = lib / filename
    path.write_text("<html></html>", encoding="utf-8")
    return lib


def test_same_story_different_url_shapes_is_detected_as_duplicate(tmp_path):
    """The core bug from Matt's library: ``/s/N`` and ``/s/N/1/`` are
    the same story but the pre-canonical index treated them as two
    separate entries. After canonical_url, both collapse to one and
    the second copy lands in ``duplicate_relpaths``."""
    lib = _tmp_library(tmp_path, "Harry Potter/foo.html")
    (lib / "misc").mkdir()
    (lib / "misc/foo.html").write_text("<html></html>", encoding="utf-8")

    idx = LibraryIndex(tmp_path / "idx.json", {"version": SCHEMA_VERSION, "libraries": {}})

    # First file: clean FFN URL.
    first = _FakeCandidate(
        lib / "Harry Potter/foo.html",
        "https://www.fanfiction.net/s/9215532",
    )
    is_new_1 = idx.record(lib, first)

    # Second file: same story, embedded URL has the /1/ chapter suffix.
    second = _FakeCandidate(
        lib / "misc/foo.html",
        "https://www.fanfiction.net/s/9215532/1/",
    )
    is_new_2 = idx.record(lib, second)

    assert is_new_1 is True
    assert is_new_2 is False

    stories = list(idx.stories_in(lib))
    assert len(stories) == 1
    url, entry = stories[0]
    assert url == "https://www.fanfiction.net/s/9215532"
    assert entry["relpath"] == "Harry Potter/foo.html"
    assert entry["duplicate_relpaths"] == ["misc/foo.html"]


def test_duplicate_relpaths_deduplicates_on_rescan(tmp_path):
    """Re-scanning the library shouldn't pile up the same path in
    ``duplicate_relpaths`` — we append only when it's genuinely new."""
    lib = _tmp_library(tmp_path, "A/s.html")
    (lib / "B").mkdir()
    (lib / "B/s.html").write_text("<html></html>", encoding="utf-8")

    idx = LibraryIndex(tmp_path / "idx.json", {"version": SCHEMA_VERSION, "libraries": {}})

    a = _FakeCandidate(lib / "A/s.html", "https://www.fanfiction.net/s/1")
    b = _FakeCandidate(lib / "B/s.html", "https://www.fanfiction.net/s/1/1/")

    idx.record(lib, a)
    idx.record(lib, b)
    # Scan again — duplicate_relpaths should still contain just one entry.
    idx.record(lib, a)
    idx.record(lib, b)

    [(_, entry)] = list(idx.stories_in(lib))
    assert entry["duplicate_relpaths"] == ["B/s.html"]


# ---------------------------------------------------------------------------
# Load-time migration of non-canonical keys in an existing index file
# ---------------------------------------------------------------------------


def test_load_migrates_non_canonical_keys(tmp_path):
    """An index written by 1.20.x (no canonicalisation) loads into the
    new build with keys rewritten and colliding entries merged."""
    path = tmp_path / "idx.json"
    raw = {
        "version": SCHEMA_VERSION,
        "libraries": {
            str(tmp_path / "lib"): {
                "last_scan": None,
                "stories": {
                    "https://www.fanfiction.net/s/9215532": {
                        "relpath": "Harry Potter/fic.html",
                        "title": "Proper Title",
                        "author": "Someone",
                        "chapter_count": 17,
                        "adapter": "ffn",
                        "confidence": "high",
                        "format": "html",
                    },
                    "https://www.fanfiction.net/s/9215532/1/": {
                        "relpath": "misc/fic.html",
                        "title": None,
                        "author": None,
                        "chapter_count": 0,
                        "adapter": "ffn",
                        "confidence": "high",
                        "format": "html",
                    },
                },
                "untrackable": [],
            }
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    idx = LibraryIndex.load(path)
    lib_state = idx.library_state(tmp_path / "lib")
    assert list(lib_state["stories"].keys()) == [
        "https://www.fanfiction.net/s/9215532"
    ]
    primary = lib_state["stories"]["https://www.fanfiction.net/s/9215532"]
    # The richer entry (with title/author/chapter_count) wins the primary slot.
    assert primary["relpath"] == "Harry Potter/fic.html"
    assert primary["duplicate_relpaths"] == ["misc/fic.html"]
    assert primary["chapter_count"] == 17
