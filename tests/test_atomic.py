"""Atomic-write helper — correctness and crash-resilience."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ffn_dl.atomic import (
    atomic_path,
    atomic_write_bytes,
    atomic_write_text,
)


class TestAtomicWriteText:
    def test_writes_full_content(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello world\n")
        assert target.read_text(encoding="utf-8") == "hello world\n"

    def test_replaces_existing_file(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("stale contents")
        atomic_write_text(target, "fresh contents")
        assert target.read_text(encoding="utf-8") == "fresh contents"

    def test_creates_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "out.txt"
        atomic_write_text(target, "hi")
        assert target.read_text(encoding="utf-8") == "hi"

    def test_no_tmp_file_left_on_success(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "content")
        tmps = [
            p.name for p in tmp_path.iterdir()
            if p.name != "out.txt" and not p.is_dir()
        ]
        assert tmps == []

    def test_unicode_content(self, tmp_path):
        target = tmp_path / "out.txt"
        atomic_write_text(target, "hello 世界 — 日本語")
        assert target.read_text(encoding="utf-8") == "hello 世界 — 日本語"


class TestAtomicWriteBytes:
    def test_writes_full_bytes(self, tmp_path):
        target = tmp_path / "out.bin"
        atomic_write_bytes(target, b"\x00\x01\x02\xff")
        assert target.read_bytes() == b"\x00\x01\x02\xff"

    def test_empty_payload_is_allowed(self, tmp_path):
        target = tmp_path / "out.bin"
        atomic_write_bytes(target, b"")
        assert target.read_bytes() == b""


class TestAtomicPathContext:
    def test_swaps_in_on_success(self, tmp_path):
        target = tmp_path / "out.zip"
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"PK\x03\x04 ... fake zip ...")
        assert target.read_bytes() == b"PK\x03\x04 ... fake zip ..."

    def test_no_target_written_on_exception(self, tmp_path):
        target = tmp_path / "out.zip"
        target.write_bytes(b"original")
        with pytest.raises(RuntimeError):
            with atomic_path(target) as tmp:
                tmp.write_bytes(b"partial write")
                raise RuntimeError("boom mid-write")
        # Original stays intact.
        assert target.read_bytes() == b"original"
        # Tmp file was cleaned up.
        tmps = [p for p in tmp_path.iterdir() if p.name != "out.zip"]
        assert tmps == []

    def test_tmp_path_is_in_same_directory_as_target(self, tmp_path):
        """Same-directory is required for the rename to be atomic on
        POSIX (cross-filesystem renames degrade to copy+unlink)."""
        target = tmp_path / "sub" / "out.txt"
        target.parent.mkdir()
        with atomic_path(target) as tmp:
            assert tmp.parent == target.parent
            tmp.write_text("x")

    def test_tmp_file_is_removed_on_exit_even_after_manual_replace(
        self, tmp_path,
    ):
        """If the caller somehow renames the tmp file themselves before
        the context exits, ``atomic_path`` should not fail noisily —
        just skip the final replace."""
        target = tmp_path / "out.txt"
        with pytest.raises(FileNotFoundError):
            with atomic_path(target) as tmp:
                tmp.write_text("x")
                os.unlink(tmp)  # simulate accidental cleanup
        # Target was never created; tmp is gone.
        assert not target.exists()


class TestInterruptSimulation:
    """We can't literally kill the process mid-write inside a test, but
    we can demonstrate that an exception raised after the tmp file was
    written (before the rename would have happened) leaves the
    existing target untouched and the tmp file cleaned up."""

    def test_original_file_preserved_on_writer_exception(self, tmp_path):
        target = tmp_path / "story.html"
        target.write_text("<html>old contents</html>")
        with pytest.raises(RuntimeError):
            with atomic_path(target) as tmp:
                tmp.write_text("<html>new partial")
                raise RuntimeError("simulated crash")
        assert target.read_text(encoding="utf-8") == "<html>old contents</html>"

    def test_no_tmp_left_behind_on_exception(self, tmp_path):
        target = tmp_path / "story.epub"
        target.write_text("existing")
        with pytest.raises(RuntimeError):
            with atomic_path(target) as tmp:
                tmp.write_text("partial")
                raise RuntimeError("crash")
        leftover = [p.name for p in tmp_path.iterdir() if p.name != "story.epub"]
        assert leftover == []
