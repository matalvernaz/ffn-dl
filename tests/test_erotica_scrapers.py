"""Smoke tests for the erotica subpackage.

Each scraper gets: URL parsing (happy + error paths), site registration
in ``ffn_dl.sites``, and ``canonical_url`` round-trip. Full end-to-end
download tests would require live HTTP and are deliberately omitted —
these tests run offline in <1s so they gate every commit.
"""

import pytest

from ffn_dl.erotica import (
    AFFScraper,
    FictionmaniaScraper,
    LiteroticaScraper,
    LushStoriesScraper,
    MCStoriesScraper,
    NiftyScraper,
    SexStoriesScraper,
    StoriesOnlineScraper,
)
from ffn_dl.erotica.search import (
    EROTICA_SITE_SLUGS,
    EROTICA_TAG_VOCABULARY,
    _normalise_sites,
    _normalise_tags,
    _parse_word_threshold,
    search_erotica,
)
from ffn_dl.sites import EROTICA_SCRAPERS, canonical_url, detect_scraper


# ── Registration ──────────────────────────────────────────────────

def test_all_erotica_scrapers_registered():
    expected = {
        LiteroticaScraper, AFFScraper, StoriesOnlineScraper, NiftyScraper,
        SexStoriesScraper, MCStoriesScraper, LushStoriesScraper,
        FictionmaniaScraper,
    }
    assert set(EROTICA_SCRAPERS) == expected


@pytest.mark.parametrize("url,expected_cls", [
    ("https://hp.adult-fanfiction.org/story.php?no=600100488", AFFScraper),
    ("https://storiesonline.net/s/40467/slug", StoriesOnlineScraper),
    ("https://www.nifty.org/nifty/gay/college/the-brotherhood/", NiftyScraper),
    ("https://www.sexstories.com/story/114893/slug", SexStoriesScraper),
    ("https://mcstories.com/AToZeb/", MCStoriesScraper),
    ("https://www.lushstories.com/stories/cuckold/a-modern-relationship",
     LushStoriesScraper),
    ("https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345",
     FictionmaniaScraper),
    ("https://www.literotica.com/s/my-story", LiteroticaScraper),
])
def test_detect_scraper_routes_correctly(url, expected_cls):
    assert detect_scraper(url) is expected_cls


# ── URL canonicalisation ──────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # AFF preserves subdomain + ?no=; strips chapter & other params.
    (
        "https://hp.adult-fanfiction.org/story.php?no=600100488&chapter=2",
        "https://hp.adult-fanfiction.org/story.php?no=600100488",
    ),
    # Same id on a different subdomain stays distinct.
    (
        "https://naruto.adult-fanfiction.org/story.php?no=600100488",
        "https://naruto.adult-fanfiction.org/story.php?no=600100488",
    ),
    # SOL: drops slug, keeps numeric id.
    (
        "https://storiesonline.net/s/40467/ouroboros-dorm-dipping",
        "https://storiesonline.net/s/40467",
    ),
    # SexStories: drops slug, keeps numeric id.
    (
        "https://www.sexstories.com/story/114893/slug",
        "https://www.sexstories.com/story/114893",
    ),
    # MCStories: drops index.html / trailing slash variants.
    (
        "https://mcstories.com/AToZeb/index.html",
        "https://mcstories.com/AToZeb/",
    ),
    # Nifty: directory path preserved.
    (
        "https://www.nifty.org/nifty/gay/college/the-brotherhood",
        "https://www.nifty.org/nifty/gay/college/the-brotherhood/",
    ),
    # Lush: category + slug preserved.
    (
        "https://www.lushstories.com/stories/cuckold/a-modern-relationship",
        "https://www.lushstories.com/stories/cuckold/a-modern-relationship",
    ),
    # Fictionmania: reader page + storyID preserved.
    (
        "https://fictionmania.tv/stories/readhtmlstory.html?storyID=74553&junk=1",
        "https://fictionmania.tv/stories/readhtmlstory.html?storyID=74553",
    ),
])
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


# ── Per-scraper URL parsing ───────────────────────────────────────

class TestAFFParsing:
    def test_story_id(self):
        assert (
            AFFScraper.parse_story_id(
                "https://hp.adult-fanfiction.org/story.php?no=600100488"
            ) == 600100488
        )

    def test_bare_id(self):
        assert AFFScraper.parse_story_id("600100488") == 600100488

    def test_subdomain_parsing(self):
        assert (
            AFFScraper.parse_subdomain(
                "https://naruto.adult-fanfiction.org/story.php?no=1"
            ) == "naruto"
        )

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError):
            AFFScraper.parse_story_id("https://example.com/foo")


class TestSOLParsing:
    def test_story_id(self):
        assert (
            StoriesOnlineScraper.parse_story_id(
                "https://storiesonline.net/s/40467/slug"
            ) == 40467
        )

    def test_bare_id(self):
        assert StoriesOnlineScraper.parse_story_id("40467") == 40467

    def test_is_author_url(self):
        assert StoriesOnlineScraper.is_author_url(
            "https://storiesonline.net/a/fan-fiction-man"
        )
        assert not StoriesOnlineScraper.is_author_url(
            "https://storiesonline.net/s/40467"
        )


class TestNiftyParsing:
    def test_story_path(self):
        assert (
            NiftyScraper.parse_story_id(
                "https://www.nifty.org/nifty/gay/college/the-brotherhood/"
            ) == "nifty/gay/college/the-brotherhood"
        )

    def test_rejects_non_nifty(self):
        with pytest.raises(ValueError):
            NiftyScraper.parse_story_id("https://example.com")


class TestSexStoriesParsing:
    def test_story_id(self):
        assert (
            SexStoriesScraper.parse_story_id(
                "https://www.sexstories.com/story/114893/slug"
            ) == 114893
        )


class TestMCStoriesParsing:
    def test_story_slug(self):
        assert (
            MCStoriesScraper.parse_story_id(
                "https://mcstories.com/AToZeb/"
            ) == "AToZeb"
        )

    def test_chapter_url(self):
        assert (
            MCStoriesScraper.parse_story_id(
                "https://mcstories.com/AToZeb/AToZeb.html"
            ) == "AToZeb"
        )

    def test_bare_slug(self):
        assert MCStoriesScraper.parse_story_id("AToZeb") == "AToZeb"


class TestLushStoriesParsing:
    def test_story_tuple(self):
        assert (
            LushStoriesScraper.parse_story_id(
                "https://www.lushstories.com/stories/cuckold/a-modern-relationship"
            ) == ("cuckold", "a-modern-relationship")
        )


class TestFictionmaniaParsing:
    def test_story_id(self):
        assert (
            FictionmaniaScraper.parse_story_id(
                "https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345"
            ) == 12345
        )

    def test_text_url_also_works(self):
        assert (
            FictionmaniaScraper.parse_story_id(
                "https://fictionmania.tv/stories/readtextstory.html?storyID=12345"
            ) == 12345
        )

    def test_bare_id(self):
        assert FictionmaniaScraper.parse_story_id("12345") == 12345


# ── Unified search normalization ──────────────────────────────────

def test_normalise_sites_gui_single():
    assert _normalise_sites(None, "literotica") == ["literotica"]


def test_normalise_sites_all_collapses_to_none():
    assert _normalise_sites(None, "all") is None
    assert _normalise_sites(None, "") is None
    assert _normalise_sites(["all"], "") is None


def test_normalise_sites_list_pass_through():
    assert _normalise_sites(["mcstories", "aff"], "") == ["mcstories", "aff"]


def test_normalise_tags_string_and_list():
    assert _normalise_tags("femdom, feet, mind-control") == [
        "femdom", "feet", "mind-control",
    ]
    assert _normalise_tags(["femdom", "feet"]) == ["femdom", "feet"]
    assert _normalise_tags(None) == []


def test_parse_word_threshold():
    assert _parse_word_threshold("") == 0
    assert _parse_word_threshold("any") == 0  # "any" falls through as 0
    assert _parse_word_threshold("5k+") == 5000
    assert _parse_word_threshold("30k") == 30000
    assert _parse_word_threshold("1000") == 1000


def test_erotica_tag_vocabulary_includes_key_fetishes():
    # Explicit check so a future cleanup doesn't accidentally drop
    # the kinks the unified search was built for.
    for tag in ("femdom", "feet", "spanking", "cuckold", "mind-control"):
        assert tag in EROTICA_TAG_VOCABULARY


def test_erotica_site_slugs_have_labels():
    from ffn_dl.erotica.search import EROTICA_SITE_LABELS
    for slug in EROTICA_SITE_SLUGS:
        assert slug in EROTICA_SITE_LABELS


def test_search_erotica_empty_sites_returns_empty():
    # ``sites=[]`` with no usable entries short-circuits without HTTP.
    assert search_erotica("", sites=["nonexistent"]) == []
