"""Tests for the abandoned-WIP detection + skip mechanism.

Covers the three public operations:

* :func:`ffn_dl.library.mark_abandoned` — auto-sweep criteria
  (WIP + stale mtime) and the sticky-marking invariant.
* :func:`ffn_dl.library.revive_abandoned` — single-URL and whole-
  root clears.
* :func:`ffn_dl.library.list_abandoned` — the read-back surface
  the CLI and GUI both render.

Plus the integration with ``build_refresh_queue`` (abandoned
entries drop out of the probe queue) and with the scanner
(``abandoned_after_days`` runs the sweep inline at scan time).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ffn_dl.library import (
    list_abandoned,
    mark_abandoned,
    revive_abandoned,
)
from ffn_dl.library.index import LibraryIndex
from ffn_dl.library.refresh import build_refresh_queue
from ffn_dl.library.scanner import scan

from .library_fixtures import ffndl_epub


def _idx_path(tmp_path: Path) -> Path:
    return tmp_path / "idx.json"


def _age_file(path: Path, days: float) -> None:
    """Backdate both access and modification times so the mtime-
    based abandoned check sees the file as stale. Same pattern as
    the stale-complete gate tests use."""
    current = path.stat().st_mtime
    target = current - days * 86400
    os.utime(path, (target, target))


# ── mark_abandoned ───────────────────────────────────────────────


def test_mark_abandoned_marks_stale_wip(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Stale WIP",
        url="https://www.fanfiction.net/s/1/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)
    scan(lib, index_path=_idx_path(tmp_path))

    idx = LibraryIndex.load(_idx_path(tmp_path))
    report = mark_abandoned(idx, lib, days=365)
    idx.save()

    assert report.newly_marked_count == 1
    assert report.kept_complete == 0
    assert report.kept_fresh == 0
    reloaded = LibraryIndex.load(_idx_path(tmp_path))
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert entry.get("abandoned_at")


def test_mark_abandoned_leaves_complete_alone(tmp_path: Path):
    """Complete fics are the stale-complete feature's domain. A
    completed work is by definition not abandoned — the author
    finished it. Regression guard so a careless threshold doesn't
    mark finished fics as abandoned."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Old Complete",
        url="https://www.fanfiction.net/s/2/1/",
        status="Complete",
    )
    _age_file(path, days=800)
    scan(lib, index_path=_idx_path(tmp_path))

    idx = LibraryIndex.load(_idx_path(tmp_path))
    report = mark_abandoned(idx, lib, days=365)
    assert report.newly_marked_count == 0
    assert report.kept_complete == 1


def test_mark_abandoned_leaves_fresh_wip_alone(tmp_path: Path):
    """A WIP whose file was written recently stays active. The
    threshold is about authors who walked away, not about recent
    downloads."""
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Fresh WIP",
        url="https://www.fanfiction.net/s/3/1/",
        status="In-Progress",
    )
    # No _age_file → mtime is now
    scan(lib, index_path=_idx_path(tmp_path))

    idx = LibraryIndex.load(_idx_path(tmp_path))
    report = mark_abandoned(idx, lib, days=365)
    assert report.newly_marked_count == 0
    assert report.kept_fresh == 1


def test_mark_abandoned_sticky_does_not_rewrite(tmp_path: Path):
    """Re-running the sweep must not bump the ``abandoned_at``
    timestamp on already-marked entries — otherwise every scan
    would lose the original mark date, which is the only signal a
    user has for "when did I declare this dead"."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Sticky",
        url="https://www.fanfiction.net/s/4/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)
    scan(lib, index_path=_idx_path(tmp_path))

    idx = LibraryIndex.load(_idx_path(tmp_path))
    first = mark_abandoned(idx, lib, days=365)
    idx.save()
    first_stamp = [
        e["abandoned_at"] for _u, e in LibraryIndex.load(_idx_path(tmp_path)).stories_in(lib)
    ][0]

    idx2 = LibraryIndex.load(_idx_path(tmp_path))
    second = mark_abandoned(idx2, lib, days=365)
    assert second.newly_marked_count == 0
    assert second.already_marked == 1
    second_stamp = [
        e["abandoned_at"] for _u, e in LibraryIndex.load(_idx_path(tmp_path)).stories_in(lib)
    ][0]
    assert first_stamp == second_stamp


def test_mark_abandoned_rejects_non_positive_days(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    scan(lib, index_path=_idx_path(tmp_path))
    idx = LibraryIndex.load(_idx_path(tmp_path))
    with pytest.raises(ValueError):
        mark_abandoned(idx, lib, days=0)
    with pytest.raises(ValueError):
        mark_abandoned(idx, lib, days=-1)


# ── revive_abandoned ─────────────────────────────────────────────


def test_revive_abandoned_single_url(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Back from dead",
        url="https://www.fanfiction.net/s/10/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)
    scan(lib, index_path=_idx_path(tmp_path))
    idx = LibraryIndex.load(_idx_path(tmp_path))
    mark_abandoned(idx, lib, days=365)
    idx.save()

    idx2 = LibraryIndex.load(_idx_path(tmp_path))
    report = revive_abandoned(idx2, urls=["https://www.fanfiction.net/s/10"])
    assert len(report.revived) == 1
    assert not report.missing
    idx2.save()

    reloaded = LibraryIndex.load(_idx_path(tmp_path))
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert "abandoned_at" not in entry


def test_revive_abandoned_all_in_root(tmp_path: Path):
    """Revive-everything clears every flag in one pass — the "I
    changed my mind, start probing all my WIPs again" escape hatch."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for n in (20, 21, 22):
        p = ffndl_epub(
            lib, title=f"WIP {n}",
            url=f"https://www.fanfiction.net/s/{n}/1/",
            status="In-Progress",
            story_id=n,
        )
        _age_file(p, days=800)
    scan(lib, index_path=_idx_path(tmp_path))

    idx = LibraryIndex.load(_idx_path(tmp_path))
    mark_abandoned(idx, lib, days=365)
    idx.save()

    idx2 = LibraryIndex.load(_idx_path(tmp_path))
    report = revive_abandoned(idx2, urls=None, roots=[lib])
    assert len(report.revived) == 3
    idx2.save()
    reloaded = LibraryIndex.load(_idx_path(tmp_path))
    for _url, entry in reloaded.stories_in(lib):
        assert "abandoned_at" not in entry


def test_revive_abandoned_missing_url_is_reported(tmp_path: Path):
    """A URL the user typoed or one that was already revived comes
    back in ``missing`` so the CLI can tell the user it had no
    effect, rather than silently succeeding."""
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Only story",
        url="https://www.fanfiction.net/s/30/1/",
    )
    scan(lib, index_path=_idx_path(tmp_path))
    idx = LibraryIndex.load(_idx_path(tmp_path))
    report = revive_abandoned(
        idx, urls=["https://www.fanfiction.net/s/99999"],
    )
    assert report.revived == []
    assert "https://www.fanfiction.net/s/99999" in report.missing


# ── list_abandoned ──────────────────────────────────────────────


def test_list_abandoned_sorted_newest_first(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for n in (40, 41):
        p = ffndl_epub(
            lib, title=f"Dead WIP {n}",
            url=f"https://www.fanfiction.net/s/{n}/1/",
            status="In-Progress",
            story_id=n,
        )
        _age_file(p, days=800)
    scan(lib, index_path=_idx_path(tmp_path))

    idx = LibraryIndex.load(_idx_path(tmp_path))
    # Pin two distinct abandoned_at stamps by calling mark_abandoned
    # twice with different epoch times.
    mark_abandoned(idx, lib, days=365, now_epoch=1_700_000_000.0)
    # Force the second mark to be visibly later by rewriting one
    # entry's stamp directly — the helper won't re-stamp an already-
    # marked entry (the sticky invariant).
    reloaded = LibraryIndex.load(_idx_path(tmp_path))
    story_entries = list(reloaded.stories_in(lib))
    story_entries[0][1]["abandoned_at"] = "2020-01-01T00:00:00Z"
    story_entries[1][1]["abandoned_at"] = "2024-01-01T00:00:00Z"
    reloaded.save()

    rows = list_abandoned(LibraryIndex.load(_idx_path(tmp_path)))
    assert len(rows) == 2
    # Newest first
    assert rows[0].abandoned_at.startswith("2024")
    assert rows[1].abandoned_at.startswith("2020")


def test_list_abandoned_empty_library(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    scan(lib, index_path=_idx_path(tmp_path))
    assert list_abandoned(LibraryIndex.load(_idx_path(tmp_path))) == []


# ── integration: build_refresh_queue skips abandoned ───────────


def test_refresh_queue_skips_abandoned_entries(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Dead",
        url="https://www.fanfiction.net/s/50/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)
    scan(lib, index_path=_idx_path(tmp_path))
    idx = LibraryIndex.load(_idx_path(tmp_path))
    mark_abandoned(idx, lib, days=365)
    idx.save()

    messages: list[str] = []
    queue, skipped = build_refresh_queue(
        lib,
        index_path=_idx_path(tmp_path),
        progress=messages.append,
    )
    assert queue == []
    assert len(skipped) == 1
    assert any("abandoned" in m for m in messages)


def test_refresh_queue_honours_skip_abandoned_false(tmp_path: Path):
    """When a caller explicitly opts out (revive-on-the-fly flow,
    say, for a one-time forced recheck), abandoned entries should
    still land in the probe queue."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Forced recheck",
        url="https://www.fanfiction.net/s/51/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)
    scan(lib, index_path=_idx_path(tmp_path))
    idx = LibraryIndex.load(_idx_path(tmp_path))
    mark_abandoned(idx, lib, days=365)
    idx.save()

    queue, skipped = build_refresh_queue(
        lib,
        index_path=_idx_path(tmp_path),
        skip_abandoned=False,
    )
    assert len(queue) == 1


# ── integration: scan auto-marks when abandoned_after_days > 0 ─


def test_scan_auto_marks_when_threshold_given(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Aged WIP",
        url="https://www.fanfiction.net/s/60/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)

    result = scan(
        lib,
        index_path=_idx_path(tmp_path),
        abandoned_after_days=365,
    )
    assert result.newly_abandoned == 1
    reloaded = LibraryIndex.load(_idx_path(tmp_path))
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert entry.get("abandoned_at")


def test_scan_does_not_mark_when_threshold_zero(tmp_path: Path):
    """``abandoned_after_days=0`` explicitly disables the sweep,
    even if the pref would have enabled it. Lets a script pass 0
    to override a user's prefs for one specific scan."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Not swept",
        url="https://www.fanfiction.net/s/61/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)

    result = scan(
        lib,
        index_path=_idx_path(tmp_path),
        abandoned_after_days=0,
    )
    assert result.newly_abandoned == 0


def test_scan_reads_pref_when_no_override(tmp_path: Path, monkeypatch):
    """With ``abandoned_after_days=None`` (default), the sweep
    threshold comes from user prefs. Scanner must call Prefs()
    lazily so headless tests can inject a fake."""
    from ffn_dl.library import scanner as _scanner

    class _FakePrefs:
        def get(self, key, default=None):
            return "365"

    monkeypatch.setattr(
        "ffn_dl.prefs.Prefs", _FakePrefs, raising=False,
    )
    # The resolver imports Prefs lazily; monkeypatch the attribute
    # on the prefs module so the scanner's own import sees the fake.

    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Pref-driven",
        url="https://www.fanfiction.net/s/62/1/",
        status="In-Progress",
    )
    _age_file(path, days=800)

    result = _scanner.scan(lib, index_path=_idx_path(tmp_path))
    assert result.newly_abandoned == 1
