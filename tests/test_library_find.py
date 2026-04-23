"""Library index search: substring match across title/author/fandom/URL."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.library import search_index
from ffn_dl.library.index import LibraryIndex


def _fresh_index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(
        tmp_path / "library-index.json",
        {"version": 1, "libraries": {}},
    )


def _seed(
    index: LibraryIndex,
    root: Path,
    url: str,
    *,
    title: str = "T",
    author: str = "A",
    fandoms: list[str] | None = None,
    relpath: str | None = None,
) -> None:
    index.library_state(root)["stories"][url] = {
        "relpath": relpath or f"path/{url.rsplit('/', 1)[-1]}.epub",
        "title": title, "author": author,
        "fandoms": fandoms or [],
        "adapter": "ffn",
        "format": "epub", "confidence": "high",
        "chapter_count": 1, "last_checked": "2026-04-01T00:00:00Z",
    }


class TestBasicMatching:
    def test_title_match(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", title="The Lost Future")
        _seed(idx, tmp_path, "https://x/2", title="A Different Story")
        matches = search_index(idx, "lost")
        assert len(matches) == 1
        assert matches[0].title == "The Lost Future"

    def test_author_match(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", author="JaneDoe92")
        _seed(idx, tmp_path, "https://x/2", author="SomeoneElse")
        matches = search_index(idx, "janedoe")
        assert len(matches) == 1

    def test_fandom_match(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", fandoms=["Harry Potter"])
        _seed(idx, tmp_path, "https://x/2", fandoms=["Naruto"])
        matches = search_index(idx, "potter")
        assert len(matches) == 1

    def test_url_match(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://archiveofourown.org/works/42")
        _seed(idx, tmp_path, "https://www.fanfiction.net/s/99")
        matches = search_index(idx, "ao3")
        assert matches == []  # URL contains "archiveofourown", not "ao3"
        matches = search_index(idx, "archiveofourown")
        assert len(matches) == 1


class TestCaseInsensitive:
    def test_query_case_insensitive(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", title="The Lost Future")
        assert len(search_index(idx, "LOST")) == 1
        assert len(search_index(idx, "LoSt")) == 1
        assert len(search_index(idx, "lost")) == 1


class TestMultiRoot:
    def test_all_roots_searched_by_default(self, tmp_path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        idx = _fresh_index(tmp_path)
        _seed(idx, root_a, "https://x/1", title="Found Me")
        _seed(idx, root_b, "https://x/2", title="Found Me")

        matches = search_index(idx, "found")
        roots = {str(m.root) for m in matches}
        assert roots == {str(root_a), str(root_b)}

    def test_roots_filter_limits_search(self, tmp_path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        idx = _fresh_index(tmp_path)
        _seed(idx, root_a, "https://x/1", title="Found Me")
        _seed(idx, root_b, "https://x/2", title="Found Me")

        matches = search_index(idx, "found", roots=[root_a])
        assert len(matches) == 1
        assert matches[0].root == root_a


class TestCrossFieldMatching:
    def test_query_matches_across_title_author(self, tmp_path):
        """A multi-word query matches when the tokens span multiple
        fields because the haystack is the space-joined concatenation."""
        idx = _fresh_index(tmp_path)
        _seed(
            idx, tmp_path, "https://x/1",
            title="The Lost Future", author="MortalCoil",
        )
        matches = search_index(idx, "lost future mortal")
        assert len(matches) == 1

    def test_empty_query_returns_empty(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", title="Any")
        assert search_index(idx, "") == []
        assert search_index(idx, "   ") == []


class TestLimit:
    def test_limit_caps_results(self, tmp_path):
        idx = _fresh_index(tmp_path)
        for i in range(20):
            _seed(idx, tmp_path, f"https://x/{i}", title="Found")
        matches = search_index(idx, "found", limit=5)
        assert len(matches) == 5

    def test_limit_none_returns_all(self, tmp_path):
        idx = _fresh_index(tmp_path)
        for i in range(20):
            _seed(idx, tmp_path, f"https://x/{i}", title="Found")
        matches = search_index(idx, "found", limit=None)
        assert len(matches) == 20


class TestLibraryMatchShape:
    def test_accessors_never_return_none(self, tmp_path):
        idx = _fresh_index(tmp_path)
        # Entry missing optional fields (fandoms, etc).
        idx.library_state(tmp_path)["stories"]["https://x/1"] = {
            "relpath": "t.epub", "title": "T", "author": "A",
            "adapter": "ffn", "format": "epub",
            "confidence": "high", "chapter_count": 1,
            "last_checked": "2026-04-01T00:00:00Z",
        }
        matches = search_index(idx, "t")
        assert matches[0].fandoms == []
        assert matches[0].title == "T"
        assert matches[0].author == "A"
        assert matches[0].absolute_path == tmp_path / "t.epub"

    def test_absolute_path_with_missing_relpath(self, tmp_path):
        idx = _fresh_index(tmp_path)
        idx.library_state(tmp_path)["stories"]["https://x/1"] = {
            "title": "T", "author": "A", "fandoms": [],
            "adapter": "ffn", "format": "epub",
            "confidence": "high", "chapter_count": 1,
            "last_checked": "2026-04-01T00:00:00Z",
        }
        # Missing relpath falls back to the library root — caller can
        # still format it without a crash.
        matches = search_index(idx, "t")
        assert matches[0].absolute_path == tmp_path
