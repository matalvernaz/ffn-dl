"""FFN scraper — URL parsing, metadata, search, author URL variants."""

from bs4 import BeautifulSoup

from ffn_dl.scraper import FFNScraper
from ffn_dl.search import _parse_results


class TestURLParsing:
    def test_parses_numeric_id(self):
        assert FFNScraper.parse_story_id("12345") == 12345

    def test_parses_story_url(self):
        assert (
            FFNScraper.parse_story_id("https://www.fanfiction.net/s/12345/1/Title")
            == 12345
        )

    def test_is_author_url_matches_canonical(self):
        assert FFNScraper.is_author_url(
            "https://www.fanfiction.net/u/12345/SomeName"
        )

    def test_is_author_url_matches_vanity(self):
        assert FFNScraper.is_author_url(
            "https://www.fanfiction.net/~plums"
        )

    def test_is_author_url_rejects_story_url(self):
        assert not FFNScraper.is_author_url(
            "https://www.fanfiction.net/s/12345"
        )


class TestMetadataParsing:
    def test_metadata_has_title_author_chapters(self, ffn_story_html):
        soup = BeautifulSoup(ffn_story_html, "lxml")
        meta = FFNScraper._parse_metadata(soup)
        assert meta["title"]
        assert meta["author"] != "Unknown Author"
        assert meta["num_chapters"] >= 1
        # Every chapter dropdown entry must produce a title entry
        assert len(meta["chapter_titles"]) == meta["num_chapters"]


class TestSearchParsing:
    def test_results_extract_expected_shape(self, ffn_search_html):
        results = _parse_results(ffn_search_html)
        assert results, "search fixture should contain results"
        r0 = results[0]
        expected_keys = {
            "title", "author", "url", "summary", "words",
            "chapters", "rating", "fandom", "status",
        }
        assert expected_keys.issubset(r0.keys())
        # Every result should link to an FFN story URL
        for r in results:
            assert r["url"].startswith("https://www.fanfiction.net/s/")
