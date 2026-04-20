"""MCStories (mcstories.com) — The Erotic Mind-Control Story Archive.

MCStories is a static-HTML archive running continuously since the 90s.
Its value to ffn-dl is the tag system: two-letter codes (``mc``, ``mf``,
``fd`` = female dominant, ``md`` = male dominant, ``ft`` = foot fetish,
``hu`` = humiliation, ``bd`` = bondage, etc.) indexed at
``/Tags/<code>.html`` as story lists, with those same codes echoed on
each story's index page. That makes MCStories a first-class citizen
for tag-driven search — femdom, MC, hypnosis, spanking, and adjacent
kinks all live here.

Story layout:
    ``/<StoryCode>/index.html``    — story landing (title, author, tags,
                                      synopsis, chapter list)
    ``/<StoryCode>/<ChapterN>.html`` — chapter bodies

Each story's index page carries Dublin Core metadata in
``<meta name="dcterms.*">`` tags and lists chapters in
``<div class="chapter"><a href="...">Chapter</a> (N words)</div>``.
Chapter bodies live inside ``<article id="mcstories">``.

Story ids on MCStories are string slugs, not numbers. We hash the
slug to satisfy the Story model's numeric-id contract.
"""

import hashlib
import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

MCS_BASE = "https://mcstories.com"

MCS_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?mcstories\.com/(?P<slug>[A-Za-z][A-Za-z0-9_-]+)/?(?:index\.html)?$",
    re.I,
)

MCS_CHAPTER_URL_RE = re.compile(
    r"^https?://(?:www\.)?mcstories\.com/(?P<slug>[A-Za-z][A-Za-z0-9_-]+)/(?P<file>[^/]+\.html)$",
    re.I,
)

MCS_TAG_URL_RE = re.compile(
    r"^https?://(?:www\.)?mcstories\.com/Tags/(?P<tag>[a-z]{2})\.html$", re.I,
)

MCS_AUTHOR_URL_RE = re.compile(
    r"^https?://(?:www\.)?mcstories\.com/Authors/(?P<slug>[^/]+)\.html$", re.I,
)


def _slug_to_id(slug: str) -> int:
    """Stable integer derived from the MCStories story slug."""
    h = hashlib.md5(slug.encode("utf-8")).hexdigest()[:12]
    return int(h, 16)


class MCStoriesScraper(BaseScraper):
    """Scraper for mcstories.com."""

    site_name = "mcstories"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the story slug. Use :func:`_slug_to_id` for numeric."""
        text = str(url_or_id).strip()
        m = MCS_STORY_URL_RE.search(text)
        if m:
            return m.group("slug")
        m2 = MCS_CHAPTER_URL_RE.search(text)
        if m2:
            return m2.group("slug")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]+", text):
            return text
        raise ValueError(
            f"Cannot parse MCStories slug from: {text!r}\n"
            "Expected e.g. https://mcstories.com/AToZeb/ or a bare slug."
        )

    @staticmethod
    def is_author_url(url):
        return bool(MCS_AUTHOR_URL_RE.search(str(url)))

    @staticmethod
    def _index_url(slug: str) -> str:
        return f"{MCS_BASE}/{slug}/"

    @staticmethod
    def _parse_metadata(soup, slug: str) -> dict:
        title = ""
        title_tag = soup.find("h3", class_="title")
        if title_tag is None:
            title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(" ", strip=True)

        author = "Unknown Author"
        author_url = ""
        byline = soup.find("h3", class_="byline")
        if byline:
            a = byline.find("a")
            if a:
                author = a.get_text(strip=True) or author
                href = a.get("href") or ""
                if href:
                    author_url = urljoin(MCS_BASE + f"/{slug}/", href)
        if not author_url:
            creator = soup.find("meta", attrs={"name": "dcterms.creator"})
            if creator and creator.get("content"):
                author = creator["content"].strip() or author

        summary = ""
        synopsis = soup.find("section", class_="synopsis")
        if synopsis:
            summary = synopsis.get_text(" ", strip=True)
        if not summary:
            desc = soup.find("meta", attrs={"name": "dcterms.description"})
            if desc and desc.get("content"):
                summary = desc["content"].strip()

        tags: list[str] = []
        subj = soup.find("meta", attrs={"name": "dcterms.subject"})
        if subj and subj.get("content"):
            tags = [t for t in subj["content"].split() if t]
        if not tags:
            codes = soup.find("div", class_="storyCodes")
            if codes:
                tags = [a.get_text(strip=True) for a in codes.find_all("a")]

        chapters: list[tuple[str, str]] = []
        for div in soup.find_all("div", class_="chapter"):
            a = div.find("a")
            if not a or not a.get("href"):
                continue
            href = urljoin(MCS_BASE + f"/{slug}/", a["href"])
            chapters.append((a.get_text(" ", strip=True) or "Chapter", href))

        if not chapters:
            # Single-chapter story with no chapter div — some old stories
            # link the body straight from the index's next nav arrow.
            next_link = soup.find("link", rel="next")
            if next_link and next_link.get("href"):
                href = urljoin(MCS_BASE + f"/{slug}/", next_link["href"])
                chapters.append((title or "Chapter 1", href))

        chapter_titles = {
            str(n + 1): t for n, (t, _) in enumerate(chapters)
        } or {"1": title or "Chapter 1"}
        num_chapters = len(chapters) or 1

        return {
            "title": title or slug,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "chapter_urls": [url for _, url in chapters],
            "extra": {
                "slug": slug,
                "tags": tags,
                "codes": " ".join(tags),
            },
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        article = soup.find("article", id="mcstories")
        if article is None:
            article = soup.find("article")
        if article is None:
            raise ValueError("Could not find MCStories chapter body.")
        # Drop the in-article title heading and nav so the exported
        # chapter starts with the prose.
        for selector in ("h3.title", "h3.byline", "h3.dateline",
                         "div.storyCodes", "section.synopsis",
                         "nav.arrows", "nav.story"):
            for el in article.select(selector):
                el.decompose()
        return article.decode_contents()

    def get_chapter_count(self, url_or_id):
        slug = self.parse_story_id(url_or_id)
        html = self._fetch(self._index_url(slug))
        soup = BeautifulSoup(html, "lxml")
        meta = self._parse_metadata(soup, slug)
        return meta["num_chapters"]

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        slug = self.parse_story_id(url_or_id)
        story_id = _slug_to_id(slug)
        index_url = self._index_url(slug)

        logger.info("Fetching MCStories %s...", slug)
        index_html = self._fetch(index_url)
        soup = BeautifulSoup(index_html, "lxml")

        meta = self._parse_metadata(soup, slug)
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]
        chapter_urls = meta["chapter_urls"]
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=index_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters or not chapter_urls:
            return story

        for i, chap_url in enumerate(chapter_urls, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            ch_title = chapter_titles.get(str(i), f"Chapter {i}")

            cached = self._load_chapter_cache(story_id, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue

            if story.chapters or i > 1:
                self._delay()
            logger.debug("Fetching MCStories chapter %d/%d", i, num_chapters)
            page = self._fetch(chap_url)
            ch_soup = BeautifulSoup(page, "lxml")
            html = self._parse_chapter_html(ch_soup)

            ch = Chapter(number=i, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch_title, False)

        return story
