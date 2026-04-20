"""Chyoa (chyoa.com) scraper — interactive CYOA erotica.

Chyoa is a "choose your own adventure" erotica platform. Every work is
a tree of chapters where each chapter forks into multiple child
chapters; readers pick a branch at each decision point. Our linear
chapter model doesn't capture the branching graph, so we simplify:
**one URL = one chapter download**.

If you paste a story URL (``/story/<slug>.<id>``), you get the root
chapter (the story's starting point). If you paste a chapter URL
(``/chapter/<slug>.<id>``), you get that chapter. A future extension
could walk the "most-visited path" through the tree to produce a
coherent linear reading, but that needs a design decision — hiding
less-popular branches silently is a destructive heuristic, so we
ship the explicit single-chapter behaviour first and leave tree
traversal for a follow-up.

HTML is clean server-side rendered: ``<h1>`` holds the chapter
title, ``<div class="chapter-content">`` holds the prose, and OG meta
tags carry the summary and canonical URL.
"""

import hashlib
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

CHYOA_BASE = "https://chyoa.com"

CHYOA_STORY_RE = re.compile(
    r"^https?://(?:www\.)?chyoa\.com/story/([^/?#\s]+)\.(\d+)", re.I,
)

CHYOA_CHAPTER_RE = re.compile(
    r"^https?://(?:www\.)?chyoa\.com/chapter/([^/?#\s]+)\.(\d+)", re.I,
)


def _slug_id_to_int(slug: str, numeric: int) -> int:
    """Combine Chyoa's slug + numeric id into a stable integer key so
    two URL variants for the same chapter hash to the same cache dir."""
    h = hashlib.md5(f"{slug}:{numeric}".encode("utf-8")).hexdigest()[:10]
    return int(h, 16)


class ChyoaScraper(BaseScraper):
    """Scraper for chyoa.com chapters/stories (single-chapter mode)."""

    site_name = "chyoa"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return ``(kind, slug, numeric)`` where ``kind`` is ``'story'``
        or ``'chapter'``. Callers needing a single int should run the
        slug + numeric through :func:`_slug_id_to_int`."""
        text = str(url_or_id).strip()
        m = CHYOA_STORY_RE.search(text)
        if m:
            return ("story", m.group(1), int(m.group(2)))
        m = CHYOA_CHAPTER_RE.search(text)
        if m:
            return ("chapter", m.group(1), int(m.group(2)))
        raise ValueError(
            f"Cannot parse Chyoa URL from: {text!r}\n"
            "Expected e.g. https://chyoa.com/story/Dominant-Girlfriend.14 "
            "or https://chyoa.com/chapter/Ooh-that-s-hot.17"
        )

    @staticmethod
    def _canonical_url(kind: str, slug: str, numeric: int) -> str:
        return f"{CHYOA_BASE}/{kind}/{slug}.{numeric}"

    @staticmethod
    def _parse_metadata(soup, kind: str, numeric: int) -> dict:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        title = (og_title.get("content") if og_title else "") or ""
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
        title = title.strip() or f"Chyoa {kind} {numeric}"

        summary = (og_desc.get("content") if og_desc else "") or ""

        author = "Unknown Author"
        author_url = ""
        author_link = soup.find("a", href=re.compile(r"^/user/"))
        if author_link:
            author = author_link.get_text(strip=True) or author
            href = author_link.get("href", "")
            if href:
                author_url = (
                    href if href.startswith("http") else CHYOA_BASE + href
                )

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": 1,
            "chapter_titles": {"1": title},
            "extra": {"chyoa_kind": kind, "numeric_id": numeric},
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        content = soup.find("div", class_="chapter-content")
        if content is None:
            # Fall back: pick the article body or the first long div.
            content = soup.find("article") or soup.find("div", id="content")
        if content is None:
            raise ValueError("Could not find Chyoa chapter body.")
        # Drop Chyoa's ad-zones and author-bio inserts that share the
        # page but aren't chapter prose.
        for selector in ("div.chyoa-adzone", "div.chyoa-banner",
                         "div.chapter-nav", "div.chapter-choices"):
            for el in content.select(selector):
                el.decompose()
        return content.decode_contents()

    def get_chapter_count(self, url_or_id):
        return 1  # single-chapter mode — see module docstring

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        kind, slug, numeric = self.parse_story_id(url_or_id)
        story_id = _slug_id_to_int(slug, numeric)
        page_url = self._canonical_url(kind, slug, numeric)

        logger.info("Fetching Chyoa %s %s.%s ...", kind, slug, numeric)
        page_html = self._fetch(page_url)
        soup = BeautifulSoup(page_html, "lxml")

        meta = self._parse_metadata(soup, kind, numeric)
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=page_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= 1 or not chapter_in_spec(1, chapters):
            return story

        cached = self._load_chapter_cache(story_id, 1)
        if cached is not None:
            story.chapters.append(cached)
            if progress_callback:
                progress_callback(1, 1, cached.title, True)
            return story

        body = self._parse_chapter_html(soup)
        ch = Chapter(number=1, title=meta["title"], html=body)
        self._save_chapter_cache(story_id, ch)
        story.chapters.append(ch)
        if progress_callback:
            progress_callback(1, 1, meta["title"], False)
        return story
