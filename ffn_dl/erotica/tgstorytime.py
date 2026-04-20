"""TGStorytime (tgstorytime.com) scraper.

TGStorytime is a dedicated transgender/crossdressing archive — peer to
Fictionmania. The site is eFiction-based: numeric story ids at
``viewstory.php?sid=<N>`` with ``&chapter=<n>`` for multi-chapter
works and ``&ageconsent=ok&warning=3`` to bypass the adult-content
interstitial that blocks anonymous access.

A chapter ``<select class="textbox" name="chapter">`` drives
navigation, with options like ``<option value='N'>N. Title</option>``
— our cheap chapter-count probe reads this and the chapter titles
land in the Story metadata.

Having TGStorytime alongside Fictionmania gives TG fiction the same
"two large archives" redundancy that gay erotica gets from Nifty +
Literotica — a reader whose story isn't on one can try the other.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

TGS_BASE = "https://www.tgstorytime.com"

TGS_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?tgstorytime\.com/viewstory\.php\?sid=(\d+)", re.I,
)

AGE_CONSENT_QS = "ageconsent=ok&warning=3&textsize=0"
"""Appended to every request so the adult-content interstitial
doesn't gate the response. ``warning=3`` covers explicit content
(level 3); stories tagged deviant use level 4 but 3 passes both."""


class TGStorytimeScraper(BaseScraper):
    """Scraper for tgstorytime.com stories."""

    site_name = "tgstorytime"

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = TGS_STORY_URL_RE.search(text)
        if m:
            return int(m.group(1))
        raise ValueError(
            f"Cannot parse TGStorytime story id from: {text!r}\n"
            "Expected e.g. https://www.tgstorytime.com/viewstory.php?sid=9219"
        )

    @staticmethod
    def _story_url(sid: int, chapter: int = 0) -> str:
        base = f"{TGS_BASE}/viewstory.php?sid={sid}&{AGE_CONSENT_QS}"
        if chapter > 1:
            base += f"&chapter={chapter}"
        return base

    @staticmethod
    def _parse_chapter_options(soup) -> list[tuple[int, str]]:
        select = soup.find("select", attrs={"name": "chapter"})
        if not select:
            return []
        options = []
        for opt in select.find_all("option"):
            value = opt.get("value", "").strip()
            if not value.isdigit():
                continue
            label = opt.get_text(" ", strip=True)
            cleaned = re.sub(r"^\d+\.\s*", "", label).strip() or f"Chapter {value}"
            options.append((int(value), cleaned))
        options.sort(key=lambda t: t[0])
        return options

    @staticmethod
    def _parse_metadata(soup, sid: int) -> dict:
        title = ""
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(" ", strip=True)
            # TGStorytime titles: "Story Title by Author"
            m = re.match(r"^(.*?)\s+by\s+.*$", raw, flags=re.I)
            title = m.group(1).strip() if m else raw

        author = "Unknown Author"
        author_url = ""
        author_link = soup.find("a", href=re.compile(r"viewuser\.php\?uid=\d+"))
        if author_link:
            author = author_link.get_text(strip=True) or author
            href = author_link.get("href", "")
            author_url = (
                href if href.startswith("http")
                else f"{TGS_BASE}/{href.lstrip('/')}"
            )

        summary = ""
        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            summary = desc["content"].strip()

        chapter_options = TGStorytimeScraper._parse_chapter_options(soup)
        num_chapters = len(chapter_options) if chapter_options else 1
        chapter_titles = (
            {str(n): t for n, t in chapter_options}
            if chapter_options else {"1": title or "Chapter 1"}
        )

        return {
            "title": title or f"TGStorytime {sid}",
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "extra": {"sid": sid},
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        story_div = soup.find("div", id="story")
        if story_div:
            return story_div.decode_contents()
        # Older skins drop the prose into the main content div without
        # an id — fall back to a broad marker.
        content = soup.find("div", id=re.compile(r"^(story|content|main)"))
        if content:
            return content.decode_contents()
        raise ValueError("Could not find TGStorytime story body.")

    def get_chapter_count(self, url_or_id):
        sid = self.parse_story_id(url_or_id)
        html = self._fetch(self._story_url(sid))
        soup = BeautifulSoup(html, "lxml")
        options = self._parse_chapter_options(soup)
        return len(options) if options else 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        sid = self.parse_story_id(url_or_id)
        story_url = self._story_url(sid)

        logger.info("Fetching TGStorytime story %s...", sid)
        page = self._fetch(story_url)
        soup = BeautifulSoup(page, "lxml")

        meta = self._parse_metadata(soup, sid)
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]
        self._save_meta_cache(sid, meta)

        story = Story(
            id=sid,
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
            self._save_chapter_cache(sid, ch1)
            story.chapters.append(ch1)
            if progress_callback:
                progress_callback(1, num_chapters, ch1_title, False)

        for chap_num in range(max(2, skip_chapters + 1), num_chapters + 1):
            if not chapter_in_spec(chap_num, chapters):
                continue
            ch_title = chapter_titles.get(str(chap_num), f"Chapter {chap_num}")

            cached = self._load_chapter_cache(sid, chap_num)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(chap_num, num_chapters, cached.title, True)
                continue

            self._delay()
            url = self._story_url(sid, chap_num)
            logger.debug("Fetching TGStorytime chapter %d/%d", chap_num, num_chapters)
            page = self._fetch(url)
            ch_soup = BeautifulSoup(page, "lxml")
            html = self._parse_chapter_html(ch_soup)

            ch = Chapter(number=chap_num, title=ch_title, html=html)
            self._save_chapter_cache(sid, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(chap_num, num_chapters, ch_title, False)

        return story
