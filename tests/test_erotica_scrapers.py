"""Smoke tests for the erotica subpackage.

Each scraper gets: URL parsing (happy + error paths), site registration
in ``ffn_dl.sites``, and ``canonical_url`` round-trip. Full end-to-end
download tests would require live HTTP and are deliberately omitted —
these tests run offline in <1s so they gate every commit.
"""

import pytest

from ffn_dl.erotica import (
    AFFScraper,
    BDSMLibraryScraper,
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
        DarkWandererScraper, GreatFeetScraper, BDSMLibraryScraper,
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
    ("http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
     BDSMLibraryScraper),
    ("http://www.bdsmlibrary.com/stories/chapter.php?storyid=10994&chapterid=31865",
     BDSMLibraryScraper),
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
    # BDSM Library: story id lives in ``?storyid=N``; chapter URLs
    # collapse to the story page so chapter and story variants dedupe.
    # HTTPS cert is expired so the canonical form stays http://.
    (
        "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
        "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
    ),
    (
        "http://www.bdsmlibrary.com/stories/chapter.php?storyid=10994&chapterid=31865",
        "http://www.bdsmlibrary.com/stories/story.php?storyid=10994",
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


# ── Tag-capability filter ────────────────────────────────────────


def test_site_supports_all_tags_matches_coverage_table():
    """The per-site tag-capability check must agree with
    :data:`TAG_SITE_COVERAGE` — the dispatcher and the GUI tag-picker
    annotation both consult this dict, so a disagreement would leak
    noise back into the fan-out."""
    from ffn_dl.erotica.search import _site_supports_all_tags

    # Literotica is one of the well-covered sites; bdsm is in the
    # vocabulary and Literotica covers it.
    assert _site_supports_all_tags("literotica", ["bdsm"]) is True

    # AFF doesn't filter by tag — its search adapter ignores the
    # ``tags`` kwarg. The coverage table reflects that for ``bdsm``.
    assert _site_supports_all_tags("aff", ["bdsm"]) is False

    # Empty tag list is always supported (non-tag search).
    assert _site_supports_all_tags("aff", []) is True

    # Niche tags (not in the vocabulary) match no site — safer
    # default than letting an arbitrary free-text tag bypass the
    # filter and return raw recent/popular rows.
    assert _site_supports_all_tags("literotica", ["nonexistent-tag"]) is False


def test_search_erotica_tag_only_drops_tag_ignoring_sites(monkeypatch):
    """Tag-only fan-out must drop sites that ignore the tag —
    otherwise they fall back to recent/popular browses and flood the
    merged result set with unrelated rows. The 'all sites + bdsm
    returns junk' bug.

    Sites that fold ``tags`` into a full-text search payload
    (:data:`_TAG_TEXT_FOLD_SITES`) ARE invoked even when the tag
    isn't pre-mapped — they handle arbitrary tags sensibly, so
    dropping them would discard real discovery surface. The forbidden
    set still catches sites whose scrapers genuinely ignore the
    ``tags`` kwarg entirely.

    The earlier slug-passthrough escape hatch (Lush) was retired
    when each adapter started routing tags through
    :func:`_translate_tag` — Lush now returns ``[]`` for tags it
    doesn't carry, so the gate no longer needs to wave it through.
    """
    from ffn_dl.erotica import search as erotica_search
    from ffn_dl.erotica.search import _TAG_TEXT_FOLD_SITES

    called: set = set()

    def stub_for(site_slug: str):
        def fn(query, **kwargs):
            called.add(site_slug)
            return []
        return fn

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub_for(site) for site in erotica_search._SITE_FNS},
    )

    search_erotica("", tags=["bdsm"])

    expected_callers = (
        set(TAG_SITE_COVERAGE["bdsm"])
        | _TAG_TEXT_FOLD_SITES
    )
    # Sites whose adapters ignore the ``tags`` kwarg entirely must
    # NOT be called for a tag-only bdsm search — their scrapers
    # would silently fall back to a default browse.
    forbidden = {
        "aff", "fictionmania", "tgstorytime", "chyoa",
        "darkwanderer", "greatfeet",
    }
    assert called == expected_callers
    assert called.isdisjoint(forbidden)


def test_search_erotica_skips_filter_when_site_explicitly_picked(monkeypatch):
    """A user who explicitly picked a single site has opted into
    whatever that site returns — don't second-guess them."""
    from ffn_dl.erotica import search as erotica_search

    called: set = set()

    def aff_stub(query, **kwargs):
        called.add("aff")
        return []

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {**erotica_search._SITE_FNS, "aff": aff_stub},
    )

    # Even though AFF doesn't cover bdsm, picking it explicitly
    # should still fire its search.
    search_erotica("", sites=["aff"], tags=["bdsm"])
    assert "aff" in called


def test_normalise_sites_extracts_slug_from_label():
    """The Site dropdown shows ``Adult-FanFiction.org (aff)`` etc. for
    readability; ``_normalise_sites`` must strip the label and return
    the bare slug so the fan-out can look it up in ``_SITE_FNS``."""
    from ffn_dl.erotica.search import _normalise_sites, _extract_slug

    # Direct slug extraction
    assert _extract_slug("Adult-FanFiction.org (aff)") == "aff"
    assert _extract_slug("Literotica (literotica)") == "literotica"
    assert _extract_slug("literotica") == "literotica"
    assert _extract_slug("All erotica sites") == "all"
    assert _extract_slug("") == ""

    # Through the GUI's sites_choice path
    assert _normalise_sites(None, "Literotica (literotica)") == ["literotica"]
    assert _normalise_sites(None, "All erotica sites") is None  # "all" collapse
    assert _normalise_sites(None, "literotica") == ["literotica"]  # bare slug
    # Through the CLI / scripted ``sites`` list path
    assert _normalise_sites(["aff", "Literotica (literotica)"], "") == [
        "aff", "literotica",
    ]


def test_search_erotica_promotes_known_tag_query(monkeypatch):
    """Round-7 audit (v2.4.33): when the user types a bare word that is
    a known erotica tag (e.g. ``feet``) and didn't pick anything in the
    tag multi-picker, ``search_erotica`` should promote it to a tag
    search. Tag-capable sites then use their native tag URL instead of
    falling back to title filtering, which is dramatically better for
    broad discovery queries."""
    from ffn_dl.erotica import search as erotica_search

    seen_tags: dict = {}

    def stub_for(site_slug: str):
        def fn(query, **kwargs):
            seen_tags[site_slug] = list(kwargs.get("tags") or [])
            return []
        return fn

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub_for(site) for site in erotica_search._SITE_FNS},
    )

    # User typed "feet" as the query; no tags picked. Expect promotion.
    search_erotica("feet")

    # GreatFeet is a tag-handler for feet and should now see the tag.
    assert seen_tags.get("greatfeet") == ["feet"]
    # Other tag-capable sites also see the promoted tag.
    assert seen_tags.get("literotica") == ["feet"]
    # AFF / Fictionmania / DarkWanderer / TGStorytime / Chyoa aren't in
    # TAG_SITE_COVERAGE["feet"] nor in text-fold/passthrough — they
    # were dropped from the fan-out.
    assert "fictionmania" not in seen_tags
    assert "tgstorytime" not in seen_tags


def test_search_erotica_does_not_promote_multiword_query(monkeypatch):
    """Promotion is exact-match only — multi-word queries that contain
    a tag word aren't promoted. Otherwise a query like ``"feet first"``
    would be silently rewritten to the ``feet`` tag, which is far too
    aggressive."""
    from ffn_dl.erotica import search as erotica_search

    seen_tags: dict = {}

    def stub_for(site_slug: str):
        def fn(query, **kwargs):
            seen_tags[site_slug] = list(kwargs.get("tags") or [])
            return []
        return fn

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub_for(site) for site in erotica_search._SITE_FNS},
    )

    search_erotica("feet first")

    # No tag was promoted; the fan-out should run with empty tags.
    for site, tags in seen_tags.items():
        assert tags == [], f"site {site} unexpectedly saw promoted tags {tags}"


def test_search_erotica_total_sites_captures_eligible_cohort(monkeypatch):
    """``ErotiCAResults.total_sites`` should hold the canonical
    eligible-sites set so Load More can use it as a stable denominator
    for its ``all exhausted?`` check. Without this, subsequent pages
    invoke ``search_erotica`` with a shrunken active set (skip_sites
    pruned the exhausted ones) and the GUI's len-based comparison
    spuriously claims end-of-results."""
    from ffn_dl.erotica import search as erotica_search

    def stub(query, **kwargs):
        return []

    monkeypatch.setattr(
        erotica_search, "_SITE_FNS",
        {site: stub for site in erotica_search._SITE_FNS},
    )

    # bdsm covers four pre-mapped sites + sexstories (text-fold) +
    # lushstories (passthrough; already in pre-map). Expect total_sites
    # to include all six even though every stub returns an empty list.
    result = search_erotica("", tags=["bdsm"])
    assert "literotica" in result.total_sites
    assert "sexstories" in result.total_sites
    assert "fictionmania" not in result.total_sites
    # Snapshot must be a SET not a list/None so callers can do set ops.
    assert isinstance(result.total_sites, set)


def test_search_aff_returns_empty_when_no_fandom():
    """Round-7 audit (v2.4.32): AFF used to default ``fandom=\"hp\"``
    silently, leaking Harry Potter results into every empty-fandom
    erotica search. Now an empty fandom yields ``[]`` so the site is
    obviously skipped in the per-site stats panel."""
    from ffn_dl.erotica.search import search_aff
    import ffn_dl.erotica.search as erotica_search

    fetched: list = []
    original_fetch = erotica_search._fetch
    try:
        def remembering_fetch(url):
            fetched.append(url)
            return ""
        erotica_search._fetch = remembering_fetch

        assert search_aff("") == []
        assert search_aff("any query", fandom="") == []
        # No fetch should have been issued — the function bails before
        # touching the network.
        assert fetched == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_nifty_returns_empty_for_unsupported_tag_only():
    """Direct callers (or fan-outs that bypass the dispatcher
    filter) get an explicit ``[]`` for an unsupported tag-only
    query — no fallback to ``/gay/`` directory noise."""
    from ffn_dl.erotica.search import search_nifty

    # Patch the fetch helper inline so this test stays offline.
    import ffn_dl.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: ""  # would-be category page
        assert search_nifty("", tags=["bdsm"]) == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_mcstories_returns_empty_for_unmapped_tag_only():
    """Same defensive contract for MCStories — an unmapped tag with
    no free-text query returns ``[]`` instead of dumping the entire
    Titles index."""
    from ffn_dl.erotica.search import search_mcstories

    import ffn_dl.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: ""
        # "polyamory" isn't in _MCS_TAG_CODES.
        assert search_mcstories("", tags=["polyamory"]) == []
    finally:
        erotica_search._fetch = original_fetch


def test_search_greatfeet_returns_empty_for_non_feet_tag_only():
    """GreatFeet's whole catalogue is the feet tag — a tag-only
    ``bdsm`` lookup must return ``[]`` rather than the homepage
    contents."""
    from ffn_dl.erotica.search import search_greatfeet

    import ffn_dl.erotica.search as erotica_search
    original_fetch = erotica_search._fetch
    try:
        erotica_search._fetch = lambda url: ""
        assert search_greatfeet("", tags=["bdsm"]) == []
    finally:
        erotica_search._fetch = original_fetch


# ── Multi-page erotica fan-out driver ────────────────────────────


def test_fetch_erotica_until_limit_accumulates_across_pages():
    """The erotica fan-out used to fetch one page per click —
    ``PER_SITE_LIMIT * supported_sites`` was the hard ceiling for a
    broad tag query. ``fetch_erotica_until_limit`` walks pages until
    it reaches the requested ``limit`` (or the page budget is hit),
    preserving the ``ErotiCAResults`` wrapper that carries the
    per-site stats panel the GUI binds to.
    """
    from ffn_dl.erotica.search import ErotiCAResults
    from ffn_dl.search import fetch_erotica_until_limit

    call_pages = []

    def fake_search(query, *, page, skip_sites=None, **kwargs):
        call_pages.append(page)
        out = ErotiCAResults()
        # Each page yields a fresh batch of 8 rows from 'literotica'.
        out.extend([
            {
                "title": f"Story p{page} #{i}",
                "url": f"https://www.literotica.com/s/p{page}-n{i}",
                "site": "literotica",
            }
            for i in range(8)
        ])
        out.site_stats = {
            "literotica": {
                "count": 8, "ok": True, "error": None, "exhausted": False,
            },
        }
        out.exhausted_sites = set()
        return out

    results, next_page = fetch_erotica_until_limit(
        fake_search, "feet", limit=25, start_page=1,
    )
    # Need ceil(25 / 8) == 4 pages to reach 25.
    assert call_pages == [1, 2, 3, 4]
    assert len(results) >= 25
    # Wrapper survived — site_stats is on the merged object.
    assert "literotica" in results.site_stats
    assert results.site_stats["literotica"]["count"] == 32  # 8 * 4
    assert next_page == 5


def test_fetch_erotica_until_limit_stops_when_all_sites_exhausted():
    """If every active site flips its ``exhausted`` flag on page 1,
    the driver stops immediately — no point burning more page budget
    re-polling sites that already said they're done."""
    from ffn_dl.erotica.search import ErotiCAResults
    from ffn_dl.search import fetch_erotica_until_limit

    call_pages = []

    def fake_search(query, *, page, skip_sites=None, **kwargs):
        call_pages.append(page)
        out = ErotiCAResults()
        out.extend([
            {
                "title": "lone result",
                "url": f"https://www.literotica.com/s/lone-{page}",
                "site": "literotica",
            },
        ])
        out.site_stats = {
            "literotica": {
                "count": 1, "ok": True, "error": None, "exhausted": True,
            },
        }
        out.exhausted_sites = {"literotica"}
        return out

    results, next_page = fetch_erotica_until_limit(
        fake_search, "feet", limit=25, start_page=1,
    )
    # Only one page — every site exhausted immediately.
    assert call_pages == [1]
    assert len(results) == 1
    assert "literotica" in results.exhausted_sites


def test_fetch_erotica_until_limit_forwards_exhausted_sites_as_skip():
    """A site exhausted on page N gets added to ``skip_sites`` for
    page N+1, so the dispatcher doesn't bother re-polling it."""
    from ffn_dl.erotica.search import ErotiCAResults
    from ffn_dl.search import fetch_erotica_until_limit

    skip_seen_per_call = []

    def fake_search(query, *, page, skip_sites=None, **kwargs):
        skip_seen_per_call.append(set(skip_sites or ()))
        out = ErotiCAResults()
        if page == 1:
            # 'aff' is exhausted after page 1; 'literotica' keeps going.
            out.extend([
                {"title": "aff p1", "url": "https://hp.adult-fanfiction.org/story.php?no=1", "site": "aff"},
                {"title": "lit p1", "url": "https://www.literotica.com/s/p1", "site": "literotica"},
            ])
            out.site_stats = {
                "aff": {
                    "count": 1, "ok": True, "error": None, "exhausted": True,
                },
                "literotica": {
                    "count": 1, "ok": True, "error": None, "exhausted": False,
                },
            }
            out.exhausted_sites = {"aff"}
        else:
            # Page 2 should only be polling 'literotica' now.
            assert "aff" in (skip_sites or set())
            out.extend([
                {"title": "lit p2", "url": "https://www.literotica.com/s/p2", "site": "literotica"},
            ])
            out.site_stats = {
                "literotica": {
                    "count": 1, "ok": True, "error": None, "exhausted": True,
                },
            }
            out.exhausted_sites = {"literotica"}
        return out

    fetch_erotica_until_limit(fake_search, "feet", limit=25, start_page=1)
    # Page 1 didn't have anything pre-skipped; page 2 has 'aff' in skip.
    assert skip_seen_per_call[0] == set()
    assert "aff" in skip_seen_per_call[1]


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


class TestBDSMLibraryParsing:
    """BDSM Library URL → story id parsing.

    The site speaks plain HTTP only (HTTPS cert is expired) so the
    parser accepts both schemes and stores nothing scheme-specific.
    Story ids come from ``?storyid=<N>`` on both ``story.php`` and
    ``chapter.php``.
    """

    def test_story_url(self):
        assert (
            BDSMLibraryScraper.parse_story_id(
                "http://www.bdsmlibrary.com/stories/story.php?storyid=10994"
            ) == 10994
        )

    def test_chapter_url(self):
        assert (
            BDSMLibraryScraper.parse_story_id(
                "http://www.bdsmlibrary.com/stories/chapter.php"
                "?storyid=10994&chapterid=31865"
            ) == 10994
        )

    def test_bare_id(self):
        assert BDSMLibraryScraper.parse_story_id("10994") == 10994

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError):
            BDSMLibraryScraper.parse_story_id("https://example.com/foo")

    def test_chapter_links_parsed_in_order(self):
        from bs4 import BeautifulSoup
        # Minimal story.php listing — three chapter anchors. The
        # parser keys off the ``chapter.php?storyid=N&chapterid=M``
        # href shape and returns (1-based-index, chapterid, title)
        # tuples in document order.
        html = """
        <html><body>
          <a href="/stories/chapter.php?storyid=42&chapterid=100">Part 1</a>
          <a href="/stories/chapter.php?storyid=42&chapterid=101">Part 2</a>
          <a href="/stories/chapter.php?storyid=42&chapterid=102">Part 3</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        chap_list = BDSMLibraryScraper._chapter_links(soup, 42)
        assert chap_list == [
            (1, 100, "Part 1"),
            (2, 101, "Part 2"),
            (3, 102, "Part 3"),
        ]


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


# ── Per-site tag translation layer ────────────────────────────────


def test_refining_tags_for_matts_interests_are_in_vocabulary():
    """The 2.4.43 expansion added narrower discovery axes under each
    of Matt's three core interests. Pin them so a future vocab churn
    can't quietly drop one — the whole point of having
    ``foot-worship`` separate from ``feet`` is to let users
    discriminate between them.
    """
    foot_refinements = {"foot-worship", "footjob", "trampling"}
    femdom_refinements = {
        "pegging", "tease-and-denial", "cfnm", "strap-on",
        "female-led", "body-worship",
    }
    cunnilingus_refinements = {"face-sitting", "queening"}
    for tag in foot_refinements | femdom_refinements | cunnilingus_refinements:
        assert tag in EROTICA_TAG_VOCABULARY, f"missing refining tag {tag!r}"
        # Each must also have at least two sites covering it —
        # one-site coverage doesn't justify a vocab slot.
        assert tag_site_count(tag) >= 2, (
            f"{tag!r} only carries {tag_site_count(tag)} sites — likely a "
            "wiring bug; refining tags should hit multiple archives"
        )


def test_wattpad_in_erotica_fan_out():
    """Wattpad must be a fan-out site after 2.4.43, with a real
    adapter wired in. Verifying via the registry rather than calling
    live so the test stays offline."""
    from ffn_dl.erotica.search import _SITE_FNS, EROTICA_SITE_LABELS
    assert "wattpad" in _SITE_FNS, "wattpad not registered in fan-out"
    assert callable(_SITE_FNS["wattpad"])
    assert EROTICA_SITE_LABELS.get("wattpad") == "Wattpad"


def test_wattpad_adapter_parses_json_ld_listitems(monkeypatch):
    """The Wattpad tag-page parser keys off JSON-LD ``ListItem``
    blocks embedded in the page (more durable than the rotating
    Tailwind class names Wattpad uses for the rendered cards).
    Verify a representative ListItem shape extracts cleanly without
    going to the network.
    """
    from ffn_dl.erotica import search as erotica_search
    from ffn_dl.erotica.search import search_wattpad_erotica

    fake_html = """
    <html><body>
    <script type="application/ld+json">{"@context":"http://schema.org",
    "@type":"ListItem","name":"Test Story",
    "description":"A test story summary.",
    "url":"https://www.wattpad.com/story/123456-test-story","position":1}
    </script>
    <script>{"@context":"http://schema.org","@type":"ListItem",
    "name":"Another Story","description":"second summary",
    "url":"https://www.wattpad.com/story/789012-another-story","position":2}
    </script>
    </body></html>
    """

    def fake_fetch(url):
        return fake_html
    monkeypatch.setattr(erotica_search, "_fetch", fake_fetch)
    results = search_wattpad_erotica("", tags=["femdom"])
    assert len(results) == 2
    assert results[0]["title"] == "Test Story"
    assert results[0]["url"].endswith("/story/123456-test-story")
    assert results[0]["site"] == "wattpad"


def test_translate_tag_resolves_matts_interests():
    """The three interests that drove the 2.4.42 search rework
    (foot fetish, femdom, cunnilingus) must translate to a real
    site-specific slug for every site that's claimed to cover them
    in :data:`TAG_SITE_COVERAGE`.

    Earlier revisions passed the unified vocab slug through
    verbatim, which silently 404-ed into stub / all-tags-index pages
    for every site whose actual slug shape differed (SOL needs
    ``foot-fetish``, Lush needs ``fetish``, AO3 needs Title-Case
    ``Foot Fetish``, etc.). Pin a representative translation so any
    future regression hits a test instead of a quiet zero-result
    fan-out.
    """
    from ffn_dl.erotica.search import _translate_tag

    # Feet: literotica permissive, SOL has its own slug, Lush has
    # only the umbrella ``fetish`` category, AO3 uses title-case.
    assert _translate_tag("literotica", "feet") == "feet"
    assert _translate_tag("storiesonline", "feet") == "foot-fetish"
    assert _translate_tag("lushstories", "feet") == "fetish"
    assert _translate_tag("ao3", "feet") == "Foot Fetish"
    assert _translate_tag("bdsmlibrary", "feet") == "41"

    # Femdom: SOL uses ``femaledom``, BDSM Library uses the F/m
    # code id (13). Literotica/Lush/AO3 use the canonical name.
    assert _translate_tag("storiesonline", "femdom") == "femaledom"
    assert _translate_tag("lushstories", "femdom") == "femdom"
    assert _translate_tag("ao3", "femdom") == "Femdom"
    assert _translate_tag("bdsmlibrary", "femdom") == "13"
    assert _translate_tag("mcstories", "femdom") == "fd"

    # Cunnilingus: Literotica + AO3 have a specific tag; SOL +
    # Lush fold it under oral-sex.
    assert _translate_tag("literotica", "cunnilingus") == "cunnilingus"
    assert _translate_tag("ao3", "cunnilingus") == "Cunnilingus"
    assert _translate_tag("storiesonline", "cunnilingus") == "oral-sex"
    assert _translate_tag("lushstories", "cunnilingus") == "oral-sex"


def test_translate_tag_returns_none_for_untranslatable():
    """Sites with no representation for a tag must return ``None`` —
    not a vocab passthrough that would 404 into a stub page."""
    from ffn_dl.erotica.search import _translate_tag

    # MCStories has only 26 codes; cunnilingus / face-sitting / etc.
    # aren't among them.
    assert _translate_tag("mcstories", "cunnilingus") is None
    assert _translate_tag("mcstories", "face-sitting") is None
    # BDSM Library has no humiliation code.
    assert _translate_tag("bdsmlibrary", "humiliation") is None
    # Lush has no harem category.
    # (Confirmed: not in the Lush /stories/<slug> live category list.)
    assert _translate_tag("lushstories", "futanari") is None


def test_mcstories_tag_codes_no_longer_misroute():
    """The 2.4.42 fix corrected seven wrong MCStories code mappings.
    Pin the post-fix correctness so a future edit can't silently
    reintroduce the ``cb=cheating`` / ``ft=feet`` / ``gr=group-sex``
    / ``hm=hypnosis`` / ``hu=humiliation`` / ``la=interracial`` /
    ``ma=transgender`` confusion that sent users to unrelated tag
    pages."""
    from ffn_dl.erotica.search import _MCS_TAG_CODES

    # Tags that USED to map but had no real MCStories representation
    # must now be absent. Adding them back without verifying the code
    # would mean silently sending users to e.g. comic-book stories
    # when they ask for cheating.
    forbidden_keys = {
        "cheating", "feet", "group-sex", "orgy",
        "interracial", "transgender", "futanari",
    }
    for key in forbidden_keys:
        assert key not in _MCS_TAG_CODES, (
            f"{key!r} has no real MCStories tag — last time we mapped "
            "it we landed users on a wholly unrelated tag page."
        )
    # Tags that DO have a real MCStories code must still resolve.
    assert _MCS_TAG_CODES["femdom"] == "fd"
    assert _MCS_TAG_CODES["humiliation"] == "hm"
    assert _MCS_TAG_CODES["mind-control"] == "mc"
