"""FFN scraper — URL parsing, metadata, search, author URL variants."""

from unittest import mock

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


class TestAuthorPageScoping:
    def test_own_stories_excludes_favourites(self):
        """Regression: FFN author pages list own stories in #st_inside
        and favourites in #fs_inside. scrape_author_stories must not
        pick up favourites."""
        html = """
        <html><body>
          <title>SomeAuthor | FanFiction</title>
          <div id="st_inside">
            <a href="/s/111/1/Mine-1">Mine 1</a>
            <a href="/s/222/1/Mine-2">Mine 2</a>
          </div>
          <div id="fs_inside">
            <a href="/s/999/1/Fav-1">Fav 1</a>
            <a href="/s/888/1/Fav-2">Fav 2</a>
          </div>
          <div id="fa"><a href="/u/42">Other Author</a></div>
        </body></html>
        """
        scraper = FFNScraper(use_cache=False)
        with mock.patch.object(scraper, "_fetch", return_value=html):
            name, stories = scraper.scrape_author_stories(
                "https://www.fanfiction.net/u/1"
            )
        ids = [u.rsplit("/", 1)[-1] for u in stories]
        assert ids == ["111", "222"]
        assert "999" not in ids
        assert "888" not in ids

    def test_falls_back_to_full_page_when_container_missing(self):
        """Older or malformed author pages without #st_inside still work
        — we don't want to silently return zero stories."""
        html = """
        <html><body>
          <title>Old Author | FanFiction</title>
          <a href="/s/777/1/Only-Story">Only</a>
        </body></html>
        """
        scraper = FFNScraper(use_cache=False)
        with mock.patch.object(scraper, "_fetch", return_value=html):
            name, stories = scraper.scrape_author_stories(
                "https://www.fanfiction.net/u/2"
            )
        assert len(stories) == 1
        assert stories[0].endswith("/s/777")


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
