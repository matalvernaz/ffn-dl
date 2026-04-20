"""Fictionmania (fictionmania.tv) scraper.

Fictionmania is the largest transgender / crossdressing / ABDL fiction
archive — open since the late 90s. The site runs on WebDNA, so URLs
have an ``.html`` extension even though they're dynamically templated.
The canonical reader URL is
``https://fictionmania.tv/stories/readhtmlstory.html?storyID=<NNNNN>``
for HTML output; ``readtextstory.html?storyID=<N>`` serves the same
story as plain text, which we fall back to when HTML parsing fails
because the WebDNA template sometimes renders the HTML version empty.

Because Fictionmania story pages embed the whole body in a single
HTML page, each story is a single chapter in our model.

Multi-part serials have separate story ids, same as SexStories.
"""

import html as html_module
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

FM_BASE = "https://fictionmania.tv"

FM_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?fictionmania\.tv/stories/read(?:html|text)story\.html\?storyID=(?P<id>\d+)",
    re.I,
)


class FictionmaniaScraper(BaseScraper):
    """Scraper for fictionmania.tv stories."""

    site_name = "fictionmania"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the numeric Fictionmania story id as an int."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = FM_STORY_URL_RE.search(text)
        if m:
            return int(m.group("id"))
        raise ValueError(
            f"Cannot parse Fictionmania story id from: {text!r}\n"
            "Expected e.g. https://fictionmania.tv/stories/readhtmlstory.html?storyID=12345 "
            "or a bare numeric id."
        )

    @staticmethod
    def _html_url(story_id: int) -> str:
        return f"{FM_BASE}/stories/readhtmlstory.html?storyID={story_id}"

    @staticmethod
    def _text_url(story_id: int) -> str:
        return f"{FM_BASE}/stories/readtextstory.html?storyID={story_id}"

    @staticmethod
    def _parse_metadata(soup, story_id: int) -> dict:
        title = ""
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(" ", strip=True)
            # Fictionmania titles end with " - Fictionmania"; strip it.
            title = re.sub(r"\s*-\s*Fictionmania\s*$", "", raw).strip()
        if not title:
            h1 = soup.find(["h1", "h2"])
            if h1:
                title = h1.get_text(" ", strip=True)
        if not title:
            title = f"Fictionmania {story_id}"

        author = "Unknown Author"
        author_url = ""
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "storylistparam.html" in href and "authorName=" in href:
                author = a.get_text(strip=True) or author
                author_url = href if href.startswith("http") else FM_BASE + "/" + href.lstrip("/")
                break

        summary = ""
        # Fictionmania often puts the description in italics just
        # under the title: <i>... synopsis ...</i>. Take the first
        # italic block whose text exceeds 30 chars as the blurb.
        for i_tag in soup.find_all("i"):
            text = i_tag.get_text(" ", strip=True)
            if len(text) > 30:
                summary = text
                break

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": 1,
            "chapter_titles": {"1": title},
            "extra": {},
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        """Extract the story body. Fictionmania wraps the prose
        inside a ``<blockquote>`` or, failing that, directly in the
        ``<body>`` with the nav stripped. We take everything after
        the synopsis block."""
        body_tag = soup.find("body")
        if body_tag is None:
            raise ValueError("Fictionmania page had no <body>.")
        # Prefer a blockquote around the body if present.
        bq = body_tag.find("blockquote")
        if bq is not None:
            return bq.decode_contents()
        # Otherwise drop obvious nav / heading anchors and emit body.
        for selector in ("a[name]", "form", "script", "style"):
            for el in body_tag.select(selector):
                el.decompose()
        return body_tag.decode_contents()

    def _fetch_with_text_fallback(self, story_id: int) -> tuple[str, bool]:
        """Fetch the HTML-formatted page; if the WebDNA template
        returned an empty shell (e.g. ``<!HAS_WEBDNA_TAGS>`` preamble
        with no story body), fall back to the plain-text URL and
        wrap it. Returns ``(html, is_wrapped_text)``."""
        html = self._fetch(self._html_url(story_id))
        # Heuristic: a "real" story page has lots of prose; an empty
        # WebDNA template is under 2 KB. Only fall back when empty.
        if len(html) > 2048 and "<body" in html.lower():
            return html, False
        logger.info(
            "Fictionmania HTML page empty for %s; falling back to text.",
            story_id,
        )
        text = self._fetch(self._text_url(story_id))
        wrapped = (
            "<html><head><title>Fictionmania text story</title></head>"
            "<body><pre>" + html_module.escape(text) + "</pre></body></html>"
        )
        return wrapped, True

    def get_chapter_count(self, url_or_id):
        return 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        story_id = self.parse_story_id(url_or_id)
        story_url = self._html_url(story_id)

        logger.info("Fetching Fictionmania story %s...", story_id)
        page_html, wrapped_text = self._fetch_with_text_fallback(story_id)
        soup = BeautifulSoup(page_html, "lxml")

        meta = self._parse_metadata(soup, story_id)
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
