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


class TestPermissionPreservation:
    """``mkstemp`` creates files with mode 0600. Without explicit
    preservation, the atomic replace clobbers the target's prior mode
    — a shared NAS file becomes private and other tools (Plex,
    Calibre, the next ffn-dl run under a different uid) lose read
    access. The fix mirrors the existing target's mode onto the temp
    before the rename.

    Skipped on Windows because ``chmod`` is mostly a no-op there and
    permission bits aren't the failure mode being protected."""

    def test_atomic_write_text_keeps_target_mode(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")
        target = tmp_path / "out.txt"
        target.write_text("stale")
        os.chmod(target, 0o644)
        atomic_write_text(target, "fresh")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o644

    def test_atomic_write_bytes_keeps_target_mode(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")
        target = tmp_path / "out.bin"
        target.write_bytes(b"stale")
        os.chmod(target, 0o664)
        atomic_write_bytes(target, b"fresh")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o664

    def test_atomic_path_keeps_target_mode(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")
        target = tmp_path / "out.epub"
        target.write_bytes(b"stale")
        os.chmod(target, 0o644)
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"fresh")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o644

    def test_new_file_gets_secure_default(self, tmp_path):
        """When there's no pre-existing target, mkstemp's 0600 is the
        right default — we don't want to invent a permissive mode for
        a brand-new file."""
        if os.name == "nt":
            pytest.skip("POSIX permission semantics only")
        target = tmp_path / "brand-new.txt"
        atomic_write_text(target, "hi")
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600


class TestAtomicPathReplaceFailure:
    """If ``os.replace`` fails on the success branch — Windows file
    lock, antivirus interception, target-directory permission flip —
    the temp file used to be orphaned because the cleanup only ran on
    the yield-exception path. Now both paths clean up."""

    def test_tmp_cleaned_when_replace_fails(self, tmp_path, monkeypatch):
        target = tmp_path / "out.epub"
        target.write_bytes(b"existing")

        import ffn_dl.atomic as atomic_mod
        orig_replace = atomic_mod.os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(atomic_mod.os, "replace", boom)

        with pytest.raises(OSError):
            with atomic_path(target) as tmp:
                tmp.write_bytes(b"new content")
        leftover = [p.name for p in tmp_path.iterdir() if p.name != "out.epub"]
        assert leftover == [], f"expected no temp leftovers, got {leftover}"

        # Sanity: the un-monkeypatched replace still works.
        monkeypatch.setattr(atomic_mod.os, "replace", orig_replace)
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"recovered")
        assert target.read_bytes() == b"recovered"
