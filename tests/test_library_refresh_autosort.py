"""Tests for the index-driven refresh engine and auto-sort helpers."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ffn_dl.cli import _apply_library_autosort, _library_subdir_for
from ffn_dl.library.refresh import build_refresh_queue, default_refresh_args
from ffn_dl.library.scanner import scan
from ffn_dl.models import Chapter, Story

from .library_fixtures import (
    bare_txt_no_url,
    ffndl_epub,
)


# ── build_refresh_queue ─────────────────────────────────────────


def _index(tmp_path: Path) -> Path:
    return tmp_path / "idx.json"


def test_build_refresh_queue_empty_library(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    # No scan has been run → index has no entries for this root
    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert queue == []
    assert skipped == []


def test_build_refresh_queue_from_indexed_library(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="A", url="https://www.fanfiction.net/s/1/1/")
    ffndl_epub(lib, title="B", url="https://archiveofourown.org/works/2")
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert len(queue) == 2
    assert skipped == []
    urls = {entry["url"] for entry in queue}
    # Keys are the canonical URL form — FFN's /1/ suffix is stripped by
    # sites.canonical_url so files carrying different URL shapes of the
    # same story collapse to one entry.
    assert urls == {
        "https://www.fanfiction.net/s/1",
        "https://archiveofourown.org/works/2",
    }
    # Each entry has the shape _run_update_queue needs
    for entry in queue:
        assert "path" in entry and entry["path"].exists()
        assert "rel" in entry and entry["rel"]
        assert "local" in entry and entry["local"] > 0


def test_build_refresh_queue_skips_missing_files(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(lib, title="Vanished")
    scan(lib, index_path=_index(tmp_path))
    path.unlink()

    messages: list[str] = []
    queue, skipped = build_refresh_queue(
        lib, index_path=_index(tmp_path), progress=messages.append,
    )
    assert queue == []
    assert len(skipped) == 1
    assert any("missing on disk" in m for m in messages)


def test_build_refresh_queue_accepts_index_count_for_foreign_format(
    tmp_path: Path,
):
    # A foreign-format file whose chapter files don't match ffn-dl's
    # `chapter_*` convention makes count_chapters return 0. The
    # refresh engine should fall back to the index's recorded count
    # so the story still gets probed.
    from ebooklib import epub

    lib = tmp_path / "lib"
    lib.mkdir()

    book = epub.EpubBook()
    book.set_identifier("foreign-id")
    book.set_title("Foreign Naming")
    book.add_author("Author")
    book.add_metadata("DC", "source", "https://www.royalroad.com/fiction/999")
    book.add_metadata("DC", "subject", "Harry Potter")
    # Chapter file name that count_chapters won't recognise
    ch = epub.EpubHtml(title="Ch 1", file_name="OEBPS/Text/ch001.xhtml")
    ch.content = b"<p>body</p>"
    book.add_item(ch)
    book.toc = [ch]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]
    story_path = lib / "foreign.epub"
    epub.write_epub(str(story_path), book)

    scan(lib, index_path=_index(tmp_path))

    # Patch the index entry's chapter_count to a known positive value
    # so we can assert the fallback picked it up.
    import json
    data = json.loads((_index(tmp_path)).read_text())
    lib_key = next(iter(data["libraries"]))
    for url, entry in data["libraries"][lib_key]["stories"].items():
        entry["chapter_count"] = 3
    (_index(tmp_path)).write_text(json.dumps(data))

    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert len(queue) == 1
    assert queue[0]["local"] == 3


def test_build_refresh_queue_ignores_untrackable_files(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bare_txt_no_url(lib)  # LOW confidence → lands in untrackable, not stories
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert queue == []
    assert skipped == []  # Not even visited — it's not a story


# ── default_refresh_args ─────────────────────────────────────────


def test_default_refresh_args_has_scraper_fields():
    args = default_refresh_args()
    # Every field _build_scraper reads off args needs to be present,
    # otherwise the GUI path will AttributeError at runtime.
    for field in (
        "max_retries",
        "no_cache",
        "delay_min",
        "delay_max",
        "chunk_size",
        "use_wayback",
        "dry_run",
        "skip_complete",
        "probe_workers",
        "format",
        "output",
        "chapters",
        "hr_as_stars",
        "strip_notes",
    ):
        assert hasattr(args, field), field


def test_default_refresh_args_honors_overrides():
    args = default_refresh_args(dry_run=True, skip_complete=True, workers=9)
    assert args.dry_run is True
    assert args.skip_complete is True
    assert args.probe_workers == 9


# ── Auto-sort ────────────────────────────────────────────────────


def _story(fandom: str | None = "Harry Potter") -> Story:
    s = Story(
        id=1,
        title="Demo",
        author="A",
        summary="",
        url="https://www.fanfiction.net/s/1/1/",
    )
    if fandom is not None:
        s.metadata["category"] = fandom
    s.chapters = [Chapter(number=1, title="Ch 1", html="<p>x</p>")]
    return s


def _autosort_args(**overrides) -> Namespace:
    args = Namespace(
        output=None,
        format="epub",
        _library_autosort=True,
        _library_template="{fandom}/{title} - {author}.{ext}",
        _library_misc="Misc",
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def test_library_subdir_none_when_autosort_disabled():
    args = _autosort_args(_library_autosort=False)
    assert _library_subdir_for(_story(), args) is None


def test_library_subdir_uses_fandom():
    subdir = _library_subdir_for(_story("Harry Potter"), _autosort_args())
    assert subdir == Path("Harry Potter")


def test_library_subdir_routes_to_misc_without_fandom():
    subdir = _library_subdir_for(_story(fandom=None), _autosort_args())
    assert subdir == Path("Misc")


def test_library_subdir_splits_comma_separated_fandoms_to_misc():
    # AO3-style "Fandom A, Fandom B" is multi-fandom → Misc
    subdir = _library_subdir_for(
        _story("Harry Potter, The Hobbit"), _autosort_args(),
    )
    assert subdir == Path("Misc")


def test_apply_library_autosort_noop_when_output_explicit():
    args = Namespace(output="/some/path")
    _apply_library_autosort(args)
    assert args.output == "/some/path"
    assert not getattr(args, "_library_autosort", False)


def test_apply_library_autosort_noop_when_no_library_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    from ffn_dl import prefs as _prefs

    # Simulate an unset preference by forcing Prefs.get to return "".
    # The DEFAULTS dict doesn't include KEY_LIBRARY_PATH, so a fresh
    # install already returns "" — but we assert it explicitly.
    class _FakePrefs:
        def get(self, key, default=None):
            return ""

    monkeypatch.setattr(_prefs, "Prefs", _FakePrefs)
    args = Namespace(output=None)
    _apply_library_autosort(args)
    assert args.output is None
    assert not getattr(args, "_library_autosort", False)


def test_apply_library_autosort_sets_routing_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from ffn_dl import prefs as _prefs

    lib = tmp_path / "my-library"

    class _FakePrefs:
        def get(self, key, default=None):
            if key == _prefs.KEY_LIBRARY_PATH:
                return str(lib)
            if key == _prefs.KEY_LIBRARY_PATH_TEMPLATE:
                return "{fandom}/{title}.{ext}"
            if key == _prefs.KEY_LIBRARY_MISC_FOLDER:
                return "Other"
            return default

    monkeypatch.setattr(_prefs, "Prefs", _FakePrefs)
    args = Namespace(output=None)
    _apply_library_autosort(args)

    assert args.output == str(lib)
    assert args._library_autosort is True
    assert args._library_template == "{fandom}/{title}.{ext}"
    assert args._library_misc == "Other"
