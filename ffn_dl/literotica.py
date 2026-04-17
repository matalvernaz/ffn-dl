"""Literotica (literotica.com) scraper.

Literotica publishes a single story as one URL (/s/<slug>) that may be
paginated via ?page=N. We treat each page as a chapter in the Story
model so long stories navigate naturally in EPUB readers. Related
stories (serial fiction) are grouped under /series/se/<id>; expanding
those works the same way as AO3 series.

The layout uses CSS-module hashed class names that change between
site builds, so selectors match on the module *prefix* (e.g.
`_article__content_`) rather than the full obfuscated class.
"""

import hashlib
import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story, chapter_in_spec
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

LIT_BASE = "https://www.literotica.com"

_SLUG_RE = re.compile(r"literotica\.com/s/([a-z0-9-]+)", re.IGNORECASE)
_SERIES_RE = re.compile(r"literotica\.com/series/se/(\d+)", re.IGNORECASE)
_AUTHOR_RE = re.compile(r"literotica\.com/authors/([^/?#]+)", re.IGNORECASE)


def _slug_to_id(slug: str) -> int:
    """Stable integer derived from the story slug — Literotica's canonical
    identifier is a string, but our Story model expects numeric ids so we
    hash deterministically. 48 bits is plenty of room to avoid collisions
    across a user's library."""
    h = hashlib.md5(slug.encode("utf-8")).hexdigest()[:12]
    return int(h, 16)


class LiteroticaScraper(BaseScraper):
    """Scraper for literotica.com stories and series."""

    site_name = "literotica"

    def __init__(self, **kwargs):
        kwargs.setdefault("delay_range", (1.0, 3.0))
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the *slug* (not an int) — use _slug_to_id for numeric.

        Callers that want a numeric id should go through _slug_to_id.
        """
        text = str(url_or_id).strip()
        m = _SLUG_RE.search(text)
        if m:
            return m.group(1)
        # Accept a bare slug as well
        if re.fullmatch(r"[a-z0-9][a-z0-9-]+", text, re.IGNORECASE):
            return text
        raise ValueError(
            f"Cannot parse Literotica story slug from: {text!r}\n"
            "Expected a URL like https://www.literotica.com/s/story-slug "
            "or a bare slug."
        )

    @staticmethod
    def is_author_url(url):
        return bool(_AUTHOR_RE.search(str(url)))

    @staticmethod
    def is_series_url(url):
        return bool(_SERIES_RE.search(str(url)))

    @staticmethod
    def _content_div(soup):
        """The story body is in a div whose CSS-module class starts
        with '_article__content_'. Module hashes change between builds,
        so we match on the prefix."""
        return soup.find(
            "div", class_=re.compile(r"^_article__content_")
        )

    @staticmethod
    def _intro_div(soup):
        return soup.find(
            "div", class_=re.compile(r"^_introduction__text_")
        )

    @staticmethod
    def _page_count(soup):
        """Return the number of paginated pages for this story."""
        max_page = 1
        for a in soup.find_all("a", href=re.compile(r"\?page=\d+")):
            m = re.search(r"\?page=(\d+)", a["href"])
            if m:
                max_page = max(max_page, int(m.group(1)))
        return max_page

    @staticmethod
    def _parse_author(soup):
        """Return (author_name, author_url) from the first /authors/ link
        that carries visible text (not an icon-only bookmark link)."""
        name = "Unknown Author"
        url = ""
        for a in soup.find_all("a", href=_AUTHOR_RE):
            text = a.get_text(strip=True)
            if text and len(text) < 40 and not text[0].isdigit():
                name = text
                href = a["href"]
                url = href if href.startswith("http") else LIT_BASE + href
                break
        if not url:
            # Fall back to the slug in the href
            a = soup.find("a", href=_AUTHOR_RE)
            if a:
                m = _AUTHOR_RE.search(a["href"])
                if m:
                    name = m.group(1)
                    url = f"{LIT_BASE}/authors/{name}/works/stories"
        return name, url

    def _parse_metadata(self, soup, slug):
        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else slug

        author, author_url = self._parse_author(soup)

        intro = self._intro_div(soup)
        summary = intro.get_text(" ", strip=True) if intro else ""

        num_pages = self._page_count(soup)

        extra = {"num_pages": num_pages}

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
            "num_pages": num_pages,
        }

    def _fetch_page(self, slug, page_num):
        url = f"{LIT_BASE}/s/{slug}"
        if page_num > 1:
            url += f"?page={page_num}"
        return self._fetch(url)

    def get_chapter_count(self, url_or_id):
        slug = self.parse_story_id(url_or_id)
        html = self._fetch(f"{LIT_BASE}/s/{slug}")
        soup = BeautifulSoup(html, "lxml")
        return self._page_count(soup)

    def scrape_author_stories(self, url):
        """Return (author_name, [story_urls]) for a Literotica author page."""
        m = _AUTHOR_RE.search(str(url))
        if not m:
            raise ValueError(f"Not a Literotica author URL: {url}")
        slug = m.group(1)
        works_url = f"{LIT_BASE}/authors/{slug}/works/stories"
        html = self._fetch(works_url)
        soup = BeautifulSoup(html, "lxml")

        author_name = slug
        # Try heading text first
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text and len(text) < 60:
                author_name = text

        seen = set()
        story_urls = []
        for a in soup.find_all("a", href=_SLUG_RE):
            m = _SLUG_RE.search(a["href"])
            if not m:
                continue
            story_slug = m.group(1)
            # Skip known non-story slugs (promo banners etc. use /s/ for events)
            if story_slug in seen:
                continue
            seen.add(story_slug)
            story_urls.append(f"{LIT_BASE}/s/{story_slug}")

        return author_name, story_urls

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from a Literotica author page."""
        m = _AUTHOR_RE.search(str(url))
        if not m:
            raise ValueError(f"Not a Literotica author URL: {url}")
        slug = m.group(1)
        works_url = f"{LIT_BASE}/authors/{slug}/works/stories"
        html = self._fetch(works_url)
        soup = BeautifulSoup(html, "lxml")

        author_name = slug
        h1 = soup.find("h1")
        if h1:
            text = h1.get_text(strip=True)
            if text and len(text) < 60:
                author_name = text

        seen = set()
        works = []
        for a in soup.find_all("a", href=_SLUG_RE):
            m2 = _SLUG_RE.search(a["href"])
            if not m2:
                continue
            story_slug = m2.group(1)
            if story_slug in seen:
                continue
            seen.add(story_slug)
            works.append({
                "title": a.get_text(strip=True) or story_slug,
                "url": f"{LIT_BASE}/s/{story_slug}",
                "author": author_name,
                "words": "",
                "chapters": "",
                "rating": "",
                "fandom": "",
                "status": "",
                "updated": "",
                "section": "own",
            })
        return author_name, works

    def scrape_series_works(self, url):
        """Return (series_name, [story_urls]) for a Literotica series page."""
        m = _SERIES_RE.search(str(url))
        if not m:
            raise ValueError(f"Not a Literotica series URL: {url}")
        series_id = m.group(1)
        page_url = f"{LIT_BASE}/series/se/{series_id}"
        html = self._fetch(page_url)
        soup = BeautifulSoup(html, "lxml")

        series_name = "Literotica series"
        h1 = soup.find("h1")
        if h1:
            t = h1.get_text(strip=True)
            if t:
                series_name = t

        seen = set()
        story_urls = []
        for a in soup.find_all("a", href=_SLUG_RE):
            m2 = _SLUG_RE.search(a["href"])
            if not m2:
                continue
            slug = m2.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            story_urls.append(f"{LIT_BASE}/s/{slug}")
        return series_name, story_urls

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        slug = self.parse_story_id(url_or_id)
        story_id = _slug_to_id(slug)
        story_url = f"{LIT_BASE}/s/{slug}"

        logger.info("Fetching Literotica story %s...", slug)
        page1_html = self._fetch_page(slug, 1)
        soup = BeautifulSoup(page1_html, "lxml")

        meta = self._parse_metadata(soup, slug)
        num_pages = meta["num_pages"]
        meta["extra"]["slug"] = slug
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        chrome_prefixes = (
            "_widget", "_pager", "_pagination",
            "_share", "_tags", "_introduction",
            "_rating", "_comments",
        )

        def extract_body(page_soup):
            body = self._content_div(page_soup)
            if body is None:
                raise ValueError(
                    "Could not locate Literotica story body "
                    "(page layout may have changed)."
                )
            # Drop site chrome & pagination widgets that also land inside
            # _article__content_. Collect victims first, then decompose,
            # so iterating a live tree doesn't invalidate references.
            victims = []
            for tag in body.find_all(True):
                if tag.attrs is None:
                    continue
                classes = tag.attrs.get("class") or []
                if any(
                    any(prefix in cls for prefix in chrome_prefixes)
                    for cls in classes
                ):
                    victims.append(tag)
            for tag in victims:
                if tag.parent is not None:
                    tag.decompose()
            return body.decode_contents()

        # Page 1 — use the soup we already parsed
        page_soups = [soup]
        for p in range(2, num_pages + 1):
            if story.chapters:
                self._delay()
            page_html = self._fetch_page(slug, p)
            page_soups.append(BeautifulSoup(page_html, "lxml"))

        for i, page_soup in enumerate(page_soups, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            cached = self._load_chapter_cache(story_id, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_pages, cached.title, True)
                continue
            html_body = extract_body(page_soup)
            title = f"Page {i}" if num_pages > 1 else meta["title"]
            ch = Chapter(number=i, title=title, html=html_body)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_pages, title, False)

        return story
