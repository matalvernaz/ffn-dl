"""Literotica scraper tests."""

from pathlib import Path

from bs4 import BeautifulSoup

from ffn_dl.erotica.literotica import LiteroticaScraper, _slug_to_id

FIXTURES = Path(__file__).parent / "fixtures"


def _story_soup():
    return BeautifulSoup(
        (FIXTURES / "literotica_story.html").read_text(encoding="utf-8"),
        "lxml",
    )


def _series_soup():
    return BeautifulSoup(
        (FIXTURES / "literotica_series.html").read_text(encoding="utf-8"),
        "lxml",
    )


class TestURLParsing:
    def test_parses_canonical_url(self):
        assert (
            LiteroticaScraper.parse_story_id(
                "https://www.literotica.com/s/my-story-title"
            )
            == "my-story-title"
        )

    def test_parses_bare_slug(self):
        assert LiteroticaScraper.parse_story_id("my-story-title") == "my-story-title"

    def test_rejects_bad_url(self):
        import pytest
        with pytest.raises(ValueError):
            LiteroticaScraper.parse_story_id("https://example.com/not-literotica")

    def test_is_author_url(self):
        assert LiteroticaScraper.is_author_url(
            "https://www.literotica.com/authors/SomeAuthor"
        )
        assert LiteroticaScraper.is_author_url(
            "https://www.literotica.com/authors/SomeAuthor/works/stories"
        )
        assert not LiteroticaScraper.is_author_url(
            "https://www.literotica.com/s/story-slug"
        )

    def test_is_series_url(self):
        assert LiteroticaScraper.is_series_url(
            "https://www.literotica.com/series/se/12345"
        )
        assert not LiteroticaScraper.is_series_url(
            "https://www.literotica.com/s/story"
        )


class TestSlugHashing:
    def test_stable_across_runs(self):
        assert _slug_to_id("same-slug") == _slug_to_id("same-slug")

    def test_different_slugs_differ(self):
        assert _slug_to_id("slug-one") != _slug_to_id("slug-two")

    def test_returns_int(self):
        assert isinstance(_slug_to_id("my-slug"), int)
        assert _slug_to_id("my-slug") > 0


class TestMetadataAndContent:
    def test_page_count(self):
        soup = _story_soup()
        # Fixture is a 3-page story; pagination links reference 2 and 3
        assert LiteroticaScraper._page_count(soup) == 3

    def test_metadata_extracts_title_and_author(self):
        scraper = LiteroticaScraper(use_cache=False)
        meta = scraper._parse_metadata(_story_soup(), "stop-toying-with-me-miss-yamanaka")
        assert meta["title"] == "Stop Toying With Me, Miss Yamanaka"
        assert meta["author"] == "Duleigh"
        assert meta["num_pages"] == 3

    def test_content_div_is_locatable(self):
        soup = _story_soup()
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        # At least a few hundred chars of text
        assert len(body.get_text(strip=True)) > 200


class TestContentDivFallbacks:
    """Exercise the three-layer selector chain in ``_content_div``.

    Literotica's CSS-module class names rebuild per release; these
    tests pin each structural fallback so a future rebuild that
    invalidates the hash prefix still finds the body through
    ``itemprop`` / ``itemtype`` microdata."""

    def test_itemprop_articlebody_wins_even_without_css_module(self):
        html = (
            '<html><body><main>'
            '<div itemprop="articleBody" class="totally_unrelated">'
            '<p>body</p>'
            '</div></main></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert "body" in body.get_text()

    def test_css_module_prefix_matches_when_itemprop_absent(self):
        html = (
            '<html><body>'
            '<div class="_article__content_FUTURE_HASH_xyz"><p>body</p></div>'
            '</body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert "body" in body.get_text()

    def test_article_itemtype_fallback(self):
        html = (
            '<html><body>'
            '<article itemtype="https://schema.org/Article">'
            '<p>body</p>'
            '</article></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert body.name == "article"

    def test_returns_none_when_no_marker(self):
        html = (
            '<html><body><p>no story markers here</p></body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        assert LiteroticaScraper._content_div(soup) is None

    def test_itemprop_preferred_over_other_matches(self):
        # If both a CSS-module hash and an itemprop element exist, the
        # itemprop wins so we track the stable contract, not the hash.
        html = (
            '<html><body>'
            '<div class="_article__content_STALE_HASH">'
            '<p>stale content</p></div>'
            '<div itemprop="articleBody" class="_article__content_FRESH">'
            '<p>fresh content</p></div>'
            '</body></html>'
        )
        soup = BeautifulSoup(html, "lxml")
        body = LiteroticaScraper._content_div(soup)
        assert body is not None
        assert "fresh" in body.get_text()


class TestSeriesExtraction:
    def test_series_works_parsed_from_fixture(self):
        import re
        soup = _series_soup()
        seen = set()
        count = 0
        for a in soup.find_all("a", href=True):
            m = re.search(r"literotica\.com/s/([a-z0-9-]+)", a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                count += 1
        # Series fixture for /series/se/100 (Ruth) has 3 chapters
        assert count >= 3
