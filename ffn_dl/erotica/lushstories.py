"""Lushstories (lushstories.com) scraper.

Lushstories is a modern tag-heavy erotica site — ``/stories/<category>/<slug>``
is the canonical story URL, where ``<category>`` covers tags like
``feet``, ``femdom``, ``spanking``, ``cuckold``, ``cheating``, etc. The
site is Nuxt/Vue SSR: the prose is still in the initial HTML response
(which is why ffn-dl can scrape it without a headless browser), but
tagged up with Tailwind classes and Vue data hashes.

Story text lives in one or more ``<div class="story-body ...">``
blocks — the site splits very long stories around inline ad units, so
we collect every such block on the page and concatenate them. Multi-
part stories are published as separate URLs with ``-2``, ``-3`` suffixes
on the slug, so every URL is one chapter for our purposes.
"""

import hashlib
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

LUSH_BASE = "https://www.lushstories.com"

LUSH_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?lushstories\.com/stories/(?P<category>[a-z0-9-]+)/(?P<slug>[a-z0-9][a-z0-9-]+)/?",
    re.I,
)

LUSH_CATEGORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?lushstories\.com/stories/(?P<category>[a-z0-9-]+)/?$",
    re.I,
)

LUSH_AUTHOR_URL_RE = re.compile(
    r"^https?://(?:www\.)?lushstories\.com/profile/(?P<user>[^/]+)", re.I,
)


def _slug_to_id(category: str, slug: str) -> int:
    """Stable integer id derived from category + slug so the same
    story URL always hashes to the same numeric key."""
    h = hashlib.md5(f"{category}/{slug}".encode("utf-8")).hexdigest()[:12]
    return int(h, 16)


class LushStoriesScraper(BaseScraper):
    """Scraper for lushstories.com stories."""

    site_name = "lushstories"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return ``(category, slug)`` as a tuple.

        Different from the other scrapers — Lush needs both halves to
        rebuild the canonical URL — so we lean on the tuple rather than
        a single id. Callers that need a single numeric key should run
        the tuple through :func:`_slug_to_id`.
        """
        text = str(url_or_id).strip()
        m = LUSH_STORY_URL_RE.search(text)
        if m:
            return (m.group("category").lower(), m.group("slug").lower())
        raise ValueError(
            f"Cannot parse Lushstories URL from: {text!r}\n"
            "Expected e.g. https://www.lushstories.com/stories/feet/foot-worship"
        )

    @staticmethod
    def is_author_url(url):
        return bool(LUSH_AUTHOR_URL_RE.search(str(url)))

    @staticmethod
    def _story_url(category: str, slug: str) -> str:
        return f"{LUSH_BASE}/stories/{category}/{slug}"

    @staticmethod
    def _parse_metadata(soup, category: str, slug: str) -> dict:
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
        if not title:
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                title = og_title["content"].strip()
        if not title:
            title = slug.replace("-", " ").title()

        author = "Unknown Author"
        author_url = ""
        author_link = soup.find("a", href=re.compile(r"^/profile/"))
        if author_link:
            href = author_link.get("href") or ""
            if href:
                author_url = LUSH_BASE + href
            # Author's name is in a nested span, not the <a> text.
            name_span = author_link.find(
                "span", class_=re.compile(r"font-medium")
            )
            if name_span:
                author = name_span.get_text(strip=True) or author
            else:
                label = author_link.get("aria-label")
                if label:
                    author = label.strip()

        summary = ""
        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            summary = desc["content"].strip()
        if not summary:
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            if og_desc and og_desc.get("content"):
                summary = og_desc["content"].strip()

        extra = {
            "category": category,
            "slug": slug,
            "tags": [category],
        }
        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": 1,
            "chapter_titles": {"1": title},
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        """Concatenate every ``.story-body`` block on the page.

        Lush splits long stories around inline ad units (featured
        advert cards), each break landing the prose in a fresh
        ``.story-body`` div. Joining them in document order recovers
        the full story."""
        blocks = soup.find_all("div", class_=re.compile(r"\bstory-body\b"))
        if not blocks:
            raise ValueError(
                "Could not find Lushstories story body "
                "(page layout may have changed)."
            )
        pieces: list[str] = []
        for block in blocks:
            inner = block.decode_contents().strip()
            if inner:
                pieces.append(inner)
        if not pieces:
            raise ValueError("Lushstories story body was empty.")
        return "\n".join(pieces)

    def get_chapter_count(self, url_or_id):
        # Lush multi-part serials are separate URLs; each URL is one chapter.
        return 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        category, slug = self.parse_story_id(url_or_id)
        story_id = _slug_to_id(category, slug)
        story_url = self._story_url(category, slug)

        logger.info("Fetching Lushstories %s/%s...", category, slug)
        page_html = self._fetch(story_url)
        soup = BeautifulSoup(page_html, "lxml")

        meta = self._parse_metadata(soup, category, slug)
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= 1:
            return story
        if not chapter_in_spec(1, chapters):
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
