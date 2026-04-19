"""Central registry of supported sites.

Keeps site-detection logic in one place so the CLI, clipboard watcher,
and GUI share a single source of truth for URL patterns instead of
each maintaining their own copies.
"""

import re
from typing import Optional

from .ao3 import AO3Scraper
from .ficwad import FicWadScraper
from .literotica import LiteroticaScraper
from .mediaminer import MediaMinerScraper
from .royalroad import RoyalRoadScraper
from .scraper import BaseScraper, FFNScraper
from .wattpad import WattpadScraper


_STORY_URL_PATTERNS: list[tuple[type[BaseScraper], re.Pattern[str]]] = [
    (FicWadScraper, re.compile(r"https?://(?:www\.)?ficwad\.com/story/\d+", re.I)),
    (
        AO3Scraper,
        re.compile(
            r"https?://(?:www\.)?(?:archiveofourown\.org|ao3\.org)/works/\d+",
            re.I,
        ),
    ),
    (
        RoyalRoadScraper,
        re.compile(r"https?://(?:www\.)?royalroad\.com/fiction/\d+", re.I),
    ),
    (
        MediaMinerScraper,
        re.compile(
            r"https?://(?:www\.)?mediaminer\.org/fanfic/"
            r"(?:view_st\.php/\d+|s/[^?#\s]+?/\d+)",
            re.I,
        ),
    ),
    (
        LiteroticaScraper,
        re.compile(r"https?://(?:www\.)?literotica\.com/s/[a-z0-9-]+", re.I),
    ),
    (
        WattpadScraper,
        re.compile(
            r"https?://(?:www\.|m\.)?wattpad\.com/(?:story/)?\d+", re.I
        ),
    ),
    (
        FFNScraper,
        re.compile(r"https?://(?:www\.)?fanfiction\.net/s/\d+", re.I),
    ),
]

# Hostname fragments for sites that don't require the full /s/N etc.
# path — used when the caller already knows they have a story URL and
# just needs to pick the scraper class (e.g. after the user pastes a
# bare URL or the CLI has the full argument in hand).
_HOSTNAME_TO_SCRAPER: list[tuple[str, type[BaseScraper]]] = [
    ("ficwad.com", FicWadScraper),
    ("archiveofourown.org", AO3Scraper),
    ("ao3.org", AO3Scraper),
    ("royalroad.com", RoyalRoadScraper),
    ("mediaminer.org", MediaMinerScraper),
    ("literotica.com", LiteroticaScraper),
    ("wattpad.com", WattpadScraper),
]

# Scrapers whose is_author_url / is_series_url static methods should be
# consulted when classifying a URL.
ALL_SCRAPERS: list[type[BaseScraper]] = [
    FFNScraper,
    FicWadScraper,
    AO3Scraper,
    RoyalRoadScraper,
    MediaMinerScraper,
    LiteroticaScraper,
    WattpadScraper,
]


def detect_scraper(url: str) -> type[BaseScraper]:
    """Return the scraper class that handles ``url``.

    Falls back to FFNScraper for bare numeric IDs and unrecognised
    hostnames — FFN has historically been the default "just give me a
    number" behaviour.
    """
    text = str(url).lower()
    for hostname, scraper_cls in _HOSTNAME_TO_SCRAPER:
        if hostname in text:
            return scraper_cls
    return FFNScraper


def is_author_url(url: str) -> bool:
    """Return True if ``url`` is an author page on any supported site."""
    return any(cls.is_author_url(url) for cls in ALL_SCRAPERS)


def is_series_url(url: str) -> bool:
    """Return True if ``url`` is a series page (AO3 or Literotica)."""
    return AO3Scraper.is_series_url(url) or LiteroticaScraper.is_series_url(url)


def extract_story_url(text: str) -> Optional[str]:
    """Return the first supported story URL found in ``text``, or None.

    Used by the clipboard watcher — users paste whole paragraphs or
    URLs-with-query-strings, and we want the canonical story URL we
    know how to download.
    """
    for _, pattern in _STORY_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None
