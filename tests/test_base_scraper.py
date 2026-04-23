"""BaseScraper shared helpers.

Most of ``BaseScraper`` is HTTP + retry plumbing that's hard to
unit-test without a fake server, but the logic-only helpers (chapter
materialisation) are worth pinning on their own so changes land with
visible test coverage instead of relying on per-scraper tests that
happen to exercise them indirectly.
"""

from pathlib import Path

import pytest

from ffn_dl.scraper import BaseScraper


class _ProbeScraper(BaseScraper):
    """Minimal concrete scraper for unit-testing ``_materialise_chapters``.

    Doesn't talk to the network: ``_fetch_parallel`` is monkey-patched
    in each test to return pre-canned bodies so we can drive the
    orchestration logic deterministically.
    """

    site_name = "probe"


@pytest.fixture
def scraper(tmp_path):
    # use_cache=True + a tmp dir lets the cache-write path run without
    # polluting the real user cache.
    return _ProbeScraper(use_cache=True, cache_dir=tmp_path)


def _descriptor(n):
    return {"url": f"https://example.invalid/ch/{n}", "title": f"Chapter {n}"}


class TestMaterialiseChapters:
    def test_fetches_all_when_nothing_cached(self, scraper):
        fetched_bodies = [
            "<div id=ct>Body 1</div>",
            "<div id=ct>Body 2</div>",
            "<div id=ct>Body 3</div>",
        ]
        calls = []

        def fake_parallel(urls):
            calls.append(list(urls))
            return fetched_bodies[: len(urls)]

        scraper._fetch_parallel = fake_parallel

        def parse(soup):
            return soup.find(id="ct").decode_contents()

        chapters = scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(1), _descriptor(2), _descriptor(3)],
            skip_chapters=0,
            chapter_spec=None,
            parse_chapter=parse,
            progress_callback=None,
        )
        assert [c.number for c in chapters] == [1, 2, 3]
        assert [c.html for c in chapters] == ["Body 1", "Body 2", "Body 3"]
        # All three urls fetched in a single batch.
        assert len(calls) == 1
        assert len(calls[0]) == 3

    def test_skip_chapters_drops_early_chapters(self, scraper):
        scraper._fetch_parallel = lambda urls: [
            "<div id=ct>body</div>" for _ in urls
        ]
        chapters = scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(i) for i in range(1, 6)],
            skip_chapters=3,
            chapter_spec=None,
            parse_chapter=lambda s: s.find(id="ct").decode_contents(),
            progress_callback=None,
        )
        assert [c.number for c in chapters] == [4, 5]

    def test_chapter_spec_filters(self, scraper):
        scraper._fetch_parallel = lambda urls: [
            "<div id=ct>b</div>" for _ in urls
        ]
        # chapter_spec is a list of (lo, hi) inclusive ranges.
        chapters = scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(i) for i in range(1, 11)],
            skip_chapters=0,
            chapter_spec=[(2, 4), (8, 9)],
            parse_chapter=lambda s: s.find(id="ct").decode_contents(),
            progress_callback=None,
        )
        assert [c.number for c in chapters] == [2, 3, 4, 8, 9]

    def test_cached_chapters_bypass_fetch(self, scraper):
        from ffn_dl.models import Chapter as ModelChapter

        # Pre-warm chapters 2 and 4 in the cache.
        for n in (2, 4):
            scraper._save_chapter_cache(
                1, ModelChapter(number=n, title=f"Chapter {n}", html=f"<p>c{n}</p>"),
            )

        requested = []

        def fake_parallel(urls):
            requested.extend(urls)
            return [f"<div id=ct>fetched {u[-1]}</div>" for u in urls]

        scraper._fetch_parallel = fake_parallel

        chapters = scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(i) for i in range(1, 6)],
            skip_chapters=0,
            chapter_spec=None,
            parse_chapter=lambda s: s.find(id="ct").decode_contents(),
            progress_callback=None,
        )
        # Every chapter present, in order.
        assert [c.number for c in chapters] == [1, 2, 3, 4, 5]
        # Cached chapters 2 and 4 retain their cached HTML; only 1, 3, 5
        # should have been fetched.
        assert len(requested) == 3
        assert chapters[1].html == "<p>c2</p>"
        assert chapters[3].html == "<p>c4</p>"

    def test_empty_plan_skips_fetch_call(self, scraper):
        called = []
        scraper._fetch_parallel = lambda urls: called.append(urls) or []
        chapters = scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(1), _descriptor(2)],
            skip_chapters=10,  # past the end
            chapter_spec=None,
            parse_chapter=lambda s: "",
            progress_callback=None,
        )
        assert chapters == []
        # _fetch_parallel shouldn't even be called when nothing's requested.
        assert called == []

    def test_progress_callback_receives_cache_flag(self, scraper):
        from ffn_dl.models import Chapter as ModelChapter

        scraper._save_chapter_cache(
            1, ModelChapter(number=2, title="Chapter 2", html="<p>c2</p>"),
        )
        scraper._fetch_parallel = lambda urls: [
            "<div id=ct>body</div>" for _ in urls
        ]

        events = []

        def on_progress(num, total, title, from_cache):
            events.append((num, total, title, from_cache))

        scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(1), _descriptor(2), _descriptor(3)],
            skip_chapters=0,
            chapter_spec=None,
            parse_chapter=lambda s: s.find(id="ct").decode_contents(),
            progress_callback=on_progress,
        )
        # One event per chapter, cache flag set only for the pre-cached one.
        assert [e[0] for e in events] == [1, 2, 3]
        assert [e[3] for e in events] == [False, True, False]

    def test_total_defaults_to_chapter_list_length(self, scraper):
        scraper._fetch_parallel = lambda urls: [
            "<div id=ct>b</div>" for _ in urls
        ]
        seen_total = []
        scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(i) for i in range(1, 4)],
            skip_chapters=0,
            chapter_spec=None,
            parse_chapter=lambda s: s.find(id="ct").decode_contents(),
            progress_callback=lambda n, t, *_: seen_total.append(t),
        )
        assert set(seen_total) == {3}

    def test_explicit_total_overrides_default(self, scraper):
        """Update mode passes a larger ``total`` so progress bars show
        the real upstream chapter count even when only a slice is
        actually downloaded."""
        scraper._fetch_parallel = lambda urls: [
            "<div id=ct>b</div>" for _ in urls
        ]
        seen_total = []
        scraper._materialise_chapters(
            story_id=1,
            chapter_list=[_descriptor(i) for i in range(1, 4)],
            skip_chapters=0,
            chapter_spec=None,
            parse_chapter=lambda s: s.find(id="ct").decode_contents(),
            progress_callback=lambda n, t, *_: seen_total.append(t),
            total=99,
        )
        assert set(seen_total) == {99}


class TestAbstractContract:
    """Every optional scrape method defaults to NotImplementedError with
    a message that tells the caller which ``is_*_url`` check to gate on.
    This keeps the CLI/GUI from producing confusing AttributeErrors
    when a user pastes, say, a Wattpad series URL (Wattpad has no
    series concept)."""

    def test_default_is_author_url_is_false(self):
        assert BaseScraper.is_author_url("https://example.invalid/user/x") is False

    def test_default_is_series_url_is_false(self):
        assert BaseScraper.is_series_url("https://example.invalid/series/1") is False

    def test_default_is_bookmarks_url_is_false(self):
        assert BaseScraper.is_bookmarks_url(
            "https://example.invalid/user/x/bookmarks"
        ) is False

    def test_scrape_series_works_raises_clear_message(self):
        s = _ProbeScraper(use_cache=False)
        with pytest.raises(NotImplementedError, match="is_series_url"):
            s.scrape_series_works("https://example.invalid/series/1")

    def test_scrape_bookmark_works_raises_clear_message(self):
        s = _ProbeScraper(use_cache=False)
        with pytest.raises(NotImplementedError, match="is_bookmarks_url"):
            s.scrape_bookmark_works("https://example.invalid/user/x/bookmarks")

    def test_scrape_author_works_raises_clear_message(self):
        s = _ProbeScraper(use_cache=False)
        with pytest.raises(NotImplementedError, match="is_author_url"):
            s.scrape_author_works("https://example.invalid/user/x")

    def test_scrape_author_stories_raises_clear_message(self):
        s = _ProbeScraper(use_cache=False)
        with pytest.raises(NotImplementedError, match="is_author_url"):
            s.scrape_author_stories("https://example.invalid/user/x")

    def test_download_and_parse_story_id_still_not_implemented(self):
        s = _ProbeScraper(use_cache=False)
        with pytest.raises(NotImplementedError):
            s.download("foo")
        with pytest.raises(NotImplementedError):
            BaseScraper.parse_story_id("foo")
        with pytest.raises(NotImplementedError):
            s.get_chapter_count("foo")


class TestConcreteScrapersImplementContract:
    """Spot-check that each concrete scraper honours the
    ``is_*_url → scrape_*`` invariant: if the URL-classifier returns
    True, the matching scrape method must not raise
    NotImplementedError."""

    def test_ao3_declares_all_three(self):
        from ffn_dl.ao3 import AO3Scraper
        # AO3 is the one site with all three optional interfaces.
        assert AO3Scraper.is_author_url(
            "https://archiveofourown.org/users/x"
        )
        assert AO3Scraper.is_series_url(
            "https://archiveofourown.org/series/1"
        )
        assert AO3Scraper.is_bookmarks_url(
            "https://archiveofourown.org/users/x/bookmarks"
        )
        # All three scrape methods are subclass-defined (not inherited
        # from BaseScraper), so they don't raise NotImplementedError
        # on the contract message.
        assert AO3Scraper.scrape_series_works is not BaseScraper.scrape_series_works
        assert (
            AO3Scraper.scrape_bookmark_works
            is not BaseScraper.scrape_bookmark_works
        )
        assert (
            AO3Scraper.scrape_author_works is not BaseScraper.scrape_author_works
        )

    def test_wattpad_has_no_series_but_has_author(self):
        from ffn_dl.wattpad import WattpadScraper
        assert WattpadScraper.is_series_url(
            "https://www.wattpad.com/story/6315313"
        ) is False
        assert WattpadScraper.is_author_url(
            "https://www.wattpad.com/user/someone"
        ) is True
        # Series scraping stays on the base-class stub (raises).
        assert (
            WattpadScraper.scrape_series_works
            is BaseScraper.scrape_series_works
        )
