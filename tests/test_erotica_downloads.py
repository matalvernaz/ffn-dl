"""End-to-end download tests for the erotica subpackage.

Every scraper's :meth:`download` path runs against a real HTML page
we've captured as a fixture — no network, no mocks of parsers. We
only stub :meth:`BaseScraper._fetch` so each ``GET`` returns the
right fixture for the requested URL, and then assert that the
produced :class:`Story` object has a sensible title, author,
metadata, and non-empty chapter bodies.

Why this shape: the sanity check during development revealed two
parse bugs (AFF's author link pattern changed; GreatFeet's title
carried a boilerplate suffix) that the URL-parsing tests wouldn't
have caught. Running the full download loop against a fixture is
what gives us coverage of ``_parse_metadata``, ``_parse_chapter_html``,
and the cache/delay control-flow all at once.
"""

from pathlib import Path

import pytest

from ffn_dl.erotica import (
    AFFScraper,
    ChyoaScraper,
    DarkWandererScraper,
    FictionmaniaScraper,
    GreatFeetScraper,
    LushStoriesScraper,
    MCStoriesScraper,
    NiftyScraper,
    SexStoriesScraper,
    StoriesOnlineScraper,
    TGStorytimeScraper,
)

FIXTURES = Path(__file__).parent / "fixtures" / "erotica"


def _load(name: str) -> str:
    """Read a fixture as text. ``errors='replace'`` tolerates the
    odd mis-encoded byte from 1997-era greatfeet.com HTML without
    blowing up the test run."""
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def _make_fetcher(pages: dict):
    """Build a ``_fetch``-shaped callable that serves ``pages`` by
    substring match on the URL. Unmatched URLs raise so a scraper
    silently requesting extra pages trips the test instead of
    hitting the live site."""
    def fetch(url, session=None):
        for needle, body in pages.items():
            if needle in url:
                return body
        raise AssertionError(f"unexpected fetch: {url}")
    return fetch


def _scraper(cls):
    """Build a scraper with cache + delay disabled so tests stay fast
    and don't leave files in the user's cache dir."""
    return cls(use_cache=False, delay_floor=0.0)


# ── Per-scraper download smoke tests ──────────────────────────────

class TestAFFDownload:
    def test_multi_chapter_story(self, monkeypatch):
        ch1 = _load("aff_story_ch1.html")
        ch2 = _load("aff_story_ch2.html")
        scraper = _scraper(AFFScraper)
        monkeypatch.setattr(
            scraper, "_fetch",
            _make_fetcher({"chapter=2": ch2, "story.php": ch1}),
        )
        story = scraper.download(
            "https://hp.adult-fanfiction.org/story.php?no=600100488",
            chapters=[(1, 2)],  # only ask for 1-2 so we don't need ch3/4 fixtures
        )
        assert story.title == "Reflections."
        assert story.author == "Wilde_Guess"
        assert story.author_url.startswith("https://members.adult-fanfiction.org")
        assert len(story.chapters) == 2
        assert story.chapters[0].number == 1
        assert story.chapters[0].html  # non-empty prose
        assert "The Journeys Begin" in story.metadata.get("fandom", "") + story.chapters[0].title


class TestSOLDownload:
    def test_single_chapter_story_with_tags(self, monkeypatch):
        html = _load("sol_story.html")
        scraper = _scraper(StoriesOnlineScraper)
        monkeypatch.setattr(
            scraper, "_fetch", _make_fetcher({"": html}),
        )
        story = scraper.download(
            "https://storiesonline.net/s/40467/ouroboros-dorm-dipping"
        )
        assert story.title == "Ouroboros: Dorm Dipping"
        assert story.author == "Fan Fiction Man"
        tags = story.metadata.get("tags", [])
        assert "Mind Control" in tags
        assert "Incest" in tags
        assert len(story.chapters) == 1
        assert len(story.chapters[0].html) > 1000  # real prose


class TestNiftyDownload:
    def test_directory_with_plain_text_chapters(self, monkeypatch):
        idx = _load("nifty_index.html")
        ch = _load("nifty_chapter.html")
        scraper = _scraper(NiftyScraper)
        def fetch(url, session=None):
            return idx if url.endswith("/") else ch
        monkeypatch.setattr(scraper, "_fetch", fetch)
        story = scraper.download(
            "https://www.nifty.org/nifty/gay/college/the-brotherhood/",
            chapters=[(1, 2)],  # limit to avoid fetching 66 mocked chapters
        )
        assert story.title  # index page doesn't carry a rich title,
        # just assert we got something and didn't error
        assert len(story.chapters) >= 1
        # Plain-text chapters get wrapped in <pre>
        assert story.chapters[0].html.startswith("<pre>")
        # Usenet-style header block was stripped — first line should be prose.
        body_head = story.chapters[0].html[:200]
        assert "From:" not in body_head  # header gone
        assert "Subject:" not in body_head


class TestSexStoriesDownload:
    def test_single_page_story(self, monkeypatch):
        html = _load("sexstories_story.html")
        scraper = _scraper(SexStoriesScraper)
        monkeypatch.setattr(scraper, "_fetch", _make_fetcher({"": html}))
        story = scraper.download(
            "https://www.sexstories.com/story/114893/first_time_masturbating_with_my_little_sister"
        )
        assert "masturbating" in story.title.lower()
        assert story.author == "Julius Incestus"
        assert story.metadata.get("tags"), "tags should be non-empty"
        assert len(story.chapters) == 1


class TestMCStoriesDownload:
    def test_index_plus_chapter(self, monkeypatch):
        idx = _load("mcstories_index.html")
        ch = _load("mcstories_chapter.html")
        scraper = _scraper(MCStoriesScraper)
        def fetch(url, session=None):
            return ch if url.endswith(".html") and "index.html" not in url else idx
        monkeypatch.setattr(scraper, "_fetch", fetch)
        story = scraper.download("https://mcstories.com/AToZeb/")
        assert story.title == "A to Zeb"
        assert story.author == "mightysopor"
        # Story codes should be captured as tags.
        assert story.metadata.get("tags") == ["mc", "mf", "fd"]
        assert len(story.chapters) == 1


class TestLushDownload:
    def test_single_chapter_cuckold_story(self, monkeypatch):
        html = _load("lush_story.html")
        scraper = _scraper(LushStoriesScraper)
        monkeypatch.setattr(scraper, "_fetch", _make_fetcher({"": html}))
        story = scraper.download(
            "https://www.lushstories.com/stories/cuckold/a-modern-relationship"
        )
        assert story.title == "A Modern Relationship"
        assert story.author == "GreyMatter"
        assert story.metadata.get("category") == "cuckold"
        # Lush splits long stories into multiple .story-body divs
        # around inline ads; the body should be the concatenated result.
        body = story.chapters[0].html
        assert len(body) > 2000


class TestTGStorytimeDownload:
    def test_multi_chapter_navigation(self, monkeypatch):
        idx = _load("tgst_story.html")
        ch = _load("tgst_chapter.html")
        scraper = _scraper(TGStorytimeScraper)
        def fetch(url, session=None):
            return ch if "chapter=" in url else idx
        monkeypatch.setattr(scraper, "_fetch", fetch)
        story = scraper.download(
            "https://www.tgstorytime.com/viewstory.php?sid=9219",
            chapters=[(1, 2)],  # limit fixture usage
        )
        assert story.title == "Rescue Below the Moons of Eden"
        assert story.author == "Elusive Faith"
        assert len(story.chapters) == 2


class TestChyoaDownload:
    def test_single_chapter(self, monkeypatch):
        html = _load("chyoa_chapter.html")
        scraper = _scraper(ChyoaScraper)
        monkeypatch.setattr(scraper, "_fetch", _make_fetcher({"": html}))
        story = scraper.download(
            "https://chyoa.com/chapter/Ooh-that-s-hot.17"
        )
        assert "Ooh" in story.title  # curly quotes survive the round-trip
        assert len(story.chapters) == 1
        assert len(story.chapters[0].html) > 100


class TestDarkWandererDownload:
    def test_forum_thread_extracts_starter_posts(self, monkeypatch):
        html = _load("dw_thread.html")
        scraper = _scraper(DarkWandererScraper)
        # Single-page thread (nav.pageNav absent) — one fetch suffices.
        monkeypatch.setattr(scraper, "_fetch", _make_fetcher({"": html}))
        story = scraper.download(
            "https://darkwanderer.net/threads/young-black-breeding-bull-in-training.23490/"
        )
        assert story.title  # non-empty
        assert story.author  # thread starter resolved
        assert len(story.chapters) >= 1


class TestGreatFeetDownload:
    def test_single_story_title_cleaned(self, monkeypatch):
        html = _load("greatfeet_story.html")
        scraper = _scraper(GreatFeetScraper)
        monkeypatch.setattr(scraper, "_fetch", _make_fetcher({"": html}))
        story = scraper.download(
            "https://www.greatfeet.com/stories/ts1735.htm"
        )
        # The ``at greatfeet.com ...`` boilerplate suffix must be
        # stripped even when the raw <title> contains embedded
        # newlines + whitespace.
        assert story.title == "Our Feet Need To Be Worshiped"
        assert "greatfeet.com" not in story.title
        assert len(story.chapters) == 1
        body = story.chapters[0].html
        assert len(body) > 500


class TestFictionmaniaDownload:
    def test_webdna_empty_falls_back_to_text(self, monkeypatch):
        text = _load("fictionmania_text.html")
        empty_html = "<html><body></body></html>"
        scraper = _scraper(FictionmaniaScraper)

        def fetch(url, session=None):
            return text if "readtextstory" in url else empty_html
        monkeypatch.setattr(scraper, "_fetch", fetch)
        story = scraper.download(
            "https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345"
        )
        # Whatever the upstream title, download should have run to a
        # populated Story with a non-empty body wrapped in <pre>.
        assert story.title
        assert len(story.chapters) == 1
        assert story.chapters[0].html


# ── Shared download-loop invariants ───────────────────────────────

@pytest.mark.parametrize("cls,fixture,url", [
    (LushStoriesScraper, {"": "lush_story.html"},
     "https://www.lushstories.com/stories/cuckold/a-modern-relationship"),
    (SexStoriesScraper, {"": "sexstories_story.html"},
     "https://www.sexstories.com/story/114893/slug"),
    (ChyoaScraper, {"": "chyoa_chapter.html"},
     "https://chyoa.com/chapter/Ooh-that-s-hot.17"),
    (GreatFeetScraper, {"": "greatfeet_story.html"},
     "https://www.greatfeet.com/stories/ts1735.htm"),
])
def test_skip_chapters_past_end_returns_empty_chapters(
    monkeypatch, cls, fixture, url,
):
    """``skip_chapters=num_chapters`` is what ``--update`` passes
    when a local copy already matches the remote chapter count. The
    story should come back with no Chapter objects — not an error."""
    scraper = _scraper(cls)
    pages = {k: _load(v) for k, v in fixture.items()}
    monkeypatch.setattr(scraper, "_fetch", _make_fetcher(pages))
    story = scraper.download(url, skip_chapters=10)
    assert story.chapters == []
    assert story.title  # metadata still parsed


# ── Empty-page invariant ──────────────────────────────────────────

@pytest.mark.parametrize("cls,url", [
    (AFFScraper,
     "https://hp.adult-fanfiction.org/story.php?no=600100488"),
    (StoriesOnlineScraper,
     "https://storiesonline.net/s/40467/slug"),
    (NiftyScraper,
     "https://www.nifty.org/nifty/gay/college/the-brotherhood/"),
    (SexStoriesScraper,
     "https://www.sexstories.com/story/114893/slug"),
    (MCStoriesScraper,
     "https://mcstories.com/AToZeb/"),
    (LushStoriesScraper,
     "https://www.lushstories.com/stories/cuckold/a-modern-relationship"),
    (TGStorytimeScraper,
     "https://www.tgstorytime.com/viewstory.php?sid=9219"),
    (ChyoaScraper,
     "https://chyoa.com/chapter/Ooh-that-s-hot.17"),
    (DarkWandererScraper,
     "https://darkwanderer.net/threads/young-black-breeding-bull-in-training.23490/"),
    (GreatFeetScraper,
     "https://www.greatfeet.com/stories/ts1735.htm"),
    (FictionmaniaScraper,
     "https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345"),
])
def test_empty_page_never_returns_silently_empty_story(monkeypatch, cls, url):
    """If the site serves an empty page (gate, error, site redesign, or
    an unexpected ``<html><body></body></html>`` response), the scraper
    must raise rather than quietly hand back a Story with no chapters
    *and* no metadata. Silent empties are corrosive in library-update:
    they overwrite a good local copy with a stub.

    Scrapers whose download path degrades gracefully on an empty page
    (returns a Story that at least carries the upstream title) are fine
    to pass this test; it's the "no error AND no content" combination
    we're ruling out.
    """
    scraper = _scraper(cls)
    empty = "<html><body></body></html>"
    monkeypatch.setattr(
        scraper, "_fetch", _make_fetcher({"": empty}),
    )
    try:
        story = scraper.download(url)
    except Exception:
        # Any exception type counts — what we're forbidding is
        # silent success. Specific scrapers raise ValueError,
        # StoryNotFoundError, AttributeError, etc.; the contract is
        # just "something went wrong was visible to the caller".
        return
    # If it DID return a Story, at least a title OR a chapter body
    # must have made it through — an entirely blank story is the
    # failure mode we're guarding against.
    has_title = bool(story.title and story.title.strip())
    has_body = any(ch.html and ch.html.strip() for ch in story.chapters)
    assert has_title or has_body, (
        f"{cls.__name__} returned an empty Story without raising — "
        "silent empties are a library-update hazard"
    )
