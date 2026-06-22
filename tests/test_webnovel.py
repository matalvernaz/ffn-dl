"""Tests for the webnovel.com scraper.

No network: parsing methods run against saved fixtures, and the download
path is driven with ``_fetch`` / ``_api_get_content`` monkeypatched. The
fixtures mirror the live API shapes verified 2026-06-22.
"""

import json
from pathlib import Path

import pytest

from ffn_dl.webnovel import (
    WebnovelLockedStoryError,
    WebnovelScraper,
    _LOCKED_NOTICE,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _load_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def meta_data():
    return _load_json("webnovel_meta.json")["data"]


@pytest.fixture(scope="session")
def free_info():
    return _load_json("webnovel_chapter_free.json")["data"]["chapterInfo"]


@pytest.fixture(scope="session")
def locked_info():
    return _load_json("webnovel_chapter_locked.json")["data"]["chapterInfo"]


@pytest.fixture(scope="session")
def catalog_html():
    return _load_text("webnovel_catalog.html")


def _scraper(**kwargs):
    """A network-free scraper instance (no disk cache)."""
    kwargs.setdefault("use_cache", False)
    return WebnovelScraper(**kwargs)


class TestURLParsing:
    def test_bare_numeric_id(self):
        assert WebnovelScraper.parse_story_id("7931338406001705") == 7931338406001705

    def test_book_url_with_slug(self):
        assert WebnovelScraper.parse_story_id(
            "https://www.webnovel.com/book/release-that-witch_7931338406001705"
        ) == 7931338406001705

    def test_book_url_without_slug(self):
        assert WebnovelScraper.parse_story_id(
            "https://www.webnovel.com/book/7931338406001705"
        ) == 7931338406001705

    def test_mobile_subdomain(self):
        assert WebnovelScraper.parse_story_id(
            "m.webnovel.com/book/7931338406001705"
        ) == 7931338406001705

    def test_scheme_optional(self):
        assert WebnovelScraper.parse_story_id(
            "webnovel.com/book/7931338406001705"
        ) == 7931338406001705

    def test_rejects_non_webnovel(self):
        with pytest.raises(ValueError):
            WebnovelScraper.parse_story_id("https://example.com/book/abc")


class TestMetadata:
    def test_build_metadata(self, meta_data):
        meta = WebnovelScraper._build_metadata(meta_data["bookInfo"])
        assert meta["title"] == "Release That Witch"
        assert meta["author"] == "Second Eye"
        assert "honorable prince" in meta["summary"]
        assert meta["extra"]["status"] == "Complete"
        assert meta["extra"]["genre"] == "Fantasy"
        assert meta["extra"]["num_chapters"] == 4
        assert meta["extra"]["cover_url"] == (
            "https://book-pic.webnovel.com/bookcover/7931338406001705"
        )

    def test_author_falls_back_to_items(self):
        meta = WebnovelScraper._build_metadata(
            {"bookName": "X", "authorItems": [{"name": "A"}, {"name": "B"}]}
        )
        assert meta["author"] == "A, B"

    def test_unknown_action_status_is_in_progress_default(self):
        # An unrecognised actionStatus must not be mislabelled "Complete".
        meta = WebnovelScraper._build_metadata(
            {"bookName": "X", "actionStatus": 999}
        )
        assert meta["extra"].get("status") is None


class TestCatalogParsing:
    def test_parses_all_chapters_in_order(self, catalog_html):
        chapters = WebnovelScraper._parse_catalog(catalog_html)
        assert [c["title"] for c in chapters] == [
            "Becoming a Prince", "The Castle", "Witches", "A Family Letter",
        ]

    def test_chapter_ids_extracted(self, catalog_html):
        chapters = WebnovelScraper._parse_catalog(catalog_html)
        assert chapters[0]["id"] == "21399558403711764"

    def test_lock_flag_from_svg_icon(self, catalog_html):
        chapters = WebnovelScraper._parse_catalog(catalog_html)
        assert [c["locked"] for c in chapters] == [False, False, False, True]


class TestChapterFormatting:
    def test_plain_text_wrapped_in_paragraph(self):
        assert WebnovelScraper._format_paragraph("Hello world.") == "<p>Hello world.</p>"

    def test_angle_brackets_escaped(self):
        out = WebnovelScraper._format_paragraph("a < b > c")
        assert "&lt;" in out and "&gt;" in out and "<p>" in out

    def test_anti_piracy_line_stripped(self):
        out = WebnovelScraper._format_paragraph(
            "Find authorized novels in Webnovel, better experience, please click for visiting."
        )
        assert out == ""

    def test_chapter_html_joins_paragraphs(self, free_info):
        html = WebnovelScraper._chapter_html(free_info)
        assert html.count("<p>") == 2  # 2 real paragraphs; piracy line stripped
        assert "Cheng Yan felt" in html
        assert "Find authorized novels" not in html


class TestLockDetection:
    def test_free_chapter_returns_real_html(self, free_info):
        s = _scraper()
        s._api_get_content = lambda book_id, cid: {"chapterInfo": free_info}
        html, locked = s._fetch_chapter(7931338406001705, "21399558403711764")
        assert not locked
        assert "Cheng Yan felt" in html

    def test_locked_chapter_returns_stub(self, locked_info):
        s = _scraper()
        s._api_get_content = lambda book_id, cid: {"chapterInfo": locked_info}
        html, locked = s._fetch_chapter(7931338406001705, "21417334739406411")
        assert locked
        assert html == _LOCKED_NOTICE
        # The teaser body must NOT leak into the chapter.
        assert "firewood burned" not in html


class TestCookieSeeding:
    def test_parse_cookie_header(self):
        pairs = WebnovelScraper._parse_cookie_header("userId=42; ticket=abc;  empty ")
        assert pairs == [("userId", "42"), ("ticket", "abc")]

    def test_no_cookie_means_no_auth(self):
        s = _scraper()
        assert s._auth_cookies == []

    def test_cookies_seeded_into_session(self):
        s = _scraper(session_cookie="userId=42; ticket=abc")
        names = {c.name: c.value for c in s.session.cookies.jar}
        assert names.get("userId") == "42"
        assert names.get("ticket") == "abc"

    def test_new_session_reseeds_cookies(self):
        s = _scraper(session_cookie="userId=42")
        worker = s._new_session()
        assert any(c.name == "userId" for c in worker.cookies.jar)


class _DownloadHarness:
    """Drive download() offline by faking the two network surfaces."""

    def __init__(self, scraper, catalog_html, meta_data, free_info, locked_info):
        self.scraper = scraper
        self.api_calls = []
        scraper._fetch = self._fetch
        scraper._api_get_content = self._api
        self._catalog = catalog_html
        self._meta = meta_data
        self._free = free_info
        self._locked = locked_info
        # The catalog marks "21417334739406411" locked.
        self._locked_cid = "21417334739406411"

    def _fetch(self, url):
        if url.endswith("/catalog"):
            return self._catalog
        raise AssertionError(f"unexpected _fetch: {url}")

    def _api(self, book_id, chapter_id):
        self.api_calls.append(chapter_id)
        if chapter_id == 0:
            return self._meta
        info = self._locked if str(chapter_id) == self._locked_cid else self._free
        return {"chapterInfo": info}


class TestDownloadOffline:
    def test_logged_out_stubs_locked_without_api_call(
        self, catalog_html, meta_data, free_info, locked_info,
    ):
        s = _scraper()
        h = _DownloadHarness(s, catalog_html, meta_data, free_info, locked_info)
        story = s.download("https://www.webnovel.com/book/7931338406001705")

        assert story.title == "Release That Witch"
        assert len(story.chapters) == 4
        assert story.chapters[3].html == _LOCKED_NOTICE
        assert "Cheng Yan felt" in story.chapters[0].html
        # Logged out: the locked chapter must be stubbed WITHOUT a request.
        assert "21417334739406411" not in [str(c) for c in h.api_calls]
        # Metadata (chapterId 0) + 3 free chapters were fetched.
        assert h.api_calls.count(0) == 1

    def test_authed_attempts_locked_chapter(
        self, catalog_html, meta_data, free_info, locked_info,
    ):
        s = _scraper(session_cookie="userId=42")
        h = _DownloadHarness(s, catalog_html, meta_data, free_info, locked_info)
        story = s.download("https://www.webnovel.com/book/7931338406001705")

        # Logged in: it DOES request the catalog-locked chapter (the account
        # might have unlocked it); here the fixture says isAuth=0 so it's
        # still stubbed.
        assert "21417334739406411" in [str(c) for c in h.api_calls]
        assert story.chapters[3].html == _LOCKED_NOTICE

    def test_all_locked_raises(
        self, catalog_html, meta_data, free_info, locked_info,
    ):
        s = _scraper()
        h = _DownloadHarness(s, catalog_html, meta_data, free_info, locked_info)
        with pytest.raises(WebnovelLockedStoryError) as exc:
            # skip the 3 free chapters; only the locked chapter 4 remains.
            s.download(
                "https://www.webnovel.com/book/7931338406001705",
                skip_chapters=3,
            )
        assert "--webnovel-cookie" in str(exc.value)
