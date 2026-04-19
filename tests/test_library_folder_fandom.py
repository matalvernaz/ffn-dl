"""Tests for the parent-folder fandom fallback in library.identifier.

FicLab (and any future downloader whose HTML doesn't carry a dedicated
fandom field) leaves ``FileMetadata.fandoms`` empty after parsing. When
the library has already been organised into fandom folders, the folder
name is the best available signal — identify() uses the immediate
subfolder under the scan root as a fandom fallback.
"""
from __future__ import annotations

from pathlib import Path

from ffn_dl.library.identifier import identify
from ffn_dl.updater import FileMetadata


def _mk_metadata(**overrides) -> FileMetadata:
    """Build a FileMetadata with sensible defaults."""
    md = FileMetadata(
        source_url="https://www.fanfiction.net/s/12345/",
        title="Some Story",
        author="Some Author",
        format="html",
    )
    for k, v in overrides.items():
        setattr(md, k, v)
    return md


def test_fandom_backfill_uses_immediate_subfolder(tmp_path):
    """A file in ``<root>/Naruto/story.html`` gets fandom ``Naruto``."""
    root = tmp_path / "library"
    fandom_dir = root / "Naruto"
    fandom_dir.mkdir(parents=True)
    path = fandom_dir / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Naruto"]


def test_fandom_backfill_respects_existing_fandoms(tmp_path):
    """If the file's metadata already carried a fandom, we don't
    overwrite it with the folder name — the explicit value wins."""
    root = tmp_path / "library"
    fandom_dir = root / "Harry Potter"
    fandom_dir.mkdir(parents=True)
    path = fandom_dir / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=["Actual Fandom From Metadata"])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Actual Fandom From Metadata"]


def test_fandom_backfill_skipped_for_files_in_library_root(tmp_path):
    """A file directly in the library root has no parent subfolder to
    borrow from — fandoms stay empty rather than borrowing 'library'."""
    root = tmp_path / "library"
    root.mkdir()
    path = root / "flat-file.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == []


def test_fandom_backfill_skips_catch_all_folders(tmp_path):
    """A folder called ``Misc`` or ``Unsorted`` isn't a fandom."""
    root = tmp_path / "library"
    misc_dir = root / "Misc"
    misc_dir.mkdir(parents=True)
    path = misc_dir / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == []


def test_fandom_backfill_when_root_not_supplied(tmp_path):
    """identify() called without ``root`` skips the backfill — the
    caller (older code path, or a test that doesn't care) gets the
    historical behaviour."""
    path = tmp_path / "anywhere" / "story.html"
    path.parent.mkdir(parents=True)
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md)  # no root=

    assert candidate.metadata.fandoms == []


def test_fandom_backfill_uses_first_segment_for_nested_folders(tmp_path):
    """A deeper layout like ``Naruto/Crossovers/story.html`` records
    ``Naruto`` as the fandom — the top-level subfolder is the user's
    primary categorisation signal."""
    root = tmp_path / "library"
    deep = root / "Naruto" / "Crossovers"
    deep.mkdir(parents=True)
    path = deep / "story.html"
    path.write_text("<html></html>", encoding="utf-8")

    md = _mk_metadata(fandoms=[])
    candidate = identify(path, md, root=root)

    assert candidate.metadata.fandoms == ["Naruto"]
