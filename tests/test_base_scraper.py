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


class TestV2414AIMDFixes:
    """Regressions for the multi-AI audit fixes in v2.4.14."""

    def test_bump_delay_up_does_not_cascade_across_workers(self):
        """A single throttle window must not multiply the delay by 2^N
        when N parallel workers each recover. Each worker passes its
        own snapshot; only the first call (whose snapshot still matches
        the shared delay) bumps."""
        scraper = BaseScraper(delay_floor=0.0, delay_start=1.0, use_cache=False)
        # All five workers saw _current_delay = 1.0 at throttle time.
        snapshot = 1.0
        for _ in range(5):
            scraper._bump_delay_up(snapshot=snapshot)
        # Old behaviour: 1 → 2 → 4 → 8 → 16 → 32 (5 doublings).
        # New behaviour: 1 → 2, then snapshot != current → no-op.
        assert scraper._current_delay == 2.0

    def test_bump_delay_up_without_snapshot_still_works(self):
        """Legacy single-fetch callers (no snapshot kwarg) keep the
        unconditional doubling for backwards compatibility."""
        scraper = BaseScraper(delay_floor=0.0, delay_start=1.0, use_cache=False)
        scraper._bump_delay_up()
        assert scraper._current_delay == 2.0
        scraper._bump_delay_up()
        assert scraper._current_delay == 4.0

    def test_chunk_size_counts_urls_not_batches(self):
        """In parallel mode ``_delay(fetches=N)`` must cross the
        chunk_size boundary every N URLs, not every batch."""
        scraper = BaseScraper(
            chunk_size=10, chunk_delay_range=(0.0, 0.0),
            delay_floor=0.0, delay_start=0.0, use_cache=False,
        )
        import time
        slept = []
        # Patch time.sleep to record durations; chunk pauses are the
        # only sleeps that fire from chunk_delay_range (we pinned at 0).
        original = time.sleep
        time.sleep = lambda s: slept.append(s)
        try:
            # 5 batches × 5 fetches each = 25 fetches; chunk_size=10
            # should fire at the boundaries that fall inside the run.
            for _ in range(5):
                scraper._delay(fetches=5)
        finally:
            time.sleep = original
        # 25 fetches with chunk_size=10 → boundaries at 10 and 20.
        # Old behaviour with `fetches=1`: never crosses the boundary
        # because counter only reached 5.
        assert scraper._fetch_count == 25

    def test_rotate_browser_returns_new_session(self):
        """The rotation helper must return its new session so callers
        in ``_fetch`` can rebind their local ``sess`` — previously the
        in-flight retry kept using the flagged fingerprint."""
        scraper = BaseScraper(use_cache=False)
        first = scraper._session()
        new = scraper._rotate_browser()
        assert new is not first
        assert scraper._tls.session is new


class TestV2415CFCookieSeedingOn200:
    """Regression for the convergence-pass fix: the 200-CF-challenge
    branch used to seed cookies into ``sess`` and then immediately
    rebind ``sess`` to a fresh rotated session, discarding the
    seeded cookies."""

    def test_200_cf_branch_returns_after_successful_seed(self, monkeypatch):
        """When ``_maybe_seed_cf_cookies`` returns True the 200-CF
        branch must ``continue`` to the next retry with the seeded
        session — not rotate it away.

        Verified by checking that after a 200-CF response, if seeding
        succeeds, no rotation happens before the next request.
        """
        from ffn_dl.scraper import BaseScraper, CloudflareBlockError

        scraper = BaseScraper(use_cache=False, max_retries=2)
        scraper._delay = lambda *a, **kw: None

        # First response: CF challenge served as 200. Second: real 200.
        responses = [
            type("R", (), {"status_code": 200, "text": "just a moment cloudflare", "headers": {}})(),
            type("R", (), {"status_code": 200, "text": "<html>real</html>", "headers": {}})(),
        ]
        call_log = []

        class FakeSession:
            def __init__(self, label):
                self.label = label
                self.headers = type("H", (), {"update": lambda *a: None})()
                self.cookies = type("C", (), {"jar": []})()
            def get(self, url, timeout=None):
                call_log.append(("get", self.label))
                return responses.pop(0)

        first = FakeSession("seeded")
        scraper._tls.session = first
        scraper.session = first

        def fake_seed(sess, url):
            call_log.append(("seed", sess.label))
            return True  # signal that cookies were applied

        def fake_rotate():
            call_log.append(("rotate",))
            new = FakeSession("rotated")
            scraper._tls.session = new
            return new

        monkeypatch.setattr(scraper, "_maybe_seed_cf_cookies", fake_seed)
        monkeypatch.setattr(scraper, "_rotate_browser", fake_rotate)
        monkeypatch.setattr(scraper, "_check_for_blocks", lambda html: (
            (_ for _ in ()).throw(CloudflareBlockError("cf"))
            if "cloudflare" in html else None
        ))

        result = scraper._fetch("https://example.invalid/x")
        assert result == "<html>real</html>"
        # Critical: the seed succeeded → no rotation should fire on
        # this iteration. Pre-fix the trace was:
        #   get/seed/rotate/get
        # Post-fix it is:
        #   get/seed/get          (no rotate between seed and next get)
        assert ("rotate",) not in call_log, call_log
        assert ("seed", "seeded") in call_log
