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

    def test_published_before_updated_assigned_by_label(self, ficwad_story_html):
        """Real fixture has Published: 2007-09-10 and Updated: 2019-11-26.
        The parser must use the nearby label, not positional order, so
        a future layout flip wouldn't silently swap the two dates."""
        soup = BeautifulSoup(ficwad_story_html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 76962)
        assert meta["extra"]["date_published"] == 1189427638
        assert meta["extra"]["date_updated"] == 1574769665


class TestDateParsingRobustness:
    """Pin the label-driven date assignment: future layout changes
    (flipped order, missing update, new-story-no-update) must still
    produce the right ``date_published``/``date_updated`` pair."""

    @staticmethod
    def _meta_html(inner):
        return (
            '<div class="storylist"><div class="title"><h4>t</h4>'
            '<span class="author">by <a href="/a/x">x</a></span></div>'
            '<blockquote class="summary"><p>s</p></blockquote>'
            f'<div class="meta">{inner}</div></div>'
        )

    def test_flipped_label_order_still_assigns_correctly(self):
        html = self._meta_html(
            'Updated:&nbsp;<span data-ts="200">2000</span> - '
            'Published:&nbsp;<span data-ts="100">1999</span>'
        )
        soup = BeautifulSoup(html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 1)
        assert meta["extra"]["date_published"] == 100
        assert meta["extra"]["date_updated"] == 200

    def test_single_timestamp_treated_as_publish(self):
        html = self._meta_html(
            'Published:&nbsp;<span data-ts="500">2000</span>'
        )
        soup = BeautifulSoup(html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 1)
        assert meta["extra"]["date_published"] == 500
        assert "date_updated" not in meta["extra"]

    def test_unlabeled_lone_span_still_harvested(self):
        # Defensive: if FicWad ever drops the label entirely, surface
        # the lone timestamp as the publish date rather than losing it.
        html = self._meta_html('<span data-ts="500">2000</span>')
        soup = BeautifulSoup(html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 1)
        assert meta["extra"]["date_published"] == 500

    def test_nonnumeric_data_ts_is_skipped(self):
        html = self._meta_html(
            'Published:&nbsp;<span data-ts="not-a-number">??</span> - '
            'Updated:&nbsp;<span data-ts="200">2000</span>'
        )
        soup = BeautifulSoup(html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 1)
        assert "date_published" not in meta["extra"]
        assert meta["extra"]["date_updated"] == 200


class TestChapterDropdown:
    def test_multi_chapter_dropdown_parsed(self, ficwad_chapter_view_html):
        soup = BeautifulSoup(ficwad_chapter_view_html, "lxml")
        chapters = FicWadScraper._discover_chapters_from_dropdown(soup)
        # 5 options minus "Story Index" = 4 chapters
        assert len(chapters) == 4
        # IDs are integers
        assert all(isinstance(c["id"], int) for c in chapters)
        # Leading "N. " stripped from titles
        assert chapters[0]["title"] == "The time has come"
        assert chapters[3]["title"] == "Of shoes, and ships, and sealing wax."

    def test_story_index_option_excluded(self, ficwad_chapter_view_html):
        soup = BeautifulSoup(ficwad_chapter_view_html, "lxml")
        chapters = FicWadScraper._discover_chapters_from_dropdown(soup)
        assert all("Story Index" not in c["title"] for c in chapters)

    def test_chapter_ids_from_option_values(self, ficwad_chapter_view_html):
        soup = BeautifulSoup(ficwad_chapter_view_html, "lxml")
        chapters = FicWadScraper._discover_chapters_from_dropdown(soup)
        assert [c["id"] for c in chapters] == [77238, 77239, 77240, 77241]

    def test_empty_dropdown_returns_empty_list(self):
        soup = BeautifulSoup(
            '<html><body><p>no select here</p></body></html>', "lxml",
        )
        assert FicWadScraper._discover_chapters_from_dropdown(soup) == []

    def test_multi_chapter_view_gives_metadata_and_chapters(
        self, ficwad_chapter_view_html,
    ):
        """End-to-end: a chapter-view page yields both story metadata
        and a chapter list — the two inputs download() needs."""
        soup = BeautifulSoup(ficwad_chapter_view_html, "lxml")
        meta = FicWadScraper._parse_metadata(soup, 77238)
        chapters = FicWadScraper._discover_chapters_from_dropdown(soup)
        assert meta["title"] == "The time has come"
        assert meta["author"] == "Vanir"
        assert len(chapters) == 4
