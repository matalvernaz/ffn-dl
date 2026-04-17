"""FicWad scraper — URL parsing, metadata, chapter dropdown."""

from bs4 import BeautifulSoup

from ffn_dl.ficwad import FicWadScraper


class TestURLParsing:
    def test_parses_numeric_id(self):
        assert FicWadScraper.parse_story_id("76962") == 76962

    def test_parses_story_url(self):
        assert (
            FicWadScraper.parse_story_id("https://ficwad.com/story/76962")
            == 76962
        )

    def test_is_author_url_matches(self):
        assert FicWadScraper.is_author_url("https://ficwad.com/a/someone")
        assert not FicWadScraper.is_author_url(
            "https://ficwad.com/story/76962"
        )


class TestMetadataAndChapters:
    def test_metadata_extracts_title(self, ficwad_story_html):
        soup = BeautifulSoup(ficwad_story_html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 76962)
        assert meta["title"] != "Unknown Title"
        assert meta["author"] != "Unknown Author"

    def test_index_page_has_chapter_links(self, ficwad_story_html):
        """FicWad's /story/<id>/1 URL lands on either a chapter-view page
        (dropdown present) or a story index page (#chapters listing).
        Either path should surface discoverable chapters for the scraper."""
        soup = BeautifulSoup(ficwad_story_html, "lxml")
        via_dropdown = FicWadScraper._discover_chapters_from_dropdown(soup)
        index_div = soup.find(id="chapters")
        if via_dropdown:
            for ch in via_dropdown:
                assert isinstance(ch["id"], int)
        else:
            # Index page: #chapters div should list at least one /story/<id>
            import re as _re
            assert index_div is not None
            links = index_div.find_all(
                "a", href=_re.compile(r"/story/\d+")
            )
            assert links, "no chapter links in #chapters div"
