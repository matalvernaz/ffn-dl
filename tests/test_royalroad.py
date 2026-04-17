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


class TestAntiPiracyStripping:
    def _make_chapter_page(self, hidden_class: str, extra_style=""):
        """Synthesize a chapter page with a hidden-class paragraph, like
        the one Royal Road injects on each request."""
        return f"""
        <html>
        <head>
          <style>
            .{hidden_class}{{display:none;{extra_style}}}
            .other-rule{{color:red}}
          </style>
        </head>
        <body>
          <div class="chapter-inner chapter-content">
            <p class="some-random-hash-1">Real content one.</p>
            <p class="{hidden_class}">
              If you spot this narrative on Amazon, know that it has
              been stolen. Report the violation.
            </p>
            <p class="some-random-hash-2">Real content two.</p>
          </div>
        </body>
        </html>
        """

    def test_display_none_class_is_stripped(self):
        html = self._make_chapter_page("cnMxYTA0ZTk4NzkyMzQ1YjU5MDdjMTRkN2NjY2M5Mjhj")
        soup = BeautifulSoup(html, "lxml")
        result = RoyalRoadScraper._parse_chapter_html(soup)
        assert "amazon" not in result.lower()
        assert "stolen" not in result.lower()
        assert "real content one" in result.lower()
        assert "real content two" in result.lower()

    def test_visibility_hidden_also_stripped(self):
        """RR sometimes uses other CSS hiding tricks alongside display:none."""
        html = """
        <html>
        <head><style>.hiddenthing{visibility:hidden}</style></head>
        <body><div class="chapter-inner">
            <p>keep me</p>
            <p class="hiddenthing">drop me (anti-piracy)</p>
        </div></body>
        </html>
        """
        soup = BeautifulSoup(html, "lxml")
        result = RoyalRoadScraper._parse_chapter_html(soup)
        assert "keep me" in result.lower()
        assert "drop me" not in result.lower()

    def test_legit_content_with_marker_words_is_kept(self):
        """Legit prose that happens to mention amazon / stolen / etc. must
        survive — we identify injection via CSS, not text markers."""
        html = """
        <html>
        <head></head>
        <body><div class="chapter-inner">
            <p>He had stolen a glance at the Amazon warrior.</p>
        </div></body>
        </html>
        """
        soup = BeautifulSoup(html, "lxml")
        result = RoyalRoadScraper._parse_chapter_html(soup)
        assert "stolen a glance" in result.lower()

    def test_hidden_classes_collector(self):
        html = """
        <style>
          .aaa{color:red}
          .bbb{display:none}
          .ccc{opacity:0}
          .ddd{speak:never}
          .eee{background:blue}
        </style>
        """
        soup = BeautifulSoup(html, "lxml")
        classes = RoyalRoadScraper._hidden_classes(soup)
        assert classes == {"bbb", "ccc", "ddd"}
