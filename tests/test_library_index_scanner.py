"""Index, template, and scanner tests for the library manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.library.candidate import Confidence, StoryCandidate
from ffn_dl.library.index import LibraryIndex, SCHEMA_VERSION
from ffn_dl.library.scanner import scan
from ffn_dl.library.template import (
    DEFAULT_MISC_FOLDER,
    DEFAULT_TEMPLATE,
    render,
)
from ffn_dl.updater import FileMetadata, extract_metadata

from .library_fixtures import (
    bare_html_with_url,
    bare_txt_no_url,
    fanficfare_epub,
    ffndl_epub,
    ffndl_html,
    ffndl_txt,
    fichub_epub,
)


# ── Template renderer ──────────────────────────────────────────


def test_render_default_single_fandom():
    md = FileMetadata(
        title="Story Title",
        author="An Author",
        fandoms=["Harry Potter"],
        format="epub",
    )
    path = render(md)
    assert path == Path("Harry Potter") / "Story Title - An Author.epub"


def test_render_no_fandom_goes_to_misc():
    md = FileMetadata(
        title="T",
        author="A",
        fandoms=[],
        format="epub",
    )
    path = render(md)
    assert path == Path(DEFAULT_MISC_FOLDER) / "T - A.epub"


def test_render_multi_fandom_goes_to_misc():
    md = FileMetadata(
        title="T",
        author="A",
        fandoms=["Harry Potter", "The Hobbit"],
        format="epub",
    )
    path = render(md)
    assert path == Path(DEFAULT_MISC_FOLDER) / "T - A.epub"


def test_render_strips_filesystem_unsafe_chars():
    md = FileMetadata(
        title='What/a"bad<title>',
        author="Normal",
        fandoms=["Good Fandom"],
        format="epub",
    )
    path = render(md)
    # Slashes and other unsafe chars replaced with underscores,
    # path structure preserved
    assert len(path.parts) == 2
    assert path.parts[0] == "Good Fandom"
    assert "/" not in path.parts[1]
    assert '"' not in path.parts[1]
    assert "<" not in path.parts[1]


def test_render_title_with_slash_does_not_split_path():
    md = FileMetadata(
        title="A/B",
        author="Author",
        fandoms=["Fandom"],
        format="epub",
    )
    path = render(md)
    assert len(path.parts) == 2
    # The "A/B" became a single sanitized filename component
    assert "A" in path.parts[1] and "B" in path.parts[1]


def test_render_custom_template():
    md = FileMetadata(
        title="T", author="A", fandoms=["F"], rating="M",
        status="Complete", format="epub",
    )
    path = render(md, template="{rating}/{fandom}/{status} - {title}.{ext}")
    assert path == Path("M") / "F" / "Complete - T.epub"


def test_render_unknown_placeholder_raises():
    md = FileMetadata(title="T", author="A", fandoms=["F"], format="epub")
    with pytest.raises(ValueError, match="Unknown placeholder"):
        render(md, template="{bogus}/{title}.{ext}")


def test_render_missing_fields_use_fallbacks():
    md = FileMetadata(fandoms=["F"], format="epub")
    path = render(md)
    assert path.parts[0] == "F"
    assert "Unknown Title" in path.parts[1]
    assert "Unknown Author" in path.parts[1]


# ── Index round-trip ───────────────────────────────────────────


def _candidate_at(path: Path) -> StoryCandidate:
    md = extract_metadata(path)
    from ffn_dl.library.identifier import identify
    return identify(path, md)


def test_index_save_load_roundtrip(tmp_path: Path):
    library = tmp_path / "lib"
    library.mkdir()
    story_path = ffndl_epub(library)

    index_file = tmp_path / "idx.json"
    idx = LibraryIndex.load(index_file)
    idx.record(library, _candidate_at(story_path))
    idx.mark_scan_complete(library)
    idx.save()

    # Second instance loads the same data
    idx2 = LibraryIndex.load(index_file)
    entries = list(idx2.stories_in(library))
    assert len(entries) == 1
    url, entry = entries[0]
    assert url == "https://www.fanfiction.net/s/12345/1/"
    assert entry["title"] == "The Sample Fic"
    assert entry["adapter"] == "ffn"
    assert entry["confidence"] == "high"


def test_index_missing_file_returns_empty(tmp_path: Path):
    idx = LibraryIndex.load(tmp_path / "does_not_exist.json")
    assert idx.library_roots() == []


def test_index_corrupt_file_returns_empty(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("this is not json{{", encoding="utf-8")
    idx = LibraryIndex.load(path)
    assert idx.library_roots() == []


def test_index_wrong_schema_version_returns_empty(tmp_path: Path):
    import json
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"version": SCHEMA_VERSION + 99, "libraries": {}}),
        encoding="utf-8",
    )
    idx = LibraryIndex.load(path)
    assert idx.library_roots() == []


def test_index_multiple_libraries_keyed_separately(tmp_path: Path):
    lib_a = tmp_path / "a"
    lib_b = tmp_path / "b"
    lib_a.mkdir()
    lib_b.mkdir()
    ffndl_epub(lib_a, title="A's Fic", url="https://www.fanfiction.net/s/1/1/")
    ffndl_epub(lib_b, title="B's Fic", url="https://archiveofourown.org/works/2")

    idx_file = tmp_path / "idx.json"
    idx = LibraryIndex.load(idx_file)
    for story in lib_a.iterdir():
        idx.record(lib_a, _candidate_at(story))
    for story in lib_b.iterdir():
        idx.record(lib_b, _candidate_at(story))
    idx.save()

    reloaded = LibraryIndex.load(idx_file)
    assert len(reloaded.library_roots()) == 2
    assert len(list(reloaded.stories_in(lib_a))) == 1
    assert len(list(reloaded.stories_in(lib_b))) == 1


def test_index_update_in_place(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    story_path = ffndl_epub(lib)

    idx = LibraryIndex.load(tmp_path / "idx.json")
    idx.record(lib, _candidate_at(story_path))
    idx.record(lib, _candidate_at(story_path))  # Same URL, second time

    # Only one entry per URL
    entries = list(idx.stories_in(lib))
    assert len(entries) == 1


def test_index_low_confidence_goes_to_untrackable(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    orphan = bare_txt_no_url(lib)

    idx = LibraryIndex.load(tmp_path / "idx.json")
    idx.record(lib, _candidate_at(orphan))

    assert list(idx.stories_in(lib)) == []
    untrackable = idx.untrackable_in(lib)
    assert len(untrackable) == 1
    assert untrackable[0]["format"] == "txt"


def test_index_clear_library_removes_entries(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    story_path = ffndl_epub(lib)

    idx = LibraryIndex.load(tmp_path / "idx.json")
    idx.record(lib, _candidate_at(story_path))
    assert len(list(idx.stories_in(lib))) == 1

    idx.clear_library(lib)
    assert list(idx.stories_in(lib)) == []


# ── Scanner integration ────────────────────────────────────────


def test_scan_mixed_library(tmp_path: Path):
    lib = tmp_path / "library"
    lib.mkdir()

    # Mixed provenance: ffn-dl's own, FFF, FicHub, bare-with-URL, no-URL
    ffndl_epub(lib, title="Own Epub")
    ffndl_html(lib, title="Own Html", url="https://www.royalroad.com/fiction/1")
    ffndl_txt(lib, title="Own Txt", url="https://archiveofourown.org/works/1")
    fanficfare_epub(lib)
    fichub_epub(lib)
    bare_html_with_url(lib, "https://www.fanfiction.net/s/88/1/")
    bare_txt_no_url(lib)

    index_file = tmp_path / "idx.json"
    result = scan(lib, index_path=index_file)

    assert result.total_files == 7
    # Six have resolvable URLs, one is the orphan TXT
    assert result.identified_via_url == 6
    assert result.ambiguous == 1
    assert result.errors == 0

    idx = LibraryIndex.load(index_file)
    tracked = list(idx.stories_in(lib))
    assert len(tracked) == 6
    assert len(idx.untrackable_in(lib)) == 1


def test_scan_recursive_walks_subdirs(tmp_path: Path):
    lib = tmp_path / "lib"
    sub = lib / "sub"
    sub.mkdir(parents=True)

    ffndl_epub(lib, title="Top")
    ffndl_epub(sub, title="Nested", url="https://www.royalroad.com/fiction/7")

    result = scan(lib, index_path=tmp_path / "idx.json", recursive=True)
    assert result.total_files == 2
    assert result.identified_via_url == 2


def test_scan_non_recursive_skips_subdirs(tmp_path: Path):
    lib = tmp_path / "lib"
    sub = lib / "sub"
    sub.mkdir(parents=True)

    ffndl_epub(lib, title="Top")
    ffndl_epub(sub, title="Nested", url="https://www.royalroad.com/fiction/7")

    result = scan(lib, index_path=tmp_path / "idx.json", recursive=False)
    assert result.total_files == 1


def test_scan_clear_existing_drops_orphans(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()

    a = ffndl_epub(lib, title="A", url="https://www.fanfiction.net/s/1/1/")
    ffndl_epub(lib, title="B", url="https://archiveofourown.org/works/2")

    idx_file = tmp_path / "idx.json"
    scan(lib, index_path=idx_file)
    assert len(list(LibraryIndex.load(idx_file).stories_in(lib))) == 2

    # Delete A off disk, rescan with clear_existing — A should vanish
    a.unlink()
    scan(lib, index_path=idx_file, clear_existing=True)
    stories = list(LibraryIndex.load(idx_file).stories_in(lib))
    assert len(stories) == 1


def test_scan_rejects_nondir(tmp_path: Path):
    with pytest.raises(NotADirectoryError):
        scan(tmp_path / "does_not_exist", index_path=tmp_path / "idx.json")
