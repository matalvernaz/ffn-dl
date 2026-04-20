"""Adult-FanFiction.org (AFF) scraper.

AFF is a subdomain-per-fandom archive: ``hp.adult-fanfiction.org``,
``naruto.adult-fanfiction.org``, ``buffy.adult-fanfiction.org``, etc.
Each story lives at ``story.php?no=<NNNNNNNNN>`` on its fandom
subdomain; chapters are siblings at the same URL with an added
``&chapter=<N>`` parameter.

Two parts of the page are load-bearing for us:

* ``<select class="chapter-select">`` with one ``<option value="N">``
  per chapter — our cheap chapter-count probe reads this.
* ``<div class="chapter-content-card">`` holds ``<h2 class="chapter-title">``
  and ``<div class="chapter-body">`` — the chapter body is lifted from
  ``.chapter-body``.

Story ids are 9-digit integers. The leading digits encode the fandom in
AFF's internal tooling but we treat them as opaque — the only contract
is "same id + same fandom subdomain = same story".
"""

import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

AFF_ROOT_DOMAIN = "adult-fanfiction.org"

AFF_DEFAULT_SUBDOMAIN = "hp"
"""Used only when parsing a bare story id with no URL context — AFF's
URL space requires a subdomain, so we have to pick one. The story pages
cross-link, so even a "wrong" subdomain redirects to the right one for
most ids; HP is the largest archive, so it's the safest default."""

AFF_URL_RE = re.compile(
    r"^https?://(?P<sub>[a-z0-9-]+)\.adult-fanfiction\.org/story\.php\?no=(?P<id>\d+)",
    re.I,
)
"""Canonical story-URL regex. AFF uses ``story.php?no=<id>`` uniformly;
everything else (chapter, reviews, etc.) is query-string variation off
the same path."""


class AFFScraper(BaseScraper):
    """Scraper for Adult-FanFiction.org stories."""

    site_name = "aff"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the numeric AFF story id as an int.

        Accepts full ``story.php?no=N`` URLs (any subdomain) or a bare
        numeric id. The subdomain is *not* part of the id — call
        :meth:`parse_subdomain` if you need it.
        """
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = AFF_URL_RE.search(text)
        if m:
            return int(m.group("id"))
        raise ValueError(
            f"Cannot parse AFF story id from: {text!r}\n"
            "Expected a URL like https://hp.adult-fanfiction.org/story.php?no=600100488 "
            "or a bare numeric id."
        )

    @staticmethod
    def parse_subdomain(url_or_id) -> str:
        """Return the fandom subdomain for a full AFF URL, or the
        default when only a bare id was supplied.

        The download path *must* know the subdomain — there's no
        ``www.adult-fanfiction.org/story.php?no=...`` route, each
        story only resolves on its home subdomain.
        """
        text = str(url_or_id).strip()
        m = AFF_URL_RE.search(text)
        if m:
            return m.group("sub").lower()
        parts = urlsplit(text if "://" in text else f"https://{text}")
        host = (parts.hostname or "").lower()
        if host.endswith(AFF_ROOT_DOMAIN) and host != AFF_ROOT_DOMAIN:
            sub = host[: -(len(AFF_ROOT_DOMAIN) + 1)]
            if sub and sub != "www":
                return sub
        return AFF_DEFAULT_SUBDOMAIN

    @staticmethod
    def _story_url(sub: str, story_id: int, chapter: int = 1) -> str:
        base = f"https://{sub}.adult-fanfiction.org/story.php?no={story_id}"
        if chapter > 1:
            base += f"&chapter={chapter}"
        return base

    @staticmethod
    def _chapter_options(soup) -> list[tuple[int, str]]:
        """Return ``[(n, title), ...]`` from ``<select class="chapter-select">``.

        Single-chapter works render no ``<select>``; the caller treats
        the absence as ``num_chapters == 1``.
        """
        select = soup.find("select", class_="chapter-select")
        if not select:
            return []
        out = []
        for opt in select.find_all("option"):
            value = opt.get("value", "").strip()
            if not value.isdigit():
                continue
            label = opt.get_text(" ", strip=True)
            cleaned = re.sub(r"^Chapter\s*\d+\s*-?\s*", "", label, flags=re.I)
            cleaned = cleaned.strip() or f"Chapter {value}"
            out.append((int(value), cleaned))
        out.sort(key=lambda t: t[0])
        return out

    @staticmethod
    def _parse_metadata(soup, sub: str, story_id: int) -> dict:
        title_tag = soup.find("title")
        raw_title = title_tag.get_text(strip=True) if title_tag else ""
        # Titles look like "Reflections. - Harry Potter - AFF Fiction Portal".
        title = raw_title
        for suffix in (" - AFF Fiction Portal",):
            if title.endswith(suffix):
                title = title[: -len(suffix)]
        fandom = ""
        if " - " in title:
            head, _, tail = title.rpartition(" - ")
            title, fandom = head, tail

        author = "Unknown Author"
        author_url = ""
        author_link = soup.find(
            "a", href=re.compile(r"authorlinks?\.php\?no=\d+", re.I),
        )
        if author_link:
            author = author_link.get_text(strip=True) or author
            href = author_link.get("href", "")
            if href:
                author_url = (
                    href if href.startswith("http")
                    else f"https://{sub}.adult-fanfiction.org/{href.lstrip('/')}"
                )

        summary = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            summary = meta_desc["content"].strip()

        chapter_options = AFFScraper._chapter_options(soup)
        num_chapters = len(chapter_options) if chapter_options else 1
        chapter_titles = (
            {str(n): t for n, t in chapter_options}
            if chapter_options else {"1": title or "Chapter 1"}
        )

        extra = {
            "subdomain": sub,
            "fandom": fandom,
        }
        return {
            "title": title or f"AFF story {story_id}",
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        body = soup.find("div", class_="chapter-body")
        if body is not None:
            return body.decode_contents()
        # Older AFF pages wrap body in chapter-content-card; fall back
        # so we don't lose content if the site partially updates markup.
        card = soup.find("div", class_="chapter-content-card")
        if card is not None:
            return card.decode_contents()
        raise ValueError("Could not find AFF chapter body on page.")

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        sub = self.parse_subdomain(url_or_id)
        html = self._fetch(self._story_url(sub, story_id))
        soup = BeautifulSoup(html, "lxml")
        options = self._chapter_options(soup)
        return len(options) if options else 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        story_id = self.parse_story_id(url_or_id)
        sub = self.parse_subdomain(url_or_id)
        story_url = self._story_url(sub, story_id)

        logger.info("Fetching AFF story %s (%s)...", story_id, sub)
        page = self._fetch(story_url)
        soup = BeautifulSoup(page, "lxml")

        meta = self._parse_metadata(soup, sub, story_id)
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
            url = self._story_url(sub, story_id, chap_num)
            logger.debug("Fetching AFF chapter %d/%d", chap_num, num_chapters)
            page = self._fetch(url)
            ch_soup = BeautifulSoup(page, "lxml")
            html = self._parse_chapter_html(ch_soup)

            ch = Chapter(number=chap_num, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(chap_num, num_chapters, ch_title, False)

        return story
