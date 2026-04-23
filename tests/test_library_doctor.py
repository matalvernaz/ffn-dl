"""Library integrity check and self-heal."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ffn_dl.library import check_integrity, heal
from ffn_dl.library.index import LibraryIndex


def _fresh_index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(tmp_path / "library-index.json", {
        "version": 1,
        "libraries": {},
    })


def _make_file(root: Path, rel: str, content: str = "data") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _seed_entry(
    index: LibraryIndex,
    root: Path,
    url: str,
    relpath: str,
    *,
    mtime: float | None = None,
    size: int | None = None,
    duplicates: list[str] | None = None,
) -> None:
    lib = index.library_state(root)
    entry = {
        "relpath": relpath,
        "title": "T",
        "author": "A",
        "fandoms": [],
        "adapter": "ffn",
        "format": "epub",
        "confidence": "high",
        "chapter_count": 1,
        "last_checked": "2026-04-01T00:00:00Z",
    }
    if mtime is not None:
        entry["file_mtime"] = mtime
    if size is not None:
        entry["file_size"] = size
    if duplicates:
        entry["duplicate_relpaths"] = list(duplicates)
    lib["stories"][url] = entry


class TestIntegrityReportClean:
    def test_empty_root_and_empty_index_is_clean(self, tmp_path):
        index = _fresh_index(tmp_path)
        report = check_integrity(tmp_path, index)
        assert report.is_clean()
        assert "clean" in report.summary().lower()

    def test_in_sync_library_is_clean(self, tmp_path):
        _make_file(tmp_path, "Fandom/story.epub")
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "Fandom/story.epub")
        report = check_integrity(tmp_path, index)
        assert report.is_clean()


class TestMissingFileDetection:
    def test_detects_entry_whose_file_is_gone(self, tmp_path):
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "Gone/story.epub")
        report = check_integrity(tmp_path, index)
        assert len(report.missing_files) == 1
        url, entry = report.missing_files[0]
        assert url == "https://x/1"
        assert entry["relpath"] == "Gone/story.epub"
        assert not report.is_clean()

    def test_heal_drops_missing_entries(self, tmp_path):
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "Gone/a.epub")
        _seed_entry(index, tmp_path, "https://x/2", "Gone/b.epub")
        # One real file to keep.
        _make_file(tmp_path, "Real/c.epub")
        _seed_entry(index, tmp_path, "https://x/3", "Real/c.epub")

        report = check_integrity(tmp_path, index)
        result = heal(tmp_path, index, report, drop_missing=True)

        assert result.removed_missing == 2
        # Re-check: only the real entry survives.
        remaining = dict(index.stories_in(tmp_path))
        assert set(remaining) == {"https://x/3"}


class TestOrphanDetection:
    def test_detects_untracked_file(self, tmp_path):
        _make_file(tmp_path, "stray/orphan.epub")
        index = _fresh_index(tmp_path)
        report = check_integrity(tmp_path, index)
        assert len(report.orphan_files) == 1
        assert report.orphan_files[0].name == "orphan.epub"

    def test_ignores_non_fanfic_extensions(self, tmp_path):
        _make_file(tmp_path, "Fandom/note.md")
        _make_file(tmp_path, "Fandom/other.pdf")
        index = _fresh_index(tmp_path)
        report = check_integrity(tmp_path, index)
        assert report.orphan_files == []

    def test_duplicates_are_not_orphans(self, tmp_path):
        _make_file(tmp_path, "A/main.epub")
        _make_file(tmp_path, "A/second.epub")
        index = _fresh_index(tmp_path)
        _seed_entry(
            index, tmp_path, "https://x/1", "A/main.epub",
            duplicates=["A/second.epub"],
        )
        report = check_integrity(tmp_path, index)
        assert report.orphan_files == []


class TestDriftDetection:
    def test_mtime_change_shows_as_drift(self, tmp_path):
        f = _make_file(tmp_path, "F/s.epub", "hello")
        st = f.stat()
        index = _fresh_index(tmp_path)
        _seed_entry(
            index, tmp_path, "https://x/1", "F/s.epub",
            mtime=st.st_mtime - 1000,  # stale
            size=st.st_size,
        )
        report = check_integrity(tmp_path, index)
        assert len(report.drifted_entries) == 1

    def test_size_change_shows_as_drift(self, tmp_path):
        f = _make_file(tmp_path, "F/s.epub", "hello")
        st = f.stat()
        index = _fresh_index(tmp_path)
        _seed_entry(
            index, tmp_path, "https://x/1", "F/s.epub",
            mtime=st.st_mtime,
            size=st.st_size + 9999,  # stale
        )
        report = check_integrity(tmp_path, index)
        assert len(report.drifted_entries) == 1

    def test_entry_without_cached_stat_is_not_drift(self, tmp_path):
        """Entries predating the stat cache shouldn't be flagged —
        "no cache yet" isn't drift."""
        _make_file(tmp_path, "F/s.epub")
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "F/s.epub")
        report = check_integrity(tmp_path, index)
        assert report.drifted_entries == []

    def test_heal_refresh_updates_stat(self, tmp_path):
        f = _make_file(tmp_path, "F/s.epub", "hello")
        st = f.stat()
        index = _fresh_index(tmp_path)
        _seed_entry(
            index, tmp_path, "https://x/1", "F/s.epub",
            mtime=st.st_mtime - 1000,
            size=st.st_size + 5,
        )
        report = check_integrity(tmp_path, index)
        result = heal(tmp_path, index, report, refresh_drift=True)
        assert result.refreshed_drift == 1
        entry = index.lookup_by_url(tmp_path, "https://x/1")
        assert entry["file_mtime"] == st.st_mtime
        assert entry["file_size"] == st.st_size


class TestStaleDuplicates:
    def test_detects_duplicate_pointing_at_gone_file(self, tmp_path):
        _make_file(tmp_path, "F/main.epub")
        # dup2.epub never existed
        index = _fresh_index(tmp_path)
        _seed_entry(
            index, tmp_path, "https://x/1", "F/main.epub",
            duplicates=["F/dup2.epub"],
        )
        report = check_integrity(tmp_path, index)
        assert "https://x/1" in report.stale_duplicate_relpaths
        assert report.stale_duplicate_relpaths["https://x/1"] == ["F/dup2.epub"]

    def test_heal_prunes_stale_duplicates_keeps_primary(self, tmp_path):
        _make_file(tmp_path, "F/main.epub")
        _make_file(tmp_path, "F/real_dup.epub")
        index = _fresh_index(tmp_path)
        _seed_entry(
            index, tmp_path, "https://x/1", "F/main.epub",
            duplicates=["F/real_dup.epub", "F/gone.epub"],
        )
        report = check_integrity(tmp_path, index)
        result = heal(tmp_path, index, report, prune_duplicates=True)
        assert result.removed_stale_duplicates == 1
        entry = index.lookup_by_url(tmp_path, "https://x/1")
        assert entry["duplicate_relpaths"] == ["F/real_dup.epub"]
        assert entry["relpath"] == "F/main.epub"


class TestStaleUntrackable:
    def test_detects_gone_untrackable_record(self, tmp_path):
        _make_file(tmp_path, "F/alive.html")
        index = _fresh_index(tmp_path)
        lib = index.library_state(tmp_path)
        lib["untrackable"].extend([
            {"relpath": "F/alive.html", "format": "html",
             "title": "T", "author": "A", "reason": "no URL"},
            {"relpath": "F/gone.html", "format": "html",
             "title": "T", "author": "A", "reason": "no URL"},
        ])
        report = check_integrity(tmp_path, index)
        assert report.stale_untrackable == [1]

    def test_heal_prunes_stale_untrackable(self, tmp_path):
        _make_file(tmp_path, "F/alive.html")
        index = _fresh_index(tmp_path)
        lib = index.library_state(tmp_path)
        lib["untrackable"].extend([
            {"relpath": "F/alive.html", "reason": "r"},
            {"relpath": "F/gone.html", "reason": "r"},
            {"relpath": "F/also_gone.html", "reason": "r"},
        ])
        report = check_integrity(tmp_path, index)
        result = heal(tmp_path, index, report, prune_untrackable=True)
        assert result.removed_stale_untrackable == 2
        assert len(lib["untrackable"]) == 1
        assert lib["untrackable"][0]["relpath"] == "F/alive.html"


class TestHealIsOptIn:
    def test_heal_with_no_flags_does_nothing(self, tmp_path):
        _make_file(tmp_path, "A/gone_was_here.epub")
        # Create the file, seed the index, delete the file to produce drift.
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "A/gone_was_here.epub")
        (tmp_path / "A" / "gone_was_here.epub").unlink()
        _make_file(tmp_path, "A/orphan.epub")

        report = check_integrity(tmp_path, index)
        assert not report.is_clean()

        result = heal(tmp_path, index, report)  # all flags False by default
        assert result.removed_missing == 0
        assert result.scanned_orphans == 0
        # Index untouched.
        assert index.lookup_by_url(tmp_path, "https://x/1") is not None

    def test_heal_report_summary_reflects_counts(self, tmp_path):
        _make_file(tmp_path, "A/real.epub")
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "A/gone.epub")
        _seed_entry(index, tmp_path, "https://x/2", "A/real.epub")

        report = check_integrity(tmp_path, index)
        result = heal(tmp_path, index, report, drop_missing=True)
        summary = result.summary().lower()
        assert "drop" in summary or "missing" in summary


class TestReportSummary:
    def test_clean_summary_names_the_root(self, tmp_path):
        index = _fresh_index(tmp_path)
        report = check_integrity(tmp_path, index)
        assert str(tmp_path) in report.summary()

    def test_dirty_summary_lists_nonzero_categories(self, tmp_path):
        _make_file(tmp_path, "A/orphan.epub")
        index = _fresh_index(tmp_path)
        _seed_entry(index, tmp_path, "https://x/1", "A/gone.epub")
        report = check_integrity(tmp_path, index)
        summary = report.summary()
        assert "missing" in summary
        assert "not tracked" in summary
        # Categories with 0 items should not appear.
        assert "stale mtime" not in summary
