"""Reorganizer tests: planner, apply, collisions, subsets, cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.library.index import LibraryIndex
from ffn_dl.library.reorganizer import apply, plan
from ffn_dl.library.scanner import scan

from .library_fixtures import (
    bare_txt_no_url,
    fanficfare_epub,
    ffndl_epub,
)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Create a library dir + index path. Scan is not yet run."""
    lib = tmp_path / "library"
    lib.mkdir()
    return lib, tmp_path / "idx.json"


# ── plan() ────────────────────────────────────────────────────


def test_plan_empty_when_already_organized(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    # File dropped directly at its template-resolved location
    fandom_dir = lib / "Harry Potter"
    fandom_dir.mkdir()
    ffndl_epub(
        fandom_dir,
        title="The Sample Fic",
        author="Test Author",
    )
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    assert moves == []


def test_plan_proposes_move_for_misplaced_file(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    # Dropped at the library root, not in a fandom folder
    ffndl_epub(lib, title="Misplaced", author="Auth")
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    assert len(moves) == 1
    assert moves[0].source.name.endswith(".epub")
    assert "Harry Potter" in str(moves[0].target)


def test_plan_uses_misc_for_multi_fandom(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    fanficfare_epub(
        lib,
        title="Crossover",
        fandoms=("Harry Potter", "The Hobbit"),
    )
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    assert len(moves) == 1
    assert "Misc" in str(moves[0].target)


def test_plan_respects_custom_template(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    ffndl_epub(lib, title="T", author="A")
    scan(lib, index_path=idx_file)

    moves = plan(
        lib,
        index_path=idx_file,
        template="{rating}/{fandom}/{title}.{ext}",
    )
    assert len(moves) == 1
    # rating "T" appears as top-level directory
    assert moves[0].target.relative_to(lib.resolve()).parts[0] == "T"


def test_plan_ignores_untrackable_files(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    bare_txt_no_url(lib)  # LOW confidence → not in stories, not planned
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    assert moves == []


# ── apply() ───────────────────────────────────────────────────


def test_apply_moves_file_and_updates_index(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    path = ffndl_epub(lib, title="Story", author="Auth")
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    assert len(moves) == 1
    result = apply(lib, moves, index_path=idx_file)

    assert result.applied == 1
    assert result.errors == 0
    assert not path.exists()  # Moved out of root
    # Target exists under fandom subdir
    assert (lib / "Harry Potter").is_dir()

    # Index reflects the new location
    idx = LibraryIndex.load(idx_file)
    entries = list(idx.stories_in(lib))
    assert len(entries) == 1
    _, entry = entries[0]
    assert entry["relpath"].startswith("Harry Potter")


def test_apply_skips_missing_source(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    path = ffndl_epub(lib, title="Story", author="Auth")
    scan(lib, index_path=idx_file)
    moves = plan(lib, index_path=idx_file)

    path.unlink()  # User deleted it between scan and apply
    result = apply(lib, moves, index_path=idx_file)

    assert result.applied == 0
    assert result.errors == 1
    assert any("source missing" in m for m in result.messages)


def test_apply_skips_when_target_exists(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    ffndl_epub(lib, title="Story", author="Auth")
    scan(lib, index_path=idx_file)
    moves = plan(lib, index_path=idx_file)

    # Pre-create the target — shouldn't be clobbered
    (lib / "Harry Potter").mkdir()
    (lib / "Harry Potter" / "Story - Auth.epub").write_bytes(b"pre-existing")

    result = apply(lib, moves, index_path=idx_file)
    assert result.applied == 0
    assert result.skipped == 1
    assert (lib / "Harry Potter" / "Story - Auth.epub").read_bytes() == b"pre-existing"


def test_apply_selected_subset_only(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    ffndl_epub(lib, title="Keep", author="A", url="https://www.fanfiction.net/s/1/1/")
    ffndl_epub(lib, title="Move", author="B", url="https://archiveofourown.org/works/2")
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    assert len(moves) == 2

    result = apply(lib, moves, index_path=idx_file, selected_indices=[0])
    assert result.applied == 1
    # The unselected move did not execute — source still at root
    unselected_source = moves[1].source
    assert unselected_source.exists()


def test_apply_creates_parent_directories(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    ffndl_epub(lib, title="Story", author="Auth")
    scan(lib, index_path=idx_file)
    moves = plan(lib, index_path=idx_file)

    assert not (lib / "Harry Potter").exists()  # Subdir not pre-created
    apply(lib, moves, index_path=idx_file)
    assert (lib / "Harry Potter").is_dir()


def test_apply_cleans_empty_source_dir(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    stash = lib / "misorganized"
    stash.mkdir()
    ffndl_epub(stash, title="Story", author="Auth")
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    apply(lib, moves, index_path=idx_file)

    # The now-empty source directory should be removed
    assert not stash.exists()


def test_apply_leaves_nonempty_source_dir(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    stash = lib / "mixed"
    stash.mkdir()
    ffndl_epub(stash, title="Story", author="Auth")
    (stash / "unrelated.txt").write_text("keep me")
    scan(lib, index_path=idx_file)

    moves = plan(lib, index_path=idx_file)
    apply(lib, moves, index_path=idx_file)

    # The sibling file stayed, so the directory stays
    assert stash.exists()
    assert (stash / "unrelated.txt").exists()


def test_apply_empty_moves_list_is_noop(tmp_path: Path):
    lib, idx_file = _setup(tmp_path)
    result = apply(lib, [], index_path=idx_file)
    assert result.applied == 0
    assert result.skipped == 0
    assert result.errors == 0
