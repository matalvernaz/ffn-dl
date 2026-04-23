"""Integrated --doctor: library + watchlist + cache in one pass."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ffn_dl.doctor import check_all, heal_all
from ffn_dl.library.index import LibraryIndex
from ffn_dl.watchlist import (
    Watch,
    WATCH_TYPE_STORY,
    WatchlistStore,
)


def _fresh_index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(
        tmp_path / "library-index.json",
        {"version": 1, "libraries": {}},
    )


def _fresh_watchlist(tmp_path: Path) -> WatchlistStore:
    return WatchlistStore(tmp_path / "watchlist.json")


def _make_watch(**kwargs) -> Watch:
    defaults = dict(
        id=uuid.uuid4().hex,
        type=WATCH_TYPE_STORY,
        site="ffn",
        target="https://www.fanfiction.net/s/1",
        label="",
        channels=["log"],
        enabled=True,
        query="",
        filters={},
        last_seen=None,
        last_checked_at="",
        last_error="",
        cooldown_until="",
        created_at="",
    )
    defaults.update(kwargs)
    return Watch(**defaults)


def _seed_lib(index, root, url, relpath):
    lib = index.library_state(root)
    lib["stories"][url] = {
        "relpath": relpath,
        "title": "T", "author": "A",
        "fandoms": [], "adapter": "ffn",
        "format": "epub", "confidence": "high",
        "chapter_count": 1, "last_checked": "2026-04-01T00:00:00Z",
    }


class TestCheckAll:
    def test_empty_everything_is_clean(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        assert report.is_clean()

    def test_library_drift_flagged(self, tmp_path):
        idx = _fresh_index(tmp_path)
        # Seed a library whose file is missing.
        root = tmp_path / "lib"
        root.mkdir()
        _seed_lib(idx, root, "https://x/1", "missing.epub")

        wl = _fresh_watchlist(tmp_path)
        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        assert not report.is_clean()
        assert root in report.library_reports
        assert report.library_reports[root].missing_files

    def test_watchlist_drift_flagged(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        wl.add(_make_watch(target=""))
        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        assert not report.is_clean()
        assert report.watchlist_report.empty_target

    def test_cache_report_always_produced(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        # Empty cache still yields a report (with total_entries=0).
        assert report.cache_report is not None


class TestSummary:
    def test_clean_summary_has_all_three_sections(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        summary = check_all(index=idx, watchlist=wl).summary()
        assert "== Library ==" in summary
        assert "== Watchlist ==" in summary
        assert "== Scraper cache ==" in summary

    def test_dirty_watchlist_shows_in_summary(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        wl.add(_make_watch(target=""))
        summary = check_all(index=idx, watchlist=wl).summary()
        assert "empty target" in summary

    def test_no_libraries_section_noted(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        summary = check_all(index=idx, watchlist=wl).summary()
        assert "no library roots" in summary.lower()


class TestHealAll:
    def test_heals_library_and_watchlist_together(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)

        # Library dirt: entry with missing file.
        root = tmp_path / "lib"
        root.mkdir()
        _seed_lib(idx, root, "https://x/1", "missing.epub")

        # Watchlist dirt: one with empty target.
        wl.add(_make_watch(target=""))

        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        result = heal_all(report, index=idx, watchlist=wl, auto_backup=False)

        # Library entry dropped.
        assert root in result.library_heals
        assert result.library_heals[root].removed_missing == 1
        # Watchlist entry dropped.
        assert result.watchlist_heal.removed == 1

    def test_auto_backup_on_by_default(self, tmp_path):
        idx = _fresh_index(tmp_path)
        idx.save()  # ensure file exists for backup to copy
        wl = _fresh_watchlist(tmp_path)

        # Make something to heal so the heal path runs.
        root = tmp_path / "lib"
        root.mkdir()
        _seed_lib(idx, root, "https://x/1", "missing.epub")
        idx.save()  # persist the seeded entry

        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        result = heal_all(report, index=idx, watchlist=wl)
        assert result.index_backups
        # Backup file exists on disk.
        assert result.index_backups[0].exists()

    def test_auto_backup_can_be_disabled(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        result = heal_all(
            report, index=idx, watchlist=wl, auto_backup=False,
        )
        assert result.index_backups == []

    def test_heal_result_summary_reflects_work(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)

        root = tmp_path / "lib"
        root.mkdir()
        _seed_lib(idx, root, "https://x/1", "missing.epub")
        wl.add(_make_watch(target=""))

        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        result = heal_all(report, index=idx, watchlist=wl, auto_backup=False)
        summary = result.summary()
        assert "Library" in summary
        assert "Watchlist" in summary

    def test_clean_report_heal_is_nop(self, tmp_path):
        idx = _fresh_index(tmp_path)
        wl = _fresh_watchlist(tmp_path)
        report = check_all(index=idx, watchlist=wl, cache_root=tmp_path / "cache")
        result = heal_all(
            report, index=idx, watchlist=wl, auto_backup=False,
        )
        assert result.summary() == "No changes."
