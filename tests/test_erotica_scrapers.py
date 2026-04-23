"""Smoke tests for the erotica subpackage.

Each scraper gets: URL parsing (happy + error paths), site registration
in ``ffn_dl.sites``, and ``canonical_url`` round-trip. Full end-to-end
download tests would require live HTTP and are deliberately omitted —
these tests run offline in <1s so they gate every commit.
"""

import pytest

from ffn_dl.erotica import (
    AFFScraper,
    ChyoaScraper,
    DarkWandererScraper,
    FictionmaniaScraper,
    GreatFeetScraper,
    LiteroticaScraper,
    LushStoriesScraper,
    MCStoriesScraper,
    NiftyScraper,
    SexStoriesScraper,
    StoriesOnlineScraper,
    TGStorytimeScraper,
)
from ffn_dl.erotica.search import (
    EROTICA_SITE_SLUGS,
    EROTICA_TAG_VOCABULARY,
    ErotiCAResults,
    TAG_SITE_COVERAGE,
    _normalise_sites,
    _normalise_tags,
    _parse_word_threshold,
    search_erotica,
    tag_site_count,
    tag_sites_for,
)
from ffn_dl.sites import EROTICA_SCRAPERS, canonical_url, detect_scraper


# ── Registration ──────────────────────────────────────────────────

def test_all_erotica_scrapers_registered():
    expected = {
        LiteroticaScraper, AFFScraper, StoriesOnlineScraper, NiftyScraper,
        SexStoriesScraper, MCStoriesScraper, LushStoriesScraper,
        FictionmaniaScraper, TGStorytimeScraper, ChyoaScraper,
        DarkWandererScraper, GreatFeetScraper,
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
    ("https://www.tgstorytime.com/viewstory.php?sid=9219", TGStorytimeScraper),
    ("https://chyoa.com/story/Insurance-Salesman-s.14", ChyoaScraper),
    ("https://chyoa.com/chapter/Ooh-that-s-hot.17", ChyoaScraper),
    ("https://darkwanderer.net/threads/foo.12345/", DarkWandererScraper),
    ("https://darkwanderer.net/threads/foo.12345/page-3",
     DarkWandererScraper),
    ("https://www.greatfeet.com/stories/ts1735.htm", GreatFeetScraper),
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
    # TGStorytime: keep sid, drop chapter/ageconsent churn.
    (
        "https://www.tgstorytime.com/viewstory.php?sid=9219&chapter=2&ageconsent=ok",
        "https://www.tgstorytime.com/viewstory.php?sid=9219",
    ),
    # Chyoa: both /story and /chapter collapse to /chapter form.
    (
        "https://chyoa.com/story/Insurance-Salesman-s.14",
        "https://chyoa.com/chapter/Insurance-Salesman-s.14",
    ),
    # Dark Wanderer: strip /page-N from paginated thread URLs.
    (
        "https://darkwanderer.net/threads/foo.12345/page-5",
        "https://darkwanderer.net/threads/foo.12345/",
    ),
    # GreatFeet: story path preserved verbatim.
    (
        "https://www.greatfeet.com/stories/ts1735.htm",
        "https://www.greatfeet.com/stories/ts1735.htm",
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


class TestAFFAuthorLinkFallbacks:
    """AFF rotates its author-link pattern every few years. The
    resolver walks a chain of href shapes down to a structural
    fallback — pin each rung so a future redesign fails loudly
    through one of these tests instead of silently losing the author
    field on every story."""

    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml")

    def test_modern_profile_link(self):
        soup = self._soup(
            '<a href="https://members.adult-fanfiction.org/'
            'profile.php?id=123">WriterX</a>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "WriterX"

    def test_legacy_authorlinks_php(self):
        soup = self._soup(
            '<a href="https://hp.adult-fanfiction.org/'
            'authorlinks.php?no=42">OldSchool</a>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert "OldSchool" in link.get_text()

    def test_structural_fallback_via_story_header_author(self):
        """If AFF drops the old href shapes entirely, a link inside
        ``div.story-header-author`` still has to resolve."""
        soup = self._soup(
            '<div class="story-header-author">'
            '<a href="/some/new/author/url?q=1">FreshWriter</a>'
            '</div>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "FreshWriter"

    def test_structural_fallback_via_generic_author_class(self):
        """Second-tier structural fallback: any container whose class
        mentions ``author``. Catches a redesign that renamed the
        specific header class."""
        soup = self._soup(
            '<div class="byline-author">'
            '<a href="/author/new">NamedWriter</a>'
            '</div>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "NamedWriter"

    def test_returns_none_when_nothing_matches(self):
        soup = self._soup(
            '<p>Just prose, no author markers anywhere.</p>'
        )
        assert AFFScraper._find_author_link(soup) is None

    def test_modern_pattern_preferred_over_legacy(self):
        """When both shapes appear on the same page (crossover period
        between AFF layouts), the modern ``profile.php?id=`` wins so
        the resulting author URL is the one AFF actually serves now."""
        soup = self._soup(
            '<div>'
            '<a href="/authorlinks.php?no=1">LegacyName</a>'
            '<a href="https://members.adult-fanfiction.org/'
            'profile.php?id=999">ModernName</a>'
            '</div>'
        )
        link = AFFScraper._find_author_link(soup)
        assert link is not None
        assert link.get_text(strip=True) == "ModernName"


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


# ── New scraper URL parsing ───────────────────────────────────────

class TestTGStorytimeParsing:
    def test_story_id(self):
        assert (
            TGStorytimeScraper.parse_story_id(
                "https://www.tgstorytime.com/viewstory.php?sid=9219"
            ) == 9219
        )

    def test_bare_id(self):
        assert TGStorytimeScraper.parse_story_id("9219") == 9219

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError):
            TGStorytimeScraper.parse_story_id("https://example.com")


class TestChyoaParsing:
    def test_story_url(self):
        kind, slug, num = ChyoaScraper.parse_story_id(
            "https://chyoa.com/story/Insurance-Salesman-s.14"
        )
        assert (kind, slug, num) == ("story", "Insurance-Salesman-s", 14)

    def test_chapter_url(self):
        kind, slug, num = ChyoaScraper.parse_story_id(
            "https://chyoa.com/chapter/Ooh-that-s-hot.17"
        )
        assert (kind, slug, num) == ("chapter", "Ooh-that-s-hot", 17)


class TestDarkWandererParsing:
    def test_thread_id(self):
        assert (
            DarkWandererScraper.parse_story_id(
                "https://darkwanderer.net/threads/foo.12345/"
            ) == 12345
        )

    def test_bare_id(self):
        assert DarkWandererScraper.parse_story_id("12345") == 12345


class TestGreatFeetParsing:
    def test_story_id(self):
        assert (
            GreatFeetScraper.parse_story_id(
                "https://www.greatfeet.com/stories/ts1735.htm"
            ) == 1735
        )

    def test_bare_id(self):
        assert GreatFeetScraper.parse_story_id("1735") == 1735


# ── UX plumbing ───────────────────────────────────────────────────

def test_erotica_results_carries_stats():
    r = ErotiCAResults()
    r.site_stats = {"mcstories": {"count": 8, "ok": True}}
    r.exhausted_sites = {"mcstories"}
    assert r.site_stats["mcstories"]["count"] == 8
    assert "mcstories" in r.exhausted_sites


def test_tag_site_count_femdom_well_covered():
    # femdom must be on at least 4 sites; otherwise tag-picker
    # annotation misleads the user about coverage.
    assert tag_site_count("femdom") >= 4


def test_tag_site_count_feet_includes_greatfeet():
    # GreatFeet is the dedicated feet archive — the one we regretted
    # missing. Guard against someone removing it from the feet list.
    assert "greatfeet" in tag_sites_for("feet")


def test_tag_site_count_every_vocabulary_tag_has_coverage():
    # No tag should appear in the vocabulary with zero sites — that
    # would be a broken entry telling the user "this tag works" when
    # it doesn't.
    for tag in EROTICA_TAG_VOCABULARY:
        assert tag_site_count(tag) >= 1, f"tag {tag!r} has no sites"


def test_normalise_tags_strips_coverage_annotation():
    # GUI passes "femdom [5 sites]"; scraper needs bare "femdom".
    assert _normalise_tags("femdom [5 sites], feet [5 sites]") == [
        "femdom", "feet",
    ]


def test_tag_coverage_only_references_registered_sites():
    # Every site listed under a tag must exist in the fan-out
    # registry — otherwise tag selection would silently skip a site
    # we claim to cover.
    known_sites = set(EROTICA_SITE_SLUGS) - {"all"}
    for tag, sites in TAG_SITE_COVERAGE.items():
        for site in sites:
            assert site in known_sites, (
                f"tag {tag!r} references unknown site {site!r}"
            )
