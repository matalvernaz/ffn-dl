"""Watchlist integrity check and self-heal."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ffn_dl.watchlist import (
    Watch,
    WatchlistStore,
    WATCH_TYPE_AUTHOR,
    WATCH_TYPE_SEARCH,
    WATCH_TYPE_STORY,
)
from ffn_dl.watchlist_doctor import check_watchlist, heal_watchlist


@pytest.fixture
def store(tmp_path):
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
        created_at="",
        last_error="",
        last_checked_at="",
        cooldown_until="",
    )
    defaults.update(kwargs)
    return Watch(**defaults)


class TestInvalidType:
    def test_detects_unknown_type(self, store):
        store.add(_make_watch(type="not-a-real-type"))
        report = check_watchlist(store)
        assert len(report.invalid_type) == 1
        assert not report.is_clean()

    def test_heal_drops_invalid_type(self, store):
        store.add(_make_watch(type="bogus"))
        store.add(_make_watch())  # valid
        report = check_watchlist(store)
        result = heal_watchlist(store, report, drop_invalid_type=True)
        assert result.removed == 1
        assert len(store.all()) == 1


class TestEmptyTarget:
    def test_detects_empty_story_watch_target(self, store):
        store.add(_make_watch(target=""))
        report = check_watchlist(store)
        assert len(report.empty_target) == 1

    def test_detects_whitespace_only_target(self, store):
        store.add(_make_watch(target="   \t\n"))
        report = check_watchlist(store)
        assert len(report.empty_target) == 1

    def test_heal_drops_empty_target(self, store):
        store.add(_make_watch(target=""))
        report = check_watchlist(store)
        result = heal_watchlist(store, report, drop_empty_target=True)
        assert result.removed == 1
        assert store.all() == []


class TestUnsupportedSite:
    def test_search_watch_on_unsupported_site(self, store):
        store.add(_make_watch(
            type=WATCH_TYPE_SEARCH,
            site="mediaminer",  # not in SEARCH_SUPPORTED_SITES
            target="mm search",
            query="naruto",
            filters={},
        ))
        report = check_watchlist(store)
        assert len(report.unsupported_site) == 1

    def test_supported_search_watch_not_flagged(self, store):
        store.add(_make_watch(
            type=WATCH_TYPE_SEARCH,
            site="ao3",
            target="search blurb",
            query="harry potter",
            filters={},
        ))
        report = check_watchlist(store)
        assert report.unsupported_site == []


class TestUnresolvableUrl:
    def test_random_url_flagged(self, store):
        store.add(_make_watch(
            target="https://example.com/not-a-real-site/1",
        ))
        report = check_watchlist(store)
        assert len(report.unresolvable_url) == 1

    def test_real_ffn_url_not_flagged(self, store):
        store.add(_make_watch(
            target="https://www.fanfiction.net/s/12345",
        ))
        report = check_watchlist(store)
        assert report.unresolvable_url == []

    def test_author_watch_with_real_url_ok(self, store):
        store.add(_make_watch(
            type=WATCH_TYPE_AUTHOR,
            site="ffn",
            target="https://www.fanfiction.net/u/999/x",
        ))
        report = check_watchlist(store)
        assert report.unresolvable_url == []


class TestDuplicates:
    def test_two_story_watches_same_url_are_duplicates(self, store):
        store.add(_make_watch(
            target="https://www.fanfiction.net/s/1",
        ))
        store.add(_make_watch(
            target="https://www.fanfiction.net/s/1",
        ))
        report = check_watchlist(store)
        assert len(report.duplicates) == 1

    def test_whitespace_trimmed_before_dedupe(self, store):
        store.add(_make_watch(
            target="https://www.fanfiction.net/s/1",
        ))
        store.add(_make_watch(
            target="  https://www.fanfiction.net/s/1  ",
        ))
        report = check_watchlist(store)
        assert len(report.duplicates) == 1

    def test_author_and_story_same_url_not_duplicate(self, store):
        """Different watch types against the same URL are legitimate —
        user may want both story-chapter alerts AND author-new-work
        alerts. Don't dedupe across types."""
        store.add(_make_watch(
            type=WATCH_TYPE_STORY,
            target="https://www.fanfiction.net/s/1",
        ))
        store.add(_make_watch(
            type=WATCH_TYPE_AUTHOR,
            target="https://www.fanfiction.net/s/1",
        ))
        report = check_watchlist(store)
        assert report.duplicates == []

    def test_search_dedupe_keys_on_site_query_filters(self, store):
        """Two search watches with identical (site, query, filters)
        collapse even when the display ``target`` differs."""
        store.add(_make_watch(
            type=WATCH_TYPE_SEARCH, site="ao3",
            target="first display", query="q", filters={"rating": "G"},
        ))
        store.add(_make_watch(
            type=WATCH_TYPE_SEARCH, site="ao3",
            target="second display", query="q", filters={"rating": "G"},
        ))
        report = check_watchlist(store)
        assert len(report.duplicates) == 1

    def test_search_with_different_filters_not_duplicate(self, store):
        store.add(_make_watch(
            type=WATCH_TYPE_SEARCH, site="ao3",
            target="x", query="q", filters={"rating": "G"},
        ))
        store.add(_make_watch(
            type=WATCH_TYPE_SEARCH, site="ao3",
            target="x", query="q", filters={"rating": "E"},
        ))
        report = check_watchlist(store)
        assert report.duplicates == []

    def test_heal_drops_duplicates_keeps_first(self, store):
        first = _make_watch(target="https://www.fanfiction.net/s/1")
        second = _make_watch(target="https://www.fanfiction.net/s/1")
        store.add(first)
        store.add(second)

        report = check_watchlist(store)
        heal_watchlist(store, report, drop_duplicates=True)
        assert len(store.all()) == 1
        assert store.all()[0].id == first.id


class TestReportSummary:
    def test_clean_watchlist_summary(self, store):
        store.add(_make_watch())
        summary = check_watchlist(store).summary()
        assert "clean" in summary.lower()

    def test_dirty_summary_lists_categories(self, store):
        store.add(_make_watch(target=""))
        store.add(_make_watch(type="bogus"))
        summary = check_watchlist(store).summary()
        assert "empty target" in summary
        assert "invalid" in summary.lower()

    def test_summary_singular_vs_plural(self, store):
        store.add(_make_watch(target=""))
        s = check_watchlist(store).summary()
        assert "entry" in s or "entries" in s


class TestHealOptIn:
    def test_no_flags_means_no_removal(self, store):
        store.add(_make_watch(target=""))
        store.add(_make_watch(type="bogus"))
        report = check_watchlist(store)
        result = heal_watchlist(store, report)  # all flags False
        assert result.removed == 0
        assert len(store.all()) == 2

    def test_heal_summary_mentions_count(self, store):
        store.add(_make_watch(target=""))
        report = check_watchlist(store)
        result = heal_watchlist(store, report, drop_empty_target=True)
        summary = result.summary()
        assert "1" in summary
