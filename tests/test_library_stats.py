"""Library stats — counts, distributions, freshness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ffn_dl.library import compute_stats
from ffn_dl.library.index import LibraryIndex


def _fresh_index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(tmp_path / "library-index.json", {
        "version": 1,
        "libraries": {},
    })


def _iso_days_ago(n: int) -> str:
    when = datetime.now(tz=timezone.utc) - timedelta(days=n)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(
    index: LibraryIndex,
    root: Path,
    url: str,
    *,
    site: str = "ffn",
    status: str = "Complete",
    fandoms: list[str] | None = None,
    format: str = "epub",
    chapter_count: int = 1,
    last_probed: str | None = None,
    remote_chapter_count: int | None = None,
    duplicates: list[str] | None = None,
) -> None:
    entry = {
        "relpath": f"path/{url.rsplit('/', 1)[-1]}.{format}",
        "title": "T",
        "author": "A",
        "fandoms": fandoms or [],
        "adapter": site,
        "format": format,
        "confidence": "high",
        "chapter_count": chapter_count,
        "status": status,
        "last_checked": _iso_days_ago(0),
    }
    if last_probed is not None:
        entry["last_probed"] = last_probed
    if remote_chapter_count is not None:
        entry["remote_chapter_count"] = remote_chapter_count
    if duplicates:
        entry["duplicate_relpaths"] = duplicates
    index.library_state(root)["stories"][url] = entry


class TestBasicCounts:
    def test_empty_library_reports_zeros(self, tmp_path):
        idx = _fresh_index(tmp_path)
        stats = compute_stats(tmp_path, idx)
        assert stats.total_stories == 0
        assert stats.total_chapters == 0
        assert stats.untrackable_files == 0

    def test_counts_chapters_across_stories(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", chapter_count=5)
        _seed(idx, tmp_path, "https://x/2", chapter_count=10)
        _seed(idx, tmp_path, "https://x/3", chapter_count=1)
        stats = compute_stats(tmp_path, idx)
        assert stats.total_stories == 3
        assert stats.total_chapters == 16

    def test_counts_untrackable(self, tmp_path):
        idx = _fresh_index(tmp_path)
        lib = idx.library_state(tmp_path)
        lib["untrackable"].extend([
            {"relpath": "a.html"}, {"relpath": "b.html"},
        ])
        stats = compute_stats(tmp_path, idx)
        assert stats.untrackable_files == 2


class TestDistributions:
    def test_by_site_counts_per_adapter(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", site="ffn")
        _seed(idx, tmp_path, "https://x/2", site="ffn")
        _seed(idx, tmp_path, "https://x/3", site="ao3")
        stats = compute_stats(tmp_path, idx)
        assert stats.by_site["ffn"] == 2
        assert stats.by_site["ao3"] == 1

    def test_by_status(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", status="Complete")
        _seed(idx, tmp_path, "https://x/2", status="Complete")
        _seed(idx, tmp_path, "https://x/3", status="In-Progress")
        stats = compute_stats(tmp_path, idx)
        assert stats.by_status["Complete"] == 2
        assert stats.by_status["In-Progress"] == 1

    def test_top_fandoms_limited_and_ordered(self, tmp_path):
        idx = _fresh_index(tmp_path)
        # 15 stories across 3 fandoms with different frequencies.
        for i in range(10):
            _seed(idx, tmp_path, f"https://x/a{i}", fandoms=["Harry Potter"])
        for i in range(5):
            _seed(idx, tmp_path, f"https://x/b{i}", fandoms=["Naruto"])
        _seed(idx, tmp_path, "https://x/c1", fandoms=["ATLA"])
        stats = compute_stats(tmp_path, idx)
        # Sorted descending by count.
        assert stats.top_fandoms[0] == ("Harry Potter", 10)
        assert stats.top_fandoms[1] == ("Naruto", 5)
        assert stats.top_fandoms[2] == ("ATLA", 1)

    def test_unknown_buckets_when_fields_missing(self, tmp_path):
        idx = _fresh_index(tmp_path)
        entry = {
            "relpath": "x.epub", "title": "T", "author": "A",
            "fandoms": [], "chapter_count": 1,
            "last_checked": _iso_days_ago(0),
        }
        idx.library_state(tmp_path)["stories"]["https://x/1"] = entry
        stats = compute_stats(tmp_path, idx)
        assert stats.by_site["unknown"] == 1


class TestFreshness:
    def test_never_probed_counted(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1")  # no last_probed
        _seed(idx, tmp_path, "https://x/2", last_probed=_iso_days_ago(1))
        stats = compute_stats(tmp_path, idx)
        assert stats.never_probed == 1

    def test_stale_probe_counted(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", last_probed=_iso_days_ago(45))  # stale
        _seed(idx, tmp_path, "https://x/2", last_probed=_iso_days_ago(5))   # fresh
        stats = compute_stats(tmp_path, idx)
        assert stats.stale_probe == 1

    def test_malformed_probe_timestamp_treated_as_never(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", last_probed="not-a-date")
        stats = compute_stats(tmp_path, idx)
        assert stats.never_probed == 1
        assert stats.stale_probe == 0

    def test_pending_updates_when_remote_exceeds_local(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(
            idx, tmp_path, "https://x/1",
            chapter_count=5, remote_chapter_count=8,
        )
        _seed(
            idx, tmp_path, "https://x/2",
            chapter_count=10, remote_chapter_count=10,
        )
        stats = compute_stats(tmp_path, idx)
        assert stats.pending_updates == 1


class TestDuplicates:
    def test_duplicate_relpaths_counted(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(
            idx, tmp_path, "https://x/1",
            duplicates=["a.epub", "b.epub"],
        )
        _seed(idx, tmp_path, "https://x/2")
        stats = compute_stats(tmp_path, idx)
        assert stats.duplicate_files == 2


class TestSummaryRendering:
    def test_empty_summary_still_mentions_root(self, tmp_path):
        idx = _fresh_index(tmp_path)
        stats = compute_stats(tmp_path, idx)
        summary = stats.summary()
        assert str(tmp_path) in summary
        assert "0" in summary

    def test_summary_lists_per_site_counts(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", site="ao3")
        stats = compute_stats(tmp_path, idx)
        summary = stats.summary()
        assert "ao3" in summary
        assert "Stories tracked:" in summary

    def test_summary_lists_top_fandoms(self, tmp_path):
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", fandoms=["Harry Potter"])
        stats = compute_stats(tmp_path, idx)
        summary = stats.summary()
        assert "Harry Potter" in summary

    def test_summary_omits_empty_categories(self, tmp_path):
        """A clean library shouldn't list "Never probed: 0" etc. —
        empty categories are noise."""
        idx = _fresh_index(tmp_path)
        _seed(idx, tmp_path, "https://x/1", last_probed=_iso_days_ago(1))
        stats = compute_stats(tmp_path, idx)
        summary = stats.summary()
        assert "Never probed" not in summary
        assert "Pending updates" not in summary
