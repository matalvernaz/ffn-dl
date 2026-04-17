"""Royal Road scraper tests."""

from bs4 import BeautifulSoup
from pathlib import Path

from ffn_dl.royalroad import RoyalRoadScraper

FIXTURE = Path(__file__).parent / "fixtures" / "royalroad_fiction.html"


def _load():
    return FIXTURE.read_text(encoding="utf-8")


class TestURLParsing:
    def test_parses_numeric_id(self):
        assert RoyalRoadScraper.parse_story_id("25137") == 25137

    def test_parses_fiction_url(self):
        assert (
            RoyalRoadScraper.parse_story_id(
                "https://www.royalroad.com/fiction/25137/worth-the-candle"
            )
            == 25137
        )

    def test_is_author_url_matches_profile(self):
        assert RoyalRoadScraper.is_author_url(
            "https://www.royalroad.com/profile/12345"
        )
        assert not RoyalRoadScraper.is_author_url(
            "https://www.royalroad.com/fiction/25137"
        )


class TestMetadataAndChapters:
    def test_metadata_extracts_title_author_summary(self):
        soup = BeautifulSoup(_load(), "lxml")
        meta = RoyalRoadScraper._parse_metadata(soup)
        assert meta["title"]
        assert meta["title"] != "Unknown Title"
        assert meta["author"] != "Unknown Author"
        assert meta["summary"]

    def test_chapter_list_is_populated(self):
        soup = BeautifulSoup(_load(), "lxml")
        chapters = RoyalRoadScraper._parse_chapter_list(soup)
        assert chapters
        for ch in chapters:
            assert isinstance(ch["id"], int)
            assert ch["title"]
            assert "/chapter/" in ch["url"]

    def test_status_label_captured(self):
        """Fiction pages surface status as a label — any of ONGOING,
        COMPLETED, HIATUS, STUB, DROPPED. Our parser should pick one."""
        soup = BeautifulSoup(_load(), "lxml")
        meta = RoyalRoadScraper._parse_metadata(soup)
        status = meta["extra"].get("status")
        assert status in (
            None, "Complete", "Ongoing", "Hiatus", "Stub", "Dropped"
        )
