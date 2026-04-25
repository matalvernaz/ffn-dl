"""Edge-case coverage added during the Phase 2.5 hardening pass."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from ffn_dl.library.scanner import scan
from ffn_dl.library.template import (
    DEFAULT_TEMPLATE,
    _MAX_SEGMENT_LEN,
    render,
)
from ffn_dl.updater import FileMetadata, extract_metadata

from .library_fixtures import ffndl_epub


# ── Template: path traversal ─────────────────────────────────────


def test_template_strips_dot_dot_segments_in_template():
    md = FileMetadata(title="T", author="A", fandoms=["F"], format="epub")
    # A hostile (or bug-typed) template that tries to escape upward
    path = render(md, template="../{fandom}/{title}.{ext}")
    assert ".." not in path.parts
    assert path.parts[0] == "F"


def test_template_strips_dot_dot_in_metadata_value():
    # Metadata values already pass through _safe; ".." ends up as "_"
    # after stripping dots and spaces. This test pins that behavior.
    md = FileMetadata(title="..", author="A", fandoms=["F"], format="epub")
    path = render(md)
    assert ".." not in path.parts
    # And the title segment is the fallback underscore, not empty
    assert path.parts[1].startswith("_")


def test_template_drops_dot_segments():
    md = FileMetadata(title="T", author="A", fandoms=["F"], format="epub")
    path = render(md, template="./{fandom}/{title}.{ext}")
    assert "." not in path.parts


def test_rendered_path_is_always_relative():
    md = FileMetadata(title="T", author="A", fandoms=["F"], format="epub")
    path = render(md)
    assert not path.is_absolute()


# ── Template: compound crossovers from extract_metadata ──────────


def test_render_routes_single_fandom_compound_crossover_to_misc():
    """``extract_metadata`` (the FicLab path in particular) hands us
    a single-element ``fandoms`` list with the raw "X + Y Crossover"
    string from the file's tags. ``render`` must catch that shape
    and route to the misc folder — otherwise reorganizing existing
    files lands them in a folder literally named for the crossover
    pair, which is the exact bug the FFN download path also had."""
    md = FileMetadata(
        title="T",
        author="A",
        fandoms=["Harry Potter + High School DxD Crossover"],
        format="epub",
    )
    path = render(md)
    assert path.parts[0] == "Misc"


def test_render_compound_crossover_respects_misc_folder_override():
    """The user's configured misc folder name flows through —
    crossovers honour the same ``library_misc_folder`` pref that
    no-fandom and AO3-crossover stories already use."""
    md = FileMetadata(
        title="T",
        author="A",
        fandoms=["Naruto + Bleach Crossover"],
        format="epub",
    )
    path = render(md, misc_folder="Unsorted")
    assert path.parts[0] == "Unsorted"


# ── Template: Windows reserved names ─────────────────────────────


@pytest.mark.parametrize(
    "reserved_name",
    ["CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT3", "com9", "lpt1"],
)
def test_template_rewrites_reserved_name_as_fandom_directory(reserved_name: str):
    # Fandom becomes a directory segment on its own — if Audible ever
    # released "CON: The Saga" as a fandom tag, Windows would reject
    # creating the directory without the underscore prefix.
    md = FileMetadata(title="T", author="A", fandoms=[reserved_name], format="epub")
    path = render(md)
    assert path.parts[0].startswith("_"), path.parts[0]


def test_template_rewrites_reserved_name_as_bare_filename():
    # Template where the entire filename IS the reserved word
    md = FileMetadata(title="CON", author="A", fandoms=["F"], format="epub")
    path = render(md, template="{fandom}/{title}.{ext}")
    assert path.parts[-1].startswith("_CON")


def test_template_preserves_titles_that_contain_reserved_words():
    # "CON - A.epub" has basename "CON - A" which is NOT reserved.
    # Windows only chokes on exact matches of the base before the
    # first dot; leaving this untouched is correct behavior.
    md = FileMetadata(title="CON", author="A", fandoms=["F"], format="epub")
    path = render(md)  # default template renders "CON - A.epub"
    assert path.parts[-1] == "CON - A.epub"
    assert not path.parts[-1].startswith("_")


def test_template_non_reserved_name_unchanged():
    md = FileMetadata(title="Sequel", author="A", fandoms=["F"], format="epub")
    path = render(md)
    assert path.parts[-1] == "Sequel - A.epub"


# ── Template: length cap ─────────────────────────────────────────


def test_template_caps_overlong_segment():
    long_title = "X" * 400
    md = FileMetadata(title=long_title, author="A", fandoms=["F"], format="epub")
    path = render(md)
    filename = path.parts[-1]
    assert len(filename) <= _MAX_SEGMENT_LEN


def test_template_preserves_extension_when_truncating():
    long_title = "X" * 400
    md = FileMetadata(title=long_title, author="A", fandoms=["F"], format="epub")
    path = render(md)
    assert path.parts[-1].endswith(".epub")


def test_template_caps_fandom_directory_segment():
    long_fandom = "Y" * 400
    md = FileMetadata(title="T", author="A", fandoms=[long_fandom], format="epub")
    path = render(md)
    assert len(path.parts[0]) <= _MAX_SEGMENT_LEN


# ── Scanner: symlinks ────────────────────────────────────────────


def test_scanner_skips_symlinked_files(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("symlinks require elevated rights on Windows")

    lib = tmp_path / "lib"
    lib.mkdir()
    real = ffndl_epub(lib, title="Real")
    link = lib / "symlink_copy.epub"
    os.symlink(real, link)

    result = scan(lib, index_path=tmp_path / "idx.json")
    # The real file is indexed exactly once; the symlink is ignored
    assert result.total_files == 1


def test_scanner_survives_symlink_loop(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("symlinks require elevated rights on Windows")

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="Real")
    # Self-referential symlink: lib/loop -> lib
    os.symlink(lib, lib / "loop")

    # Must not hang (os.walk with followlinks=False is the guarantee)
    result = scan(lib, index_path=tmp_path / "idx.json")
    assert result.total_files == 1


# ── Updater: malformed EPUB logging ──────────────────────────────


def test_extract_metadata_logs_warning_on_malformed_epub(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
):
    bad = tmp_path / "not_really.epub"
    # An EPUB is a zip; this is neither. ebooklib will raise.
    bad.write_text("this is not a zip at all", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="ffn_dl.updater"):
        md = extract_metadata(bad)

    # No exception propagates — the contract stays unchanged
    assert md.format == "epub"
    assert md.title is None
    # But the user now gets a log line they can see
    assert any("Failed to read EPUB" in r.message for r in caplog.records)
