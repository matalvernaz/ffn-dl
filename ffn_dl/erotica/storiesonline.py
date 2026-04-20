"""StoriesOnline (storiesonline.net, "SOL") scraper.

SOL's strength is its tag system — every story carries a rich set of
codes (Ma/Fa, cons, mc, incest, school, school, etc.) and the site
exposes tag browses at ``/stories/bytag/<tag>`` (colon-join for AND).
Our unified erotica search hits those tag URLs directly; this module
handles individual story download.

Story URLs: ``/s/<numeric_id>/<slug>``. Multi-chapter works expose a
``<select>`` (or a chapter TOC list) with chapter URLs shaped
``/s/<id>/<n>``. Single-chapter works embed the whole body in one page.

Free users see a ``<div class="paywall">`` block in place of premium
content; we extract whatever precedes it too. The Advanced Search form
at ``/library/searchf.php`` is paywalled, so our search layer uses the
free tag-browse instead.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

SOL_BASE = "https://storiesonline.net"

SOL_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?storiesonline\.net/s/(?P<id>\d+)(?:/(?P<slug>[^/?#\s]+))?",
    re.I,
)

SOL_AUTHOR_URL_RE = re.compile(
    r"^https?://(?:www\.)?storiesonline\.net/a/(?P<slug>[^/?#\s]+)", re.I,
)

SOL_TAG_URL_RE = re.compile(
    r"^https?://(?:www\.)?storiesonline\.net/stories/bytag/(?P<tags>[^/?#\s]+)", re.I,
)


class StoriesOnlineScraper(BaseScraper):
    """Scraper for storiesonline.net stories."""

    site_name = "storiesonline"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the numeric SOL story id as an int."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = SOL_STORY_URL_RE.search(text)
        if m:
            return int(m.group("id"))
        raise ValueError(
            f"Cannot parse SOL story id from: {text!r}\n"
            "Expected a URL like https://storiesonline.net/s/40467/slug "
            "or a bare numeric id."
        )

    @staticmethod
    def is_author_url(url):
        return bool(SOL_AUTHOR_URL_RE.search(str(url)))

    @staticmethod
    def is_series_url(url):
        # SOL's "universes" (/univ/) and "series" (/ser/) both behave
        # like AO3 series — a listing of related stories. Treat both
        # as series URLs.
        text = str(url)
        return "/storiesonline.net/univ/" in text or "/storiesonline.net/ser/" in text

    @staticmethod
    def _story_url(story_id: int, slug: str = "") -> str:
        tail = f"/{slug}" if slug else ""
        return f"{SOL_BASE}/s/{story_id}{tail}"

    @staticmethod
    def _chapter_url(story_id: int, slug: str, chap: int) -> str:
        # SOL chapter pagination: "/s/<id>/<n>" when there's no slug
        # on the URL, or "/s/<id>/<slug>:<n>" variants in older builds.
        # The plain numeric form always works — we use it.
        if chap <= 1:
            return StoriesOnlineScraper._story_url(story_id, slug)
        return f"{SOL_BASE}/s/{story_id}/{chap}"

    @staticmethod
    def _is_multi_chapter(html: str) -> bool:
        """Return True when the story has more than one chapter.

        SOL embeds ``var ismult=1;`` in inline JS on multi-chapter
        works and ``ismult=0`` on single-chapter ones — a stable flag
        the site uses for its own scroller, so it's safe to lean on.
        """
        m = re.search(r"var\s+ismult\s*=\s*(\d+)", html)
        return bool(m and int(m.group(1)) == 1)

    @staticmethod
    def _chapter_links(soup, story_id: int) -> list[tuple[int, str]]:
        """Return sorted ``[(n, title), ...]`` for every chapter link
        on the page.

        SOL builds a chapter list in the sidebar / TOC with hrefs
        like ``/s/<id>/2``, ``/s/<id>/3``. We pick every such link
        whose target matches this story.
        """
        chapters: dict[int, str] = {}
        chap_href_re = re.compile(rf"^/s/{story_id}/(\d+)(?:[/?#]|$)")
        for a in soup.find_all("a", href=chap_href_re):
            m = chap_href_re.match(a.get("href", ""))
            if not m:
                continue
            n = int(m.group(1))
            if n < 1 or n > 10000:
                continue
            title = a.get_text(" ", strip=True) or f"Chapter {n}"
            chapters.setdefault(n, title)
        return sorted(chapters.items())

    @staticmethod
    def _parse_metadata(soup, html: str, story_id: int, slug: str) -> dict:
        title_tag = soup.find("h1", id="s-title")
        title = title_tag.get_text(strip=True) if title_tag else (
            slug.replace("-", " ").title() if slug else f"SOL story {story_id}"
        )

        author = "Unknown Author"
        author_url = ""
        auth_tag = soup.find("h2", id="s-auth")
        if auth_tag:
            raw = auth_tag.get_text(" ", strip=True)
            author = re.sub(r"^by\s+", "", raw, flags=re.I).strip() or author
        for link in soup.find_all("link", rel="author"):
            href = link.get("href") or ""
            if href:
                author_url = href if href.startswith("http") else SOL_BASE + href
                break
        if not author_url:
            a = soup.find("a", attrs={"rel": "author"})
            if a and a.get("href"):
                href = a["href"]
                author_url = href if href.startswith("http") else SOL_BASE + href

        summary = ""
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            summary = og_desc["content"].strip()
        if not summary:
            notice = soup.find("div", class_="notice")
            if notice:
                first_p = notice.find("p")
                if first_p:
                    summary = first_p.get_text(" ", strip=True)

        tags = [
            m.get("content", "").strip()
            for m in soup.find_all("meta", attrs={"property": "article:tag"})
            if m.get("content")
        ]

        section = ""
        section_meta = soup.find("meta", attrs={"property": "article:section"})
        if section_meta and section_meta.get("content"):
            section = section_meta["content"].strip()

        if StoriesOnlineScraper._is_multi_chapter(html):
            chap_list = StoriesOnlineScraper._chapter_links(soup, story_id)
            if chap_list:
                num_chapters = chap_list[-1][0]
                chapter_titles = {str(n): t for n, t in chap_list}
                # The link list usually omits chapter 1; add a placeholder.
                chapter_titles.setdefault("1", "Chapter 1")
            else:
                num_chapters = 1
                chapter_titles = {"1": title}
        else:
            num_chapters = 1
            chapter_titles = {"1": title}

        extra = {
            "slug": slug,
            "tags": tags,
            "section": section,
        }
        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        """Extract the prose body from a SOL page.

        Prose lives inside ``<article>`` → one or more ``<p>``. We
        serialize the article contents minus the header/notice block
        so the EPUB carries just the story text. Paywall blocks
        (``<div class="paywall">``) contain visible tease content —
        we keep them so the reader sees whatever SOL shows to
        anonymous users.
        """
        article = soup.find("article")
        if article is None:
            raise ValueError("Could not find SOL <article> on page.")
        # Drop the story header/notice (cover + caution tags); the
        # reader already has that metadata separately.
        for selector in ("header", "div.notice", "nav", "div.breadcrumb"):
            for el in article.select(selector):
                el.decompose()
        return article.decode_contents()

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        html = self._fetch(self._story_url(story_id))
        if not self._is_multi_chapter(html):
            return 1
        soup = BeautifulSoup(html, "lxml")
        chap_list = self._chapter_links(soup, story_id)
        return chap_list[-1][0] if chap_list else 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        story_id = self.parse_story_id(url_or_id)
        m = SOL_STORY_URL_RE.search(str(url_or_id))
        slug = m.group("slug") if (m and m.group("slug")) else ""
        story_url = self._story_url(story_id, slug)

        logger.info("Fetching SOL story %s (%s)...", story_id, slug or "no-slug")
        page1_html = self._fetch(story_url)
        soup = BeautifulSoup(page1_html, "lxml")

        meta = self._parse_metadata(soup, page1_html, story_id, slug)
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]
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

        if skip_chapters >= num_chapters:
            return story

        if skip_chapters < 1 and chapter_in_spec(1, chapters):
            html = self._parse_chapter_html(soup)
            ch1_title = chapter_titles.get("1", "Chapter 1")
            ch1 = Chapter(number=1, title=ch1_title, html=html)
            self._save_chapter_cache(story_id, ch1)
            story.chapters.append(ch1)
            if progress_callback:
                progress_callback(1, num_chapters, ch1_title, False)

        for chap_num in range(max(2, skip_chapters + 1), num_chapters + 1):
            if not chapter_in_spec(chap_num, chapters):
                continue
            ch_title = chapter_titles.get(str(chap_num), f"Chapter {chap_num}")

            cached = self._load_chapter_cache(story_id, chap_num)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(chap_num, num_chapters, cached.title, True)
                continue

            self._delay()
            url = self._chapter_url(story_id, slug, chap_num)
            logger.debug("Fetching SOL chapter %d/%d", chap_num, num_chapters)
            page = self._fetch(url)
            ch_soup = BeautifulSoup(page, "lxml")
            html = self._parse_chapter_html(ch_soup)

            ch = Chapter(number=chap_num, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(chap_num, num_chapters, ch_title, False)

        return story
