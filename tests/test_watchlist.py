"""Offline tests for the watchlist feature.

These cover the pieces of :mod:`ficary.watchlist` that don't require
real HTTP: the storage layer (load/save/corrupt-recovery) and the
polling runner (diffing + cooldown), using fake scrapers and a fake
notification dispatcher. Per-scraper HTTP paths already have their own
tests; we don't re-test them here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ficary import watchlist
from ficary.notifications import Notification
from ficary.watchlist import (
    NOTIFICATION_COOLDOWN_S,
    SCHEMA_VERSION,
    SEARCH_WATCH_RESULT_CAP,
    WATCH_TYPE_AUTHOR,
    WATCH_TYPE_SEARCH,
    WATCH_TYPE_STORY,
    Watch,
    WatchlistStore,
    classify_target,
    run_once,
    site_key_for_url,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakePrefs:
    """Minimal Prefs stand-in — the runner only calls ``.get(key)``."""

    def __init__(self, values: dict | None = None):
        self._values = values or {}

    def get(self, key, default=None):
        return self._values.get(key, default)


class _FakeScraper:
    """Records the method calls the watchlist runner makes."""

    def __init__(self, *, chapter_count=None, author_works=None):
        self._chapter_count = chapter_count
        self._author_works = author_works or (None, [])

    def get_chapter_count(self, url):
        if self._chapter_count is None:
            raise AssertionError("Unexpected get_chapter_count call")
        return self._chapter_count

    def scrape_author_works(self, url):
        return self._author_works


def _factory(scraper: _FakeScraper):
    """Return a scraper_factory callable that always yields ``scraper``."""
    return lambda url: scraper


class _NotifierSpy:
    """Fake for ``dispatch_notification`` — records every call."""

    def __init__(self):
        self.calls: list[tuple[list[str], Notification, object]] = []

    def __call__(self, channels, notification, prefs):
        self.calls.append((list(channels), notification, prefs))
        return list(channels), []  # delivered, failures


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = WatchlistStore(tmp_path / "watchlist.json")
    original = Watch(type=WATCH_TYPE_STORY, site="ao3", target="https://example/works/1")
    store.add(original)

    reloaded = WatchlistStore(tmp_path / "watchlist.json")
    reloaded.reload()
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0].id == original.id
    assert reloaded.all()[0].target == original.target


def test_store_atomic_write(tmp_path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    store.add(Watch(target="https://example/works/1"))
    # No stray `.tmp` file after a successful save.
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    payload = json.loads(path.read_text())
    assert payload["version"] == SCHEMA_VERSION
    assert len(payload["watches"]) == 1


def test_store_recovers_from_corrupt_file(tmp_path, caplog):
    path = tmp_path / "watchlist.json"
    path.write_text("{ not valid json", encoding="utf-8")

    store = WatchlistStore(path)
    with caplog.at_level("WARNING"):
        store.reload()

    assert store.all() == []
    # Original file is quarantined rather than silently deleted so the
    # user can still recover hand-edited entries after a format slip.
    quarantined = list(tmp_path.glob("watchlist.corrupt-*.json"))
    assert len(quarantined) == 1
    assert any("unreadable" in rec.message for rec in caplog.records)


def test_store_tolerates_unknown_fields(tmp_path):
    """A future schema with extra keys must still load on an older build."""
    path = tmp_path / "watchlist.json"
    payload = {
        "version": SCHEMA_VERSION + 5,
        "watches": [
            {
                "id": "abc",
                "type": WATCH_TYPE_STORY,
                "target": "https://example/works/1",
                "some_future_field": {"nested": 42},
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = WatchlistStore(path)
    store.reload()

    [watch] = store.all()
    assert watch.id == "abc"
    assert watch.target == "https://example/works/1"


def test_store_prefix_get(tmp_path):
    store = WatchlistStore(tmp_path / "watchlist.json")
    w = Watch(id="abcdef1234", target="https://example/1")
    store.add(w)
    assert store.get("abcdef") is w
    assert store.get("zzzz") is None


def test_store_remove_returns_false_on_miss(tmp_path):
    store = WatchlistStore(tmp_path / "watchlist.json")
    assert store.remove("not-a-real-id") is False


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


def test_classify_target_recognises_story_urls():
    assert classify_target("https://archiveofourown.org/works/12345") == WATCH_TYPE_STORY
    assert classify_target("https://www.fanfiction.net/s/999") == WATCH_TYPE_STORY


def test_classify_target_recognises_author_urls():
    # Use an actual author URL shape from AO3 — the sites registry
    # delegates to each scraper's `is_author_url` static method.
    assert classify_target("https://archiveofourown.org/users/someone") == WATCH_TYPE_AUTHOR


def test_classify_target_returns_none_for_unknown():
    assert classify_target("https://example.com/not-a-supported-site") is None


def test_site_key_for_url():
    assert site_key_for_url("https://archiveofourown.org/works/1") == "ao3"
    assert site_key_for_url("https://www.fanfiction.net/s/1") == "ffn"
    assert site_key_for_url("https://royalroad.com/fiction/1") == "royalroad"


# ---------------------------------------------------------------------------
# Story poll
# ---------------------------------------------------------------------------


def test_story_watch_first_poll_sets_baseline_without_alerting(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_STORY,
        site="ao3",
        target="https://example/works/1",
        channels=["pushover"],
    ))
    spy = _NotifierSpy()

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=5)),
        notifier=spy,
    )

    assert len(results) == 1
    assert results[0].ok
    assert results[0].new_items == []
    assert spy.calls == []  # baseline poll does not notify


def test_story_watch_detects_new_chapter_and_notifies(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    existing = Watch(
        type=WATCH_TYPE_STORY,
        site="ao3",
        target="https://example/works/1",
        channels=["pushover"],
        last_seen=5,  # previously-seen count
    )
    store.add(existing)
    spy = _NotifierSpy()

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=spy,
    )

    [result] = results
    assert result.ok
    assert result.chapter_delta == 2
    assert len(spy.calls) == 1
    channels, notification, _ = spy.calls[0]
    assert channels == ["pushover"]
    assert "2 new chapter" in notification.title
    assert notification.url == "https://example/works/1"


def test_story_watch_no_change_does_not_notify(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_STORY,
        target="https://example/works/1",
        channels=["pushover"],
        last_seen=7,
    ))
    spy = _NotifierSpy()

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=spy,
    )

    assert results[0].chapter_delta == 0
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Author poll
# ---------------------------------------------------------------------------


def _author_works(urls):
    return ("Author Name", [
        {"title": f"Story {i}", "url": u} for i, u in enumerate(urls)
    ])


def test_author_watch_first_poll_sets_baseline_without_alerting(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_AUTHOR,
        site="ao3",
        target="https://example/users/foo",
        channels=["discord"],
    ))
    spy = _NotifierSpy()

    fake = _FakeScraper(author_works=_author_works([
        "https://example/works/1", "https://example/works/2",
    ]))
    results = run_once(store, _FakePrefs(), scraper_factory=_factory(fake), notifier=spy)

    assert results[0].new_items == []
    assert spy.calls == []


def test_author_watch_detects_new_work(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_AUTHOR,
        site="ao3",
        target="https://example/users/foo",
        channels=["discord"],
        last_seen=["https://example/works/1"],
    ))
    spy = _NotifierSpy()

    fake = _FakeScraper(author_works=_author_works([
        "https://example/works/1",
        "https://example/works/2",
        "https://example/works/3",
    ]))
    results = run_once(store, _FakePrefs(), scraper_factory=_factory(fake), notifier=spy)

    [result] = results
    assert sorted(result.new_items) == [
        "https://example/works/2",
        "https://example/works/3",
    ]
    assert len(spy.calls) == 1
    _, notification, _ = spy.calls[0]
    assert "2 new works" in notification.title


# ---------------------------------------------------------------------------
# Search poll
# ---------------------------------------------------------------------------


def test_search_watch_caps_tracked_urls(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_SEARCH,
        site="ao3",
        query="fluff",
        channels=["pushover"],
    ))

    def fake_search(query, page=1, **filters):
        # Return more than the cap so we can verify the slice.
        return [
            {"title": f"Hit {i}", "url": f"https://example/works/{i}"}
            for i in range(SEARCH_WATCH_RESULT_CAP + 25)
        ]

    # watchlist._poll_search imports `search` lazily, so we monkeypatch
    # the attribute on the real module after import-time.
    import ficary.search as search_module
    monkeypatch.setattr(search_module, "search_ao3", fake_search, raising=False)

    spy = _NotifierSpy()
    run_once(store, _FakePrefs(), notifier=spy)

    reloaded = WatchlistStore(store.path)
    reloaded.reload()
    [w] = reloaded.all()
    assert len(w.last_seen) == SEARCH_WATCH_RESULT_CAP


def test_search_watch_rejects_unsupported_site(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_SEARCH,
        site="bogus",
        query="anything",
        channels=["pushover"],
    ))

    results = run_once(store, _FakePrefs(), notifier=_NotifierSpy())

    assert results[0].ok is False
    assert "bogus" in results[0].error


# ---------------------------------------------------------------------------
# Cooldown, error handling, disabled watches
# ---------------------------------------------------------------------------


def test_cooldown_suppresses_repeat_notification(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_STORY,
        target="https://example/works/1",
        channels=["pushover"],
        last_seen=5,
    ))
    spy = _NotifierSpy()

    fake_now = {"t": 1_000_000.0}

    # First poll: new chapter → notification fires, cooldown is set.
    run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=6)),
        notifier=spy,
        now=lambda: fake_now["t"],
    )
    assert len(spy.calls) == 1

    # Second poll moments later: another new chapter, but we're still
    # inside the cooldown window, so no notification.
    fake_now["t"] += NOTIFICATION_COOLDOWN_S / 2
    run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=spy,
        now=lambda: fake_now["t"],
    )
    assert len(spy.calls) == 1  # unchanged

    # After the cooldown elapses, the next new chapter notifies again.
    fake_now["t"] += NOTIFICATION_COOLDOWN_S + 1
    run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=8)),
        notifier=spy,
        now=lambda: fake_now["t"],
    )
    assert len(spy.calls) == 2


def test_disabled_watches_are_skipped(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_STORY,
        target="https://example/works/1",
        channels=["pushover"],
        enabled=False,
        last_seen=5,
    ))
    spy = _NotifierSpy()

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=99)),
        notifier=spy,
    )

    assert results == []
    assert spy.calls == []


def test_run_once_watch_ids_filter_polls_only_selected(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    kept = Watch(
        type=WATCH_TYPE_STORY,
        target="https://example/works/1",
        channels=["pushover"],
        last_seen=5,
    )
    skipped = Watch(
        type=WATCH_TYPE_STORY,
        target="https://example/works/2",
        channels=["pushover"],
        last_seen=5,
    )
    store.add(kept)
    store.add(skipped)

    polled_urls: list[str] = []

    class _RecordingScraper:
        def get_chapter_count(self, url):
            polled_urls.append(url)
            return 5

    results = run_once(
        store, _FakePrefs(),
        watch_ids={kept.id},
        scraper_factory=lambda url: _RecordingScraper(),
        notifier=_NotifierSpy(),
    )

    assert [r.watch_id for r in results] == [kept.id]
    assert polled_urls == ["https://example/works/1"]


def test_scraper_exception_is_captured_not_raised(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(Watch(
        type=WATCH_TYPE_STORY,
        target="https://example/works/1",
        channels=["pushover"],
        last_seen=5,
    ))

    class _Exploder:
        def get_chapter_count(self, url):
            raise RuntimeError("site on fire")

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=lambda url: _Exploder(),
        notifier=_NotifierSpy(),
    )

    assert results[0].ok is False
    assert "site on fire" in results[0].error
    # The watch itself records the error so the CLI/GUI can surface it
    # without re-running the poll.
    [persisted] = WatchlistStore(store.path).all() or [None] or [None]  # reload
    # Simpler: re-open and inspect.
    reloaded = WatchlistStore(store.path)
    reloaded.reload()
    [w] = reloaded.all()
    assert "site on fire" in w.last_error


def test_run_once_reloads_store_inside_lock(tmp_path):
    """The autopoll-vs-manual-poll race: two callers each pre-load
    the same on-disk store, then take turns running. Without an
    inside-lock reload, caller A's writes silently overwrite caller
    B's. The fix re-reads from disk after acquiring _RUN_ONCE_LOCK,
    so a watch B added while A was reading still shows up in A's
    iteration.
    """
    path = tmp_path / "w.json"
    # Caller A loads first — sees the empty file.
    store_a = WatchlistStore(path)
    store_a.reload()
    assert store_a.all() == []

    # Caller B sneaks in and writes a new watch to disk.
    store_b = WatchlistStore(path)
    store_b.add(Watch(
        type=WATCH_TYPE_STORY,
        site="ao3",
        target="https://example/works/42",
        channels=[],
    ))

    # Caller A runs poll. It must see B's watch because run_once
    # reloads inside the lock, not act on its stale empty list and
    # save back a watch-less file.
    results = run_once(
        store_a, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=3)),
        notifier=_NotifierSpy(),
    )
    assert len(results) == 1
    # And the disk file still has B's watch — A's save() didn't wipe it.
    reloaded = WatchlistStore(path)
    reloaded.reload()
    assert len(reloaded.all()) == 1
# ---------------------------------------------------------------------------
# Auto-download (round-10 F2)
# ---------------------------------------------------------------------------


class _DownloaderSpy:
    """Fake for the injectable ``downloader`` — records calls, returns
    canned paths, optionally raises."""

    def __init__(self, paths=None, raise_exc=None):
        self.calls = []
        self._paths = paths or []
        self._raise = raise_exc

    def __call__(self, watch, result):
        self.calls.append((watch.id, list(result.new_items)))
        if self._raise is not None:
            raise self._raise
        return list(self._paths)


def _autodl_watch(**kw):
    defaults = dict(
        type=WATCH_TYPE_STORY,
        site="ao3",
        target="https://example/works/1",
        channels=["pushover"],
        auto_download=True,
        last_seen=5,
    )
    defaults.update(kw)
    return Watch(**defaults)


def test_auto_download_fires_and_path_lands_in_message(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_autodl_watch())
    spy = _NotifierSpy()
    dl = _DownloaderSpy(paths=["/lib/Story.epub"])

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=spy, downloader=dl,
    )

    [result] = results
    assert dl.calls and dl.calls[0][0] == result.watch_id
    assert result.downloaded_paths == ["/lib/Story.epub"]
    _, notification, _ = spy.calls[0]
    assert "Saved to: /lib/Story.epub" in notification.message


def test_auto_download_skipped_without_flag(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_autodl_watch(auto_download=False))
    dl = _DownloaderSpy(paths=["/x.epub"])

    run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=_NotifierSpy(), downloader=dl,
    )
    assert dl.calls == []


def test_auto_download_skipped_on_baseline_poll(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_autodl_watch(last_seen=None))  # first poll = baseline
    dl = _DownloaderSpy(paths=["/x.epub"])

    run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=_NotifierSpy(), downloader=dl,
    )
    assert dl.calls == []  # no new_items -> no download


def test_auto_download_failure_isolated_and_surfaced(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    bad = _autodl_watch(id="badwatch1", target="https://example/works/1")
    good = _autodl_watch(id="goodwatch", target="https://example/works/2")
    store.add(bad)
    store.add(good)
    spy = _NotifierSpy()

    calls = []

    def flaky_downloader(watch, result):
        calls.append(watch.id)
        if watch.id == "badwatch1":
            raise RuntimeError("disk full")
        return ["/lib/Good.epub"]

    results = run_once(
        store, _FakePrefs(),
        scraper_factory=_factory(_FakeScraper(chapter_count=7)),
        notifier=spy, downloader=flaky_downloader,
    )

    assert calls == ["badwatch1", "goodwatch"]  # failure didn't block
    by_id = {r.watch_id: r for r in results}
    assert "disk full" in by_id["badwatch1"].download_error
    assert store.get("badwatch1").last_error.startswith("auto-download failed")
    assert by_id["goodwatch"].downloaded_paths == ["/lib/Good.epub"]
    # Both notifications still dispatched; the failed one says so.
    assert len(spy.calls) == 2
    messages = [n.message for _, n, _ in spy.calls]
    assert any("Auto-download failed" in m for m in messages)
    assert any("Saved to: /lib/Good.epub" in m for m in messages)


def test_auto_download_field_roundtrips(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_autodl_watch())
    reloaded = WatchlistStore(tmp_path / "w.json")
    reloaded.reload()
    assert reloaded.all()[0].auto_download is True


def test_make_watch_downloader_routes_library_and_fresh(tmp_path, monkeypatch):
    """cli.make_watch_downloader: a tracked story updates in place at its
    library path/format; an untracked URL downloads fresh. Built on
    DownloadJob.from_prefs — no fake namespaces."""
    from ficary import cli as cli_mod
    from ficary.library.index import LibraryIndex

    idx_path = tmp_path / "index.json"
    idx = LibraryIndex.load(idx_path)
    root = tmp_path / "lib"
    (root / "HP").mkdir(parents=True)
    tracked = "https://www.fanfiction.net/s/111"
    lib = idx.library_state(root)
    lib["stories"] = {tracked: {"relpath": "HP/Tale.html"}}
    (root / "HP" / "Tale.html").write_text("x", encoding="utf-8")
    idx.save()

    calls = []

    def fake_download_one(url, job, output_dir, *, update_path=None,
                          on_export=None, **kw):
        calls.append((url, job.format, str(output_dir), update_path))
        if on_export:
            on_export(Path(output_dir) / "out.epub")
        return True

    monkeypatch.setattr(cli_mod, "_download_one", fake_download_one)
    monkeypatch.setattr(
        cli_mod, "check_format_deps", lambda fmt: None, raising=False)
    import ficary.exporters as exporters
    monkeypatch.setattr(exporters, "check_format_deps", lambda fmt: None)

    class _P:
        def get(self, key):
            return "epub"

        def get_bool(self, key):
            return False

    # Point LibraryIndex.load at our tmp index for the factory's lookup.
    monkeypatch.setattr(
        "ficary.library.index.LibraryIndex.load",
        classmethod(lambda cls, path=None: cls(idx_path, __import__("json").loads(idx_path.read_text()))),
    )

    downloader = cli_mod.make_watch_downloader(_P())

    story_watch = Watch(type=WATCH_TYPE_STORY, target=tracked,
                        auto_download=True)
    from ficary.watchlist import PollResult
    saved = downloader(story_watch, PollResult(watch_id="w", ok=True,
                                               new_items=[tracked]))
    assert len(saved) == 1
    url, fmt, outdir, update_path = calls[0]
    assert url == tracked
    assert fmt == "html"  # derived from the tracked file's suffix
    assert update_path == root / "HP" / "Tale.html"
    assert outdir == str(root / "HP")
