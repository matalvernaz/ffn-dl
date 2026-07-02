"""Round-10 F1: heal manifests + cache quarantine + restore-last pieces."""
import json
from pathlib import Path

import pytest

from ficary import heal_manifest as hm


@pytest.fixture
def portable_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("ficary.portable.portable_root", lambda: tmp_path)
    return tmp_path


class TestManifestRoundTrip:
    def test_write_latest_load(self, portable_tmp):
        path = hm.write_manifest(hm.HealManifest(
            label="test heal",
            index_snapshot="/x/index.backup.json",
            dropped_index_entries=3,
        ))
        assert path.exists()
        latest = hm.latest_manifest()
        assert latest is not None
        assert latest.label == "test heal"
        assert latest.index_snapshot == "/x/index.backup.json"
        assert latest.dropped_index_entries == 3
        assert latest.has_anything_to_restore()

    def test_prunes_to_depth_cap(self, portable_tmp):
        for i in range(14):
            hm.write_manifest(hm.HealManifest(label=f"heal {i}"))
        assert len(hm.list_manifests()) == 10

    def test_mark_restored_persists(self, portable_tmp):
        hm.write_manifest(hm.HealManifest(
            label="x", watchlist_snapshot="/w.json"))
        manifest = hm.latest_manifest()
        hm.mark_restored(manifest)
        again = hm.latest_manifest()
        assert again.restored_at  # stamped on disk

    def test_corrupt_manifest_skipped(self, portable_tmp):
        hm.write_manifest(hm.HealManifest(label="good",
                                          index_snapshot="/i.json"))
        newest = hm.list_manifests()[0]
        bad = newest.with_name("heal-99991231-235959-deadbeef.json")
        bad.write_text("{not json", encoding="utf-8")
        latest = hm.latest_manifest()
        assert latest is not None and latest.label == "good"

    def test_nothing_to_restore(self, portable_tmp):
        hm.write_manifest(hm.HealManifest(label="counts only",
                                          dropped_index_entries=2))
        latest = hm.latest_manifest()
        assert not latest.has_anything_to_restore()


class TestCacheQuarantine:
    def _report(self, orphans, cache_root):
        from ficary.cache_doctor import CacheReport
        report = CacheReport(cache_root=cache_root)
        report.orphan_entries = orphans
        return report

    def test_prune_moves_to_trash(self, tmp_path):
        from ficary import cache_doctor
        cache_root = tmp_path / "cache"
        orphan = cache_root / "ffn_123"
        orphan.mkdir(parents=True)
        (orphan / "ch_0001.json").write_text(
            json.dumps({"title": "t", "html": "<p>x</p>"}), encoding="utf-8")
        result = cache_doctor.prune(self._report([orphan], cache_root))
        assert result.pruned == 1
        assert not orphan.exists()
        assert result.quarantine_dir is not None
        moved = result.quarantine_dir / "ffn_123" / "ch_0001.json"
        assert moved.exists()  # recoverable, not deleted

    def test_trash_excluded_from_orphan_scan(self, tmp_path):
        from ficary.cache_doctor import _site_prefix
        assert _site_prefix(".trash") is None

    def test_old_batches_swept(self, tmp_path):
        import os
        import time
        from ficary import cache_doctor
        cache_root = tmp_path / "cache"
        old_batch = cache_root / ".trash" / "20200101-000000"
        old_batch.mkdir(parents=True)
        stale = time.time() - 30 * 24 * 3600
        os.utime(old_batch, (stale, stale))
        orphan = cache_root / "ao3_9"
        orphan.mkdir()
        cache_doctor.prune(self._report([orphan], cache_root))
        assert not old_batch.exists()  # aged out on the next prune
