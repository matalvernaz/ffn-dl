"""Author-page scraping for the erotica adapters (round-10 F4).

Offline: each scraper's _fetch is monkeypatched to serve the captured
fixture. BDSM Library deliberately has no author scraping — its
author.php pages render empty server-side (see bdsmlibrary.py)."""
from pathlib import Path

import pytest

from ficary import sites, url_classifier
from ficary.erotica.aff import AFFScraper
from ficary.erotica.sexstories import SexStoriesScraper
from ficary.erotica.storiesonline import StoriesOnlineScraper

FIXTURES = Path(__file__).parent / "fixtures" / "erotica"


def _serve(scraper, fixture, monkeypatch):
    html = (FIXTURES / fixture).read_text(encoding="utf-8", errors="replace")
    monkeypatch.setattr(scraper, "_fetch", lambda url, **kw: html)
    monkeypatch.setattr(scraper, "_delay", lambda *a, **kw: None)
    return scraper


class TestAFFAuthor:
    URL = "https://members.adult-fanfiction.org/profile.php?id=1296987001"

    def test_is_author_url(self):
        assert AFFScraper.is_author_url(self.URL)
        assert AFFScraper.is_author_url(
            "https://hp.adult-fanfiction.org/authorlinks.php?no=12345")
        assert not AFFScraper.is_author_url(
            "https://hp.adult-fanfiction.org/story.php?no=600100488")

    def test_scrape_author_works(self, monkeypatch):
        s = _serve(AFFScraper(use_cache=False), "aff_profile.html", monkeypatch)
        author, works = s.scrape_author_works(self.URL)
        assert author == "Wilde_Guess"
        assert len(works) >= 5
        assert all("story.php?no=" in w["url"] for w in works)
        # Per-fandom subdomains preserved (stories only resolve there).
        assert any("hp.adult-fanfiction.org" in w["url"] for w in works)
        assert any("cartoon.adult-fanfiction.org" in w["url"] for w in works)
        assert all(w["author"] == "Wilde_Guess" for w in works)
        urls = [w["url"] for w in works]
        assert len(urls) == len(set(urls))  # deduped

    def test_max_results_caps(self, monkeypatch):
        s = _serve(AFFScraper(use_cache=False), "aff_profile.html", monkeypatch)
        _, works = s.scrape_author_works(self.URL, max_results=3)
        assert len(works) == 3

    def test_cli_shape(self, monkeypatch):
        s = _serve(AFFScraper(use_cache=False), "aff_profile.html", monkeypatch)
        author, urls = s.scrape_author_stories(self.URL)
        assert author == "Wilde_Guess"
        assert urls and all(isinstance(u, str) for u in urls)

    def test_sites_predicate_and_classifier(self):
        assert sites.is_author_url(self.URL)
        ref = url_classifier.classify(self.URL)
        assert ref is not None
        assert ref.kind == "author_works"
        assert ref.scraper_cls is AFFScraper


class TestSOLAuthor:
    URL = "https://storiesonline.net/a/fan-fiction-man"

    def test_scrape_author_works(self, monkeypatch):
        s = _serve(StoriesOnlineScraper(use_cache=False), "sol_author.html",
                   monkeypatch)
        author, works = s.scrape_author_works(self.URL)
        assert author == "Fan Fiction Man"
        assert len(works) == 10  # one fixture page; page 2 repeats -> stop
        assert all(w["url"].startswith("https://storiesonline.net/s/") for w in works)
        titles = [w["title"] for w in works]
        assert "Anakin’s Redemption" in titles

    def test_pagination_stops_on_no_new(self, monkeypatch):
        calls = []
        html = (FIXTURES / "sol_author.html").read_text(encoding="utf-8",
                                                        errors="replace")
        s = StoriesOnlineScraper(use_cache=False)
        monkeypatch.setattr(s, "_delay", lambda *a, **kw: None)

        def fetch(url, **kw):
            calls.append(url)
            return html

        monkeypatch.setattr(s, "_fetch", fetch)
        s.scrape_author_works(self.URL)
        assert len(calls) == 2  # page 2 added nothing new -> walk stopped

    def test_classifier(self):
        ref = url_classifier.classify(self.URL)
        assert ref is not None and ref.kind == "author_works"
        assert ref.scraper_cls is StoriesOnlineScraper


class TestSexStoriesAuthor:
    URL = "https://sexstories.com/profile1176433/"

    def test_scrape_author_works(self, monkeypatch):
        s = _serve(SexStoriesScraper(use_cache=False),
                   "sexstories_profile.html", monkeypatch)
        author, works = s.scrape_author_works(self.URL)
        assert author == "ArchiesHard"
        assert works
        assert all(w["url"].startswith("https://") and "/story/" in w["url"]
                   for w in works)
        ids = [w["url"] for w in works]
        assert len(ids) == len(set(ids))

    def test_bad_url_raises(self):
        s = SexStoriesScraper(use_cache=False)
        with pytest.raises(ValueError):
            s.scrape_author_works("https://sexstories.com/story/104006/")

    def test_classifier(self):
        ref = url_classifier.classify(self.URL)
        assert ref is not None and ref.kind == "author_works"
        assert ref.scraper_cls is SexStoriesScraper
