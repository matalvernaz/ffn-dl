"""Regression tests for scheme/www-optional URL detection in sites.py.

Users paste bare hosts (``fanfiction.net/s/123``) as often as full URLs;
detection must handle those without a leading ``https://`` or ``www.`` —
while still refusing a host fragment buried in a larger token.
"""

import pytest

from ffn_dl.scraper import FFNScraper
from ffn_dl.sites import detect_scraper, extract_story_url
from ffn_dl.royalroad import RoyalRoadScraper
from ffn_dl.webnovel import WebnovelScraper


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.fanfiction.net/s/123", FFNScraper),
        ("www.fanfiction.net/s/123", FFNScraper),
        ("fanfiction.net/s/123", FFNScraper),
        ("royalroad.com/fiction/456", RoyalRoadScraper),
        ("webnovel.com/book/7931338406001705", WebnovelScraper),
        ("m.webnovel.com/book/release-that-witch_7931338406001705", WebnovelScraper),
    ],
)
def test_detect_scraper_scheme_and_www_optional(url, expected):
    assert detect_scraper(url) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("read fanfiction.net/s/999 tonight", "https://fanfiction.net/s/999"),
        ("here: webnovel.com/book/abc_777 ok", "https://webnovel.com/book/abc_777"),
        ("https://www.royalroad.com/fiction/5", "https://www.royalroad.com/fiction/5"),
    ],
)
def test_extract_story_url_finds_bare_hosts(text, expected):
    assert extract_story_url(text) == expected


def test_extract_story_url_guards_against_host_substring():
    # ``notfanfiction.net`` must NOT match ``fanfiction.net`` now that the
    # scheme (which used to act as the left anchor) is optional.
    assert extract_story_url("notfanfiction.net/s/1") is None
