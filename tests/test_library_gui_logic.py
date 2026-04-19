"""Tests for wx-free helpers used by the library GUI dialogs."""

from __future__ import annotations

from pathlib import Path

from ffn_dl.library.gui_logic import format_move_label, relative_to_root
from ffn_dl.library.reorganizer import MoveOp


def _op(source: str, target: str, url: str = "https://x/y/1/") -> MoveOp:
    return MoveOp(source=Path(source), target=Path(target), source_url=url)


def test_relative_to_root_under_root():
    root = Path("/lib")
    p = Path("/lib/Harry Potter/Fic.epub")
    assert relative_to_root(p, root) == "Harry Potter/Fic.epub"


def test_relative_to_root_outside_root_returns_absolute():
    root = Path("/lib")
    p = Path("/elsewhere/Fic.epub")
    assert relative_to_root(p, root) == "/elsewhere/Fic.epub"


def test_format_move_label_checked_prefix():
    root = Path("/lib")
    op = _op("/lib/Fic.epub", "/lib/Harry Potter/Fic.epub")
    label = format_move_label(op, root, checked=True)
    assert label.startswith("[x] ")


def test_format_move_label_unchecked_prefix():
    root = Path("/lib")
    op = _op("/lib/Fic.epub", "/lib/Harry Potter/Fic.epub")
    label = format_move_label(op, root, checked=False)
    assert label.startswith("[ ] ")


def test_format_move_label_uses_arrow_for_relocation():
    root = Path("/lib")
    op = _op("/lib/Fic.epub", "/lib/Harry Potter/Fic.epub")
    label = format_move_label(op, root, checked=True)
    assert "→" in label
    assert "renamed to" not in label


def test_format_move_label_uses_renamed_phrasing_for_rename():
    # Same parent directory — it's a pure rename, not a move
    root = Path("/lib")
    op = _op("/lib/Harry Potter/A.epub", "/lib/Harry Potter/B.epub")
    label = format_move_label(op, root, checked=True)
    assert "renamed to" in label
    assert "→" not in label


def test_format_move_label_uses_paths_relative_to_root():
    root = Path("/lib")
    op = _op("/lib/misplaced/Fic.epub", "/lib/Harry Potter/Fic.epub")
    label = format_move_label(op, root, checked=True)
    assert "misplaced/Fic.epub" in label
    assert "Harry Potter/Fic.epub" in label
    # Full absolute paths shouldn't leak through when under root
    assert "/lib/misplaced" not in label
    assert "/lib/Harry Potter" not in label


def test_format_move_label_stable_for_identical_inputs():
    root = Path("/lib")
    op = _op("/lib/a.epub", "/lib/b/a.epub")
    assert format_move_label(op, root, True) == format_move_label(op, root, True)
