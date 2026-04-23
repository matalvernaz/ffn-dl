"""AO3 scraper — metadata, chapters, series, chapter-count probe."""

import re

import pytest
from bs4 import BeautifulSoup

from ffn_dl.ao3 import AO3LockedError, AO3Scraper


class TestURLParsing:
    def test_parses_numeric_id(self):
        assert AO3Scraper.parse_story_id("41952030") == 41952030

    def test_parses_canonical_url(self):
        assert (
            AO3Scraper.parse_story_id("https://archiveofourown.org/works/41952030")
            == 41952030
        )

    def test_parses_url_with_chapter_suffix(self):
        assert (
            AO3Scraper.parse_story_id(
                "https://archiveofourown.org/works/41952030/chapters/105137868"
            )
            == 41952030
        )

    def test_accepts_ao3_org_mirror(self):
        assert AO3Scraper.parse_story_id("https://ao3.org/works/999") == 999

    def test_is_author_url_matches_users(self):
        assert AO3Scraper.is_author_url(
            "https://archiveofourown.org/users/someone/works"
        )
        assert not AO3Scraper.is_author_url(
            "https://archiveofourown.org/works/41952030"
        )

    def test_is_series_url_matches_series_numeric(self):
        assert AO3Scraper.is_series_url(
            "https://archiveofourown.org/series/1234"
        )
        assert not AO3Scraper.is_series_url(
            "https://archiveofourown.org/works/1234"
        )

    def test_is_bookmarks_url_matches_users_bookmarks(self):
        assert AO3Scraper.is_bookmarks_url(
            "https://archiveofourown.org/users/someone/bookmarks"
        )
        assert AO3Scraper.is_bookmarks_url(
            "https://archiveofourown.org/users/someone/bookmarks?page=2"
        )
        assert not AO3Scraper.is_bookmarks_url(
            "https://archiveofourown.org/users/someone/works"
        )
        assert not AO3Scraper.is_bookmarks_url(
            "https://archiveofourown.org/works/1234"
        )


class TestMetadataParsing:
    def test_parses_work_metadata(self, ao3_work_full_html):
        soup = BeautifulSoup(ao3_work_full_html, "lxml")
        meta = AO3Scraper._parse_metadata(soup)
        assert meta["title"] == "Harry Potter and Harry Potter"
        assert "HarryPotterFanFicArchive_Archivist" in meta["author"]
        extra = meta["extra"]
        assert "Harry Potter" in extra.get("category", "")
        assert extra.get("rating") == "Explicit"
        assert extra.get("language") == "English"
        assert extra.get("words") == "11,053"
        assert extra.get("chapter_ratio") == "4/4"
        assert extra.get("status") == "Complete"

    def test_parses_chapters(self, ao3_work_full_html):
        soup = BeautifulSoup(ao3_work_full_html, "lxml")
        chapters = AO3Scraper._parse_chapters(soup, "Fallback Title")
        assert len(chapters) == 4
        assert all(ch.html for ch in chapters)
        # AO3 inserts h3.landmark sentinels we should strip
        for ch in chapters:
            assert "landmark" not in ch.html.lower()[:200]


class TestChapterCountProbe:
    def test_bare_page_exposes_chapter_count(self, ao3_work_bare_html):
        soup = BeautifulSoup(ao3_work_bare_html, "lxml")
        count = AO3Scraper._parse_chapter_count_from_stats(soup)
        assert count == 4

    def test_full_page_also_exposes_count(self, ao3_work_full_html):
        # The stats block is present on both pages — the cheap probe works
        # as long as AO3 keeps emitting it.
        soup = BeautifulSoup(ao3_work_full_html, "lxml")
        assert AO3Scraper._parse_chapter_count_from_stats(soup) == 4


class TestSeriesParsing:
    def test_series_extracts_name_and_work_links(self, ao3_series_html):
        # Inline-parse the saved page through the scraper's logic. We
        # can't call scrape_series_works (it fetches) but we can
        # reproduce its core — find h4.heading > a[href=/works/<id>].
        soup = BeautifulSoup(ao3_series_html, "lxml")
        h2 = soup.find("h2", class_="heading")
        assert h2 is not None
        assert "Gumballs" in h2.get_text(strip=True)

        seen = set()
        work_urls = []
        for heading in soup.find_all("h4", class_="heading"):
            link = heading.find("a", href=re.compile(r"^/works/\d+"))
            if not link:
                continue
            wid_m = re.search(r"/works/(\d+)", link["href"])
            if wid_m and wid_m.group(1) not in seen:
                seen.add(wid_m.group(1))
                work_urls.append(wid_m.group(1))
        assert len(work_urls) >= 3  # the fixture lists 4 works
        assert all(w.isdigit() for w in work_urls)


class TestSeriesPagination:
    """AO3 series paginate at 20 works/page. Walking ``rel=next`` is the
    only way to collect the tail — a series of 30 works had its last
    10 silently dropped before this was fixed."""

    @staticmethod
    def _page(heading, ids, next_page=None):
        works = "\n".join(
            f'<li class="work"><h4 class="heading">'
            f'<a href="/works/{wid}">Work {wid}</a></h4></li>'
            for wid in ids
        )
        nav = (
            f'<ol class="pagination"><li><a rel="next" '
            f'href="?page={next_page}">Next</a></li></ol>'
            if next_page else ""
        )
        return (
            f'<html><body><h2 class="heading">{heading}</h2>'
            f'<ul class="series work index group">{works}</ul>'
            f'{nav}</body></html>'
        )

    def test_two_page_series_collects_all_works(self):
        scraper = AO3Scraper(use_cache=False)
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if "page=1" in url:
                return self._page(
                    "My Series", list(range(1, 21)), next_page=2,
                )
            return self._page(
                "My Series", list(range(21, 31)), next_page=None,
            )

        scraper._fetch = fake_fetch
        scraper._delay = lambda: None

        name, urls = scraper.scrape_series_works(
            "https://archiveofourown.org/series/999"
        )
        assert name == "My Series"
        # 20 + 10 = 30 works across the two pages
        assert len(urls) == 30
        assert urls[0].endswith("/works/1")
        assert urls[-1].endswith("/works/30")
        # Both pages fetched
        assert any("page=1" in u for u in calls)
        assert any("page=2" in u for u in calls)

    def test_stops_when_no_next_link(self):
        scraper = AO3Scraper(use_cache=False)

        def fake_fetch(url):
            return self._page("S", [1, 2, 3], next_page=None)

        scraper._fetch = fake_fetch
        scraper._delay = lambda: None
        name, urls = scraper.scrape_series_works(
            "https://archiveofourown.org/series/1"
        )
        assert len(urls) == 3

    def test_stops_when_page_adds_nothing_new(self):
        """Defence in depth: if AO3 ever loops ``rel=next`` back to an
        earlier page, we bail instead of paginating forever."""
        scraper = AO3Scraper(use_cache=False)

        def fake_fetch(url):
            # Always returns the same 3 works, always with rel=next.
            return self._page("S", [1, 2, 3], next_page=99)

        scraper._fetch = fake_fetch
        scraper._delay = lambda: None
        name, urls = scraper.scrape_series_works(
            "https://archiveofourown.org/series/1"
        )
        assert len(urls) == 3


class TestAdultGate:
    """The AO3 adult-content gate is bypassed by ``view_adult=true`` in
    the query string. If it isn't (misconfigured fetch, AO3 policy
    change), ``_check_for_blocks`` must raise a clear error rather
    than letting the gate page be parsed as an empty story."""

    def test_gate_page_raises_ao3_locked(self):
        scraper = AO3Scraper(use_cache=False)
        gate_html = (
            "<html><body>"
            "<p>This work could have adult content. If you proceed, "
            "you have agreed that you are willing to see such content.</p>"
            "<a href='?view_adult=true'>Proceed</a>"
            "</body></html>"
        )
        with pytest.raises(AO3LockedError):
            scraper._check_for_blocks(gate_html)

    def test_login_required_page_raises_ao3_locked(self):
        scraper = AO3Scraper(use_cache=False)
        html = (
            "<html><body>"
            "<p>Sorry, you don't have permission to view this work.</p>"
            "</body></html>"
        )
        with pytest.raises(AO3LockedError):
            scraper._check_for_blocks(html)

    def test_real_work_page_does_not_raise(self, ao3_work_full_html):
        scraper = AO3Scraper(use_cache=False)
        # A genuine work page must pass the gate check — regression
        # guard against overeager pattern matching on body text.
        scraper._check_for_blocks(ao3_work_full_html)
