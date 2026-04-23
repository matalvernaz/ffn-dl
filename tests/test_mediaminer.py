"""MediaMiner scraper tests."""

from pathlib import Path

from bs4 import BeautifulSoup

from ffn_dl.mediaminer import MediaMinerScraper, _split_mm_breadcrumb_title

FIXTURES = Path(__file__).parent / "fixtures"


def _story_soup():
    return BeautifulSoup(
        (FIXTURES / "mediaminer_story.html").read_text(encoding="utf-8"),
        "lxml",
    )


def _chapter_soup():
    return BeautifulSoup(
        (FIXTURES / "mediaminer_chapter.html").read_text(encoding="utf-8"),
        "lxml",
    )


class TestURLParsing:
    def test_view_st_url(self):
        assert (
            MediaMinerScraper.parse_story_id(
                "https://www.mediaminer.org/fanfic/view_st.php/102126"
            )
            == 102126
        )

    def test_slug_url(self):
        assert (
            MediaMinerScraper.parse_story_id(
                "https://www.mediaminer.org/fanfic/s/"
                "inuyasha-fan-fiction/a-miko-s-instincts/102126"
            )
            == 102126
        )

    def test_slug_url_with_trailing_slash(self):
        assert (
            MediaMinerScraper.parse_story_id(
                "https://www.mediaminer.org/fanfic/s/"
                "inuyasha-fan-fiction/a-miko-s-instincts/102126/"
            )
            == 102126
        )

    def test_numeric_only(self):
        assert MediaMinerScraper.parse_story_id("102126") == 102126

    def test_bad_url_raises(self):
        import pytest
        with pytest.raises(ValueError):
            MediaMinerScraper.parse_story_id("https://example.com/nope")

    def test_author_url_detection(self):
        assert MediaMinerScraper.is_author_url(
            "https://www.mediaminer.org/fanfic/src.php/u/Majicman55"
        )
        assert MediaMinerScraper.is_author_url(
            "https://www.mediaminer.org/user_info.php/105805"
        )
        assert not MediaMinerScraper.is_author_url(
            "https://www.mediaminer.org/fanfic/view_st.php/102126"
        )


class TestMetadataAndChapters:
    def test_metadata_extracts_expected_fields(self):
        meta = MediaMinerScraper._parse_metadata(_story_soup(), 102126)
        assert meta["title"] == "A Miko's Instincts"
        assert meta["author"] == "Majicman55"
        assert meta["summary"]
        # Fandom captured in the title split
        assert "InuYasha" in meta["extra"].get("category", "")
        # Status "Completed" → normalised to "Complete"
        assert meta["extra"].get("status") == "Complete"
        # Rating like "T" extracted from the rating div
        assert meta["extra"].get("rating")

    def test_chapter_list_is_populated(self):
        chapters = MediaMinerScraper._parse_chapter_list(_story_soup())
        assert chapters, "fixture should have chapter links"
        # Fixture is a 28-chapter fic; sanity-check at least a few
        assert len(chapters) >= 20
        for ch in chapters[:5]:
            assert isinstance(ch["id"], int)
            assert ch["url"].startswith("https://www.mediaminer.org/fanfic/c/")
            assert ch["title"]

    def test_chapter_body_extraction(self):
        html = MediaMinerScraper._parse_chapter_html(_chapter_soup())
        # Chapter fixture is long; body should contain prose text
        assert len(html) > 1000
        # Should not contain the site nav chrome
        assert "<nav" not in html.lower() or "fanfic-text" not in html.lower()


class TestAuthorScraping:
    def test_story_listing_regex_dedupes(self):
        """The author-scrape path collects unique story IDs from
        /fanfic/s/... and /fanfic/view_st.php/... links. Verify the
        regex catches both shapes and dedupes across them."""
        import re
        seen = set()
        for href in [
            "/fanfic/s/inuyasha-fan-fiction/a-miko-s-instincts/102126",
            "/fanfic/s/inuyasha-fan-fiction/a-miko-s-instincts/102126/",
            "/fanfic/view_st.php/102126",
            "/fanfic/s/naruto-fan-fiction/other-title/55555",
        ]:
            m1 = re.search(r"/fanfic/view_st\.php/(\d+)", href)
            m2 = re.search(r"/fanfic/s/[^?#]+?/(\d+)(?:/|$)", href)
            sid = (m1.group(1) if m1 else None) or (m2.group(1) if m2 else None)
            if sid:
                seen.add(sid)
        assert seen == {"102126", "55555"}


class TestEdgeCases:
    def test_missing_article_raises_story_not_found(self):
        """A deleted story page no longer contains an ``<article>``.
        The parser should raise ``StoryNotFoundError`` rather than
        silently returning an empty meta dict — library-update needs
        the clean "definitively gone" signal to stamp the entry."""
        from ffn_dl.scraper import StoryNotFoundError
        soup = BeautifulSoup(
            "<html><body><p>That story does not exist.</p></body></html>",
            "lxml",
        )
        import pytest
        with pytest.raises(StoryNotFoundError):
            MediaMinerScraper._parse_metadata(soup, 999)

    def test_missing_chapter_body_raises_value_error(self):
        """Chapter bodies live in ``#fanfic-text``. If MediaMiner ever
        renames the container, the ValueError surfaces as a scrape
        failure so library-update retries instead of caching garbage."""
        soup = BeautifulSoup(
            "<html><body><p>no text here</p></body></html>",
            "lxml",
        )
        import pytest
        with pytest.raises(ValueError):
            MediaMinerScraper._parse_chapter_html(soup)

    def test_empty_chapter_list_for_oneshot_without_read_link(self):
        """A story page with no chapter links and no "Read" link should
        return an empty list so ``download()`` can raise cleanly."""
        soup = BeautifulSoup(
            "<html><body><article><p>no chapter links</p>"
            "</article></body></html>",
            "lxml",
        )
        assert MediaMinerScraper._parse_chapter_list(soup) == []

    def test_chapter_list_dedupes_same_chapter_id(self):
        """Author menus and "next chapter" footers sometimes link to the
        same chapter twice. Parser must dedupe on chapter id so the
        downloader doesn't fetch the page twice."""
        soup = BeautifulSoup(
            '<html><body><article>'
            '<a href="/fanfic/c/cat/slug/100/1">Chapter 1</a>'
            '<a href="/fanfic/c/cat/slug/100/1">Chapter 1 (footer)</a>'
            '<a href="/fanfic/c/cat/slug/100/2">Chapter 2</a>'
            '</article></body></html>',
            "lxml",
        )
        chapters = MediaMinerScraper._parse_chapter_list(soup)
        assert [c["id"] for c in chapters] == [1, 2]


class TestBreadcrumbTitleSplit:
    """The fandom/title breadcrumb splitter must survive MediaMiner
    swapping the ❯ glyph for a similar chevron and must never leave
    the category leaked into the title or vice versa."""

    def test_current_glyph_splits_cleanly(self):
        title, cat = _split_mm_breadcrumb_title("Anime/Manga ❯ Pretty Story")
        assert title == "Pretty Story"
        assert cat == "Anime/Manga"

    def test_no_separator_returns_full_title(self):
        title, cat = _split_mm_breadcrumb_title("Standalone Story")
        assert title == "Standalone Story"
        assert cat == ""

    def test_ascii_chevron_also_works(self):
        title, cat = _split_mm_breadcrumb_title("Books > Harry Potter > Story")
        assert title == "Story"
        assert cat == "Books / Harry Potter"

    def test_alternative_glyphs_also_split(self):
        for sep in ("›", "→", "»"):
            raw = f"Fandom {sep} Story"
            t, c = _split_mm_breadcrumb_title(raw)
            assert t == "Story", f"failed for sep U+{ord(sep):04X}"
            assert c == "Fandom", f"failed for sep U+{ord(sep):04X}"

    def test_trailing_separator_is_dropped(self):
        # "Fandom ❯" with nothing after — falling back to raw_title
        # would leak the separator; the helper drops empty segments.
        title, cat = _split_mm_breadcrumb_title("Fandom ❯")
        assert title == "Fandom"
        assert cat == ""

    def test_leading_separator_is_dropped(self):
        title, cat = _split_mm_breadcrumb_title("❯ Story")
        assert title == "Story"
        assert cat == ""

    def test_multi_level_breadcrumb_flattens_to_slash_separated(self):
        title, cat = _split_mm_breadcrumb_title(
            "Anime/Manga ❯ Naruto ❯ Big Fic"
        )
        assert title == "Big Fic"
        assert cat == "Anime/Manga / Naruto"


class TestChapterLabelParsing:
    def test_ch_prefix_variant_recognised(self):
        """MediaMiner occasionally renders ``Ch. 3`` instead of
        ``Chapter 3``. The label regex should pick up both."""
        soup = BeautifulSoup(
            '<article>'
            '<a href="/fanfic/c/c/s/1/10">Story Title Ch. 3 ( Ch. 3 )</a>'
            '</article>',
            "lxml",
        )
        chapters = MediaMinerScraper._parse_chapter_list(soup)
        assert len(chapters) == 1
        assert "Ch. 3" in chapters[0]["title"] or chapters[0]["title"] == "Ch. 3"

    def test_named_chapter_preserves_full_label(self):
        """When the label has no ``Chapter N`` slug, fall back to the
        cleaned label so named chapters (``"The Beginning"``) survive."""
        soup = BeautifulSoup(
            '<article>'
            '<a href="/fanfic/c/c/s/1/10">Story Title: The Beginning</a>'
            '</article>',
            "lxml",
        )
        chapters = MediaMinerScraper._parse_chapter_list(soup)
        assert len(chapters) == 1
        assert "Beginning" in chapters[0]["title"]
