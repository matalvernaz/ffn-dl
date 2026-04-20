"""Offline tests for the watchlist feature.

These cover the pieces of :mod:`ffn_dl.watchlist` that don't require
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

from ffn_dl import watchlist
from ffn_dl.notifications import Notification
from ffn_dl.watchlist import (
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
    import ffn_dl.search as search_module
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
