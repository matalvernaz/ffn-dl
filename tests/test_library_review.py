"""Tests for the review flow that promotes untrackable library entries."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.library.index import LibraryIndex
from ffn_dl.library.review import promote_untrackable, untrackable_for_root
from ffn_dl.library.scanner import scan

from .library_fixtures import bare_txt_no_url


def _scanned_library(tmp_path: Path) -> tuple[Path, Path, LibraryIndex]:
    """Build a library with one untrackable file + scan it + return idx."""
    lib = tmp_path / "lib"
    lib.mkdir()
    bare_txt_no_url(lib)
    idx_file = tmp_path / "idx.json"
    scan(lib, index_path=idx_file)
    return lib, idx_file, LibraryIndex.load(idx_file)


def test_untrackable_for_root_after_scan(tmp_path: Path):
    lib, _idx_file, idx = _scanned_library(tmp_path)
    untrackable = untrackable_for_root(idx, lib)
    assert len(untrackable) == 1
    assert untrackable[0]["format"] == "txt"


def test_promote_with_supported_url_succeeds(tmp_path: Path):
    lib, idx_file, idx = _scanned_library(tmp_path)
    untrackable = untrackable_for_root(idx, lib)
    relpath = untrackable[0]["relpath"]

    result = promote_untrackable(
        idx, lib, relpath,
        url="https://www.fanfiction.net/s/54321/1/",
    )
    assert result.ok
    assert result.adapter == "ffn"

    # Reload from disk to confirm the promotion was saved
    reloaded = LibraryIndex.load(idx_file)
    assert untrackable_for_root(reloaded, lib) == []
    stories = list(reloaded.stories_in(lib))
    assert len(stories) == 1
    url, entry = stories[0]
    # Index keys use the canonical URL form; FFN's /1/ suffix is
    # stripped by sites.canonical_url.
    assert url == "https://www.fanfiction.net/s/54321"
    assert entry["adapter"] == "ffn"
    assert entry["confidence"] == "medium"


def test_promote_with_unsupported_url_fails(tmp_path: Path):
    lib, _idx_file, idx = _scanned_library(tmp_path)
    untrackable = untrackable_for_root(idx, lib)
    relpath = untrackable[0]["relpath"]

    result = promote_untrackable(idx, lib, relpath, url="https://example.com/foo")
    assert not result.ok
    assert "does not match" in result.message
    # The entry is still in untrackable
    assert len(untrackable_for_root(idx, lib)) == 1


def test_promote_with_unknown_relpath_fails(tmp_path: Path):
    lib, _idx_file, idx = _scanned_library(tmp_path)
    result = promote_untrackable(
        idx, lib, "made-up-file.txt",
        url="https://www.fanfiction.net/s/1/1/",
    )
    assert not result.ok
    assert "No untrackable entry" in result.message


def test_promote_batched_saves_deferred(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    # Two untrackable files in one library
    bare_txt_no_url(lib).rename(lib / "one.txt")
    (lib / "two.txt").write_text("just text, no url\n")
    idx_file = tmp_path / "idx.json"
    scan(lib, index_path=idx_file)
    idx = LibraryIndex.load(idx_file)

    untrackable = untrackable_for_root(idx, lib)
    assert len(untrackable) == 2

    # save=False on the first call; file on disk must be unchanged
    # until we either save explicitly or make a save=True call.
    first_relpath = untrackable[0]["relpath"]
    result_a = promote_untrackable(
        idx, lib, first_relpath,
        url="https://www.fanfiction.net/s/1/1/",
        save=False,
    )
    assert result_a.ok

    mid_reload = LibraryIndex.load(idx_file)
    assert len(untrackable_for_root(mid_reload, lib)) == 2  # not yet persisted

    second_relpath = untrackable[1]["relpath"]
    result_b = promote_untrackable(
        idx, lib, second_relpath,
        url="https://archiveofourown.org/works/999",
        save=True,
    )
    assert result_b.ok

    final = LibraryIndex.load(idx_file)
    assert untrackable_for_root(final, lib) == []
    assert len(list(final.stories_in(lib))) == 2
