"""Dark Wanderer (darkwanderer.net) scraper.

Dark Wanderer is a XenForo-based cuckold community. Stories are
forum threads at ``/threads/<slug>.<tid>/`` — the original post is
usually chapter 1, and follow-up posts by the same author serve as
subsequent chapters. Replies by other members are interspersed and
generally not part of the story; we keep only the posts by the
thread starter to keep the output on-topic.

XenForo paginates long threads; the page links are ``/threads/<slug>.<tid>/page-N``.
We walk every page, collecting thread-starter posts in order, and
treat each one as a chapter.

Tagging the cuckold kink with a dedicated site gives it the same
two-archive footing as TG (Fictionmania + TGStorytime) and adds a
community-specific voice — the stories on Dark Wanderer are written
in a different register than the tagged Lushstories-and-SOL kind.
"""

import logging
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

DW_BASE = "https://darkwanderer.net"

DW_THREAD_URL_RE = re.compile(
    r"^https?://(?:www\.)?darkwanderer\.net/threads/(?P<slug>[^/.]+)\.(?P<tid>\d+)",
    re.I,
)


class DarkWandererScraper(BaseScraper):
    """Scraper for darkwanderer.net forum threads treated as stories."""

    site_name = "darkwanderer"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the numeric thread id as an int."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = DW_THREAD_URL_RE.search(text)
        if m:
            return int(m.group("tid"))
        raise ValueError(
            f"Cannot parse Dark Wanderer thread id from: {text!r}\n"
            "Expected e.g. https://darkwanderer.net/threads/my-story.12345/"
        )

    @staticmethod
    def _thread_url(tid: int, slug: str = "") -> str:
        if slug:
            return f"{DW_BASE}/threads/{slug}.{tid}/"
        return f"{DW_BASE}/threads/{tid}/"

    @staticmethod
    def _parse_metadata(soup, tid: int, slug: str) -> dict:
        title = ""
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            # "Title | Darkwanderer - Cuckold forums" — strip the suffix.
            raw = og_title["content"]
            title = re.sub(r"\s*\|\s*Darkwanderer.*$", "", raw).strip()
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                raw = title_tag.get_text(" ", strip=True)
                title = re.sub(r"\s*\|\s*Darkwanderer.*$", "", raw).strip()
        if not title:
            title = slug.replace("-", " ").title() or f"Thread {tid}"

        author = "Unknown Author"
        author_url = ""
        starter_link = soup.find(
            "a", class_=re.compile(r"username"),
            href=re.compile(r"^/members/"),
        )
        if starter_link:
            author = starter_link.get_text(strip=True) or author
            href = starter_link.get("href", "")
            if href:
                author_url = urljoin(DW_BASE, href)

        summary = ""
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            summary = og_desc["content"].strip()

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": {"thread_id": tid, "slug": slug},
        }

    @staticmethod
    def _page_count(soup) -> int:
        """XenForo paginates with ``<nav class="pageNav">``; last link
        is the final page."""
        nav = soup.find("nav", class_="pageNav")
        if not nav:
            return 1
        max_page = 1
        for a in nav.find_all("a", href=True):
            m = re.search(r"page-(\d+)", a["href"])
            if m:
                max_page = max(max_page, int(m.group(1)))
        return max_page

    @staticmethod
    def _thread_starter_username(soup) -> str:
        """Name to compare each post's author against so we can keep
        only the starter's posts (= the story chapters)."""
        # XenForo's thread_view template exposes the starter via
        # <a class="username" ...> in the post-starter block. Fallback
        # to the first post's username.
        first_post_user = soup.find(
            "a", href=re.compile(r"^/members/"), class_=re.compile(r"username"),
        )
        return first_post_user.get_text(strip=True) if first_post_user else ""

    @staticmethod
    def _starter_posts(soup, starter: str) -> list[str]:
        """Return ``[html, ...]`` for each post authored by ``starter``.

        We look for ``<article class="message">`` blocks and match on
        the ``data-author`` attribute or the nested username link."""
        htmls = []
        for article in soup.find_all("article", class_=re.compile(r"\bmessage\b")):
            author_attr = (article.get("data-author") or "").strip()
            if starter and author_attr and author_attr != starter:
                continue
            if not starter and not author_attr:
                continue
            body = article.find("div", class_=re.compile(r"bbWrapper"))
            if body is None:
                body = article.find("div", class_=re.compile(r"message-content"))
            if body is None:
                continue
            # Drop quoted blocks so we don't include replies-within-a-quote.
            for quote in body.select("blockquote, div.bbCodeBlock"):
                quote.decompose()
            htmls.append(body.decode_contents())
        return htmls

    def get_chapter_count(self, url_or_id):
        tid = self.parse_story_id(url_or_id)
        slug = ""
        m = DW_THREAD_URL_RE.search(str(url_or_id))
        if m:
            slug = m.group("slug")
        html = self._fetch(self._thread_url(tid, slug))
        soup = BeautifulSoup(html, "lxml")
        starter = self._thread_starter_username(soup)
        total_pages = self._page_count(soup)
        # First page already fetched — count its starter posts, then
        # page through the rest. This does N HTTP calls for an N-page
        # thread, which is the same cost as downloading anyway.
        count = len(self._starter_posts(soup, starter))
        for page in range(2, total_pages + 1):
            self._delay()
            page_html = self._fetch(
                self._thread_url(tid, slug) + f"page-{page}"
            )
            page_soup = BeautifulSoup(page_html, "lxml")
            count += len(self._starter_posts(page_soup, starter))
        return max(count, 1)

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        tid = self.parse_story_id(url_or_id)
        slug = ""
        m = DW_THREAD_URL_RE.search(str(url_or_id))
        if m:
            slug = m.group("slug")
        thread_url = self._thread_url(tid, slug)

        logger.info("Fetching Dark Wanderer thread %s...", tid)
        first_html = self._fetch(thread_url)
        first_soup = BeautifulSoup(first_html, "lxml")

        meta = self._parse_metadata(first_soup, tid, slug)
        starter = self._thread_starter_username(first_soup)
        total_pages = self._page_count(first_soup)

        all_posts: list[str] = list(self._starter_posts(first_soup, starter))
        for page in range(2, total_pages + 1):
            self._delay()
            page_html = self._fetch(thread_url + f"page-{page}")
            page_soup = BeautifulSoup(page_html, "lxml")
            all_posts.extend(self._starter_posts(page_soup, starter))

        if not all_posts:
            raise ValueError(
                f"No starter posts found in thread {tid}; "
                "the thread may be empty or use a non-default layout."
            )

        num_chapters = len(all_posts)
        meta["num_chapters"] = num_chapters
        meta["chapter_titles"] = {
            str(i + 1): f"Post {i + 1}" for i in range(num_chapters)
        }
        self._save_meta_cache(tid, meta)

        story = Story(
            id=tid,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=thread_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        for i, post_html in enumerate(all_posts, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            cached = self._load_chapter_cache(tid, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue
            title = f"Post {i}"
            ch = Chapter(number=i, title=title, html=post_html)
            self._save_chapter_cache(tid, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, title, False)

        return story
