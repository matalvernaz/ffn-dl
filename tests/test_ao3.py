"""AO3 scraper — metadata, chapters, series, chapter-count probe."""

import re

from bs4 import BeautifulSoup

from ffn_dl.ao3 import AO3Scraper


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
