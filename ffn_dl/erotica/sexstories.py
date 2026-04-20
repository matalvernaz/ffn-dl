"""SexStories.com (sexstories.com, XNXX Stories) scraper.

SexStories is a single-page story archive — every story lives at
``/story/<numeric_id>/<slug>`` and fits on one HTML page. Multi-part
serials are modelled as separate story ids with "part 2" / "part 3"
appended to the slug, so we don't paginate: one URL = one chapter.

Page layout (as of 2026):
    <div id="top_panel">
        <h2>Title <span class="title_link">by <a href="/profileNNN/Name">…</a></span></h2>
        <div class="top_info">Tag1, Tag2, Tag3, …</div>
    </div>
    <div class="block_panel"><h2>Introduction: </h2>…</div>   ← optional
    <div class="block_panel">…body…</div>

We emit the introduction block and the body as a single chapter. Tags
are the comma-separated list in the first ``top_info`` div — the
entire site is tag-indexed so exposing them is essential for the
unified erotica search.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

SS_BASE = "https://www.sexstories.com"

SS_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?sexstories\.com/story/(?P<id>\d+)(?:/(?P<slug>[^/?#\s]+))?",
    re.I,
)

SS_AUTHOR_URL_RE = re.compile(
    r"^https?://(?:www\.)?sexstories\.com/profile(?P<id>\d+)/", re.I,
)


class SexStoriesScraper(BaseScraper):
    """Scraper for sexstories.com stories."""

    site_name = "sexstories"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the numeric SexStories story id as an int."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = SS_STORY_URL_RE.search(text)
        if m:
            return int(m.group("id"))
        raise ValueError(
            f"Cannot parse SexStories story id from: {text!r}\n"
            "Expected a URL like https://www.sexstories.com/story/114893/slug "
            "or a bare numeric id."
        )

    @staticmethod
    def is_author_url(url):
        return bool(SS_AUTHOR_URL_RE.search(str(url)))

    @staticmethod
    def _story_url(story_id: int, slug: str = "") -> str:
        return f"{SS_BASE}/story/{story_id}/{slug}" if slug else f"{SS_BASE}/story/{story_id}"

    @staticmethod
    def _parse_metadata(soup, story_id: int, slug: str) -> dict:
        top = soup.find("div", id="top_panel")
        title = ""
        author = "Unknown Author"
        author_url = ""
        tags: list[str] = []

        if top is not None:
            h2 = top.find("h2")
            if h2:
                # Author is in a trailing <span class="title_link">.
                span = h2.find("span", class_="title_link")
                if span:
                    a = span.find("a")
                    if a:
                        author = a.get_text(strip=True) or author
                        href = a.get("href") or ""
                        if href:
                            author_url = (
                                href if href.startswith("http") else SS_BASE + href
                            )
                    span.extract()  # so the remaining h2 is just the title
                title = h2.get_text(" ", strip=True)

            info_divs = top.find_all("div", class_="top_info")
            if info_divs:
                # First top_info is the tag / category list.
                tags_raw = info_divs[0].get_text(" ", strip=True)
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        if not title:
            title = slug.replace("_", " ").strip().title() or f"SexStories {story_id}"

        # Introduction is the first block_panel with a <h2>Introduction</h2>.
        summary = ""
        for block in soup.find_all("div", class_="block_panel"):
            h2 = block.find("h2")
            label = h2.get_text(" ", strip=True).lower() if h2 else ""
            if "introduction" in label:
                # Drop the heading, take the remaining text.
                text = block.get_text(" ", strip=True)
                summary = text.replace(h2.get_text(" ", strip=True), "", 1).strip()
                break

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": 1,
            "chapter_titles": {"1": title},
            "extra": {"slug": slug, "tags": tags},
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        """Concatenate the introduction + content block_panels.

        The second ``block_panel`` (after the ``top_info`` meta block)
        is the story body; the first is the introduction when present.
        Joining both preserves the author's preface verbatim."""
        blocks = soup.find_all("div", class_="block_panel")
        if not blocks:
            raise ValueError("Could not find SexStories story body.")
        pieces: list[str] = []
        for block in blocks:
            h2 = block.find("h2")
            if h2:
                h2.extract()
            pieces.append(block.decode_contents())
        return "\n<hr />\n".join(pieces)

    def get_chapter_count(self, url_or_id):
        # All SexStories works are single-page.
        return 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        story_id = self.parse_story_id(url_or_id)
        m = SS_STORY_URL_RE.search(str(url_or_id))
        slug = m.group("slug") if (m and m.group("slug")) else ""
        story_url = self._story_url(story_id, slug)

        logger.info("Fetching SexStories story %s...", story_id)
        page_html = self._fetch(story_url)
        soup = BeautifulSoup(page_html, "lxml")

        meta = self._parse_metadata(soup, story_id, slug)
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
