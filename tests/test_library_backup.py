"""Library-index backup / restore / rolling-prune."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ffn_dl.library.backup import (
    _MAX_BACKUPS,
    backup,
    list_backups,
    restore,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestBackup:
    def test_returns_none_when_index_missing(self, tmp_path):
        assert backup(tmp_path / "missing.json") is None

    def test_creates_timestamped_sibling(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{"version": 1}')
        out = backup(idx)
        assert out is not None
        assert out.parent == idx.parent
        assert out.name.startswith("library-index.backup-")
        assert out.name.endswith(".json")
        assert out.read_text() == '{"version": 1}'

    def test_backups_are_distinct_content(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{"n": 1}')
        out1 = backup(idx)
        # Ensure a different timestamp (same-second backups collide on
        # filename — our rolling scheme is 1-second granularity).
        time.sleep(1.1)
        _write(idx, '{"n": 2}')
        out2 = backup(idx)
        assert out1.name != out2.name
        assert out1.read_text() == '{"n": 1}'
        assert out2.read_text() == '{"n": 2}'

    def test_list_backups_is_newest_first(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{}')
        earlier = idx.with_name("library-index.backup-20200101-000000.json")
        later = idx.with_name("library-index.backup-20260101-000000.json")
        _write(earlier, "a")
        _write(later, "b")

        listed = list_backups(idx)
        assert listed[0] == later
        assert listed[1] == earlier


class TestRollingPrune:
    def test_prunes_past_max_backups(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{}')

        # Pre-seed a stack of synthesised backups past the cap.
        for i in range(_MAX_BACKUPS + 5):
            ts = f"202601{i:02d}-000000"
            _write(
                idx.with_name(f"library-index.backup-{ts}.json"),
                f"bkp {i}",
            )
        # One more via the real API should prune the oldest.
        backup(idx)
        listed = list_backups(idx)
        assert len(listed) == _MAX_BACKUPS
        # The oldest (20260100) should be gone.
        names = [p.name for p in listed]
        assert "library-index.backup-20260100-000000.json" not in names

    def test_below_cap_keeps_everything(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{}')
        backup(idx)
        time.sleep(1.1)
        backup(idx)
        listed = list_backups(idx)
        assert len(listed) == 2


class TestRestore:
    def test_restores_content(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{"version": 1, "v": "old"}')
        bkp = backup(idx)
        assert bkp is not None

        # Overwrite the index with something different.
        idx.write_text('{"version": 1, "v": "NEW_BAD"}', encoding="utf-8")
        assert '"NEW_BAD"' in idx.read_text()

        restore(bkp, idx)
        assert '"old"' in idx.read_text()

    def test_restore_leaves_backup_intact(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{"v": "old"}')
        bkp = backup(idx)
        original_bkp_bytes = bkp.read_bytes()

        idx.write_text('{"v": "new"}', encoding="utf-8")
        restore(bkp, idx)
        assert bkp.read_bytes() == original_bkp_bytes

    def test_restore_is_atomic(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{"v": "original"}')
        bkp = idx.with_name("library-index.backup-20260101-000000.json")
        _write(bkp, '{"v": "rollback_me"}')

        # Leave no tmp residue after a successful restore — the
        # atomic-write helper does a tmp+rename cycle.
        restore(bkp, idx)
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".library-index") and p.name.endswith(".tmp")
        ]
        assert leftovers == []

    def test_restore_raises_when_backup_missing(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{}')
        with pytest.raises(FileNotFoundError):
            restore(tmp_path / "does-not-exist.json", idx)


class TestListBackupsEdges:
    def test_missing_parent_returns_empty(self, tmp_path):
        assert list_backups(tmp_path / "nonexistent" / "idx.json") == []

    def test_ignores_non_matching_files(self, tmp_path):
        idx = tmp_path / "library-index.json"
        _write(idx, '{}')
        # Noise files in the same dir.
        _write(tmp_path / "library-index.json.tmp", "x")
        _write(tmp_path / "random.json", "x")
        _write(tmp_path / "library-index.old", "x")
        backup(idx)

        listed = list_backups(idx)
        assert len(listed) == 1
        assert "backup-" in listed[0].name
