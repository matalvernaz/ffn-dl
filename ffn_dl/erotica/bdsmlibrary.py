"""BDSM Library (bdsmlibrary.com) scraper.

BDSM Library has been running since the early 2000s as a community
archive for BDSM, femdom, foot-fetish, and related fiction. The site
speaks plain HTTP only — HTTPS serves an expired certificate that
curl_cffi won't accept by default, so we stick to ``http://``.

URL shapes:

* Story (chapter index): ``/stories/story.php?storyid=<N>``
* Chapter: ``/stories/chapter.php?storyid=<N>&chapterid=<M>``
* Author: ``/stories/author.php?authorid=<N>``

The story page is a thin landing — title, author, story codes
(BDSM Library's tag system: ``F/m``, ``feet``, ``BDSM``, ``D/s``, etc.),
synopsis, and a list of chapter links. The chapter pages carry the
prose inside a single ``<div class="storyblock">``.

Chapter numbering is implicit in the listing order on story.php; we
preserve that order rather than relying on title parsing (some
chapters are named ``Part 1`` / ``Chapter 3`` / ``Epilogue`` / etc.).
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

BDSMLIB_BASE = "http://www.bdsmlibrary.com"

BDSMLIB_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?bdsmlibrary\.com/stories/story\.php\?storyid=(\d+)",
    re.I,
)

BDSMLIB_CHAPTER_URL_RE = re.compile(
    r"^https?://(?:www\.)?bdsmlibrary\.com/stories/chapter\.php"
    r"\?storyid=(\d+)&chapterid=(\d+)",
    re.I,
)

BDSMLIB_AUTHOR_URL_RE = re.compile(
    r"^https?://(?:www\.)?bdsmlibrary\.com/stories/author\.php\?authorid=\d+",
    re.I,
)


class BDSMLibraryScraper(BaseScraper):
    """Scraper for bdsmlibrary.com stories."""

    site_name = "bdsmlibrary"

    # BDSM Library sends ``Content-Type: text/html; charset=UTF-8``
    # but the actual bytes are Windows-1252 (the RTF-to-HTML converter
    # the site uses preserves the original 8-bit smart quotes / dashes
    # without re-encoding). Trusting the header decodes apostrophes
    # and curly quotes as U+FFFD; pinning cp1252 keeps the prose
    # readable on disk.
    response_encoding = "cp1252"

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = BDSMLIB_STORY_URL_RE.search(text)
        if m:
            return int(m.group(1))
        m = BDSMLIB_CHAPTER_URL_RE.search(text)
        if m:
            return int(m.group(1))
        raise ValueError(
            f"Cannot parse BDSM Library story id from: {text!r}\n"
            "Expected e.g. http://www.bdsmlibrary.com/stories/story.php?storyid=10994 "
            "or a bare numeric id."
        )

    @staticmethod
    def is_author_url(url):
        return bool(BDSMLIB_AUTHOR_URL_RE.search(str(url)))

    @staticmethod
    def _story_url(story_id: int) -> str:
        return f"{BDSMLIB_BASE}/stories/story.php?storyid={story_id}"

    @staticmethod
    def _chapter_url(story_id: int, chapter_id: int) -> str:
        return (
            f"{BDSMLIB_BASE}/stories/chapter.php"
            f"?storyid={story_id}&chapterid={chapter_id}"
        )

    @staticmethod
    def _chapter_links(soup, story_id: int) -> list[tuple[int, int, str]]:
        """Return ``[(chapter_index, chapter_id, title), ...]`` in
        document order. BDSM Library's story.php renders the chapter
        list as anchors pointing to ``chapter.php?storyid=N&chapterid=M``;
        each anchor's text is the chapter title.

        ``chapter_index`` is the 1-based position in the chapter list,
        which is what we use for caching and progress reporting.
        ``chapter_id`` is the site's internal chapter ID required to
        construct the chapter URL.
        """
        out: list[tuple[int, int, str]] = []
        seen: set[int] = set()
        for a in soup.find_all("a", href=re.compile(
            rf"chapter\.php\?storyid={story_id}&chapterid=\d+", re.I,
        )):
            m = re.search(r"chapterid=(\d+)", a.get("href", ""))
            if not m:
                continue
            cid = int(m.group(1))
            if cid in seen:
                continue
            seen.add(cid)
            title = a.get_text(" ", strip=True) or f"Chapter {len(out) + 1}"
            out.append((len(out) + 1, cid, title))
        return out

    @staticmethod
    def _parse_metadata(soup, story_id: int) -> dict:
        """Pull title/author/synopsis/chapter-list from a story.php page."""
        title = f"BDSM Library story {story_id}"
        # Title appears as a bolded anchor at the top of the listing
        # (``<b><a href="story.php?storyid=N">Title</a></b>``); the
        # <title> tag is also reliable.
        title_tag = soup.find("title")
        if title_tag:
            raw = re.sub(r"\s+", " ", title_tag.get_text(" ", strip=True))
            # "BDSM Library - Story: <Title>"
            m = re.match(r"^\s*BDSM Library\s*[-–]\s*Story\s*:\s*(.+)$", raw, re.I)
            if m:
                title = m.group(1).strip()

        author = "Anonymous"
        author_url = ""
        author_a = soup.find(
            "a", href=re.compile(r"author\.php\?authorid=\d+", re.I),
        )
        if author_a:
            author = author_a.get_text(" ", strip=True) or author
            href = author_a.get("href", "")
            if href:
                author_url = (
                    href if href.startswith("http")
                    else BDSMLIB_BASE + ("/" if not href.startswith("/") else "")
                    + href.lstrip("/")
                )

        # Story codes (tags) live in italic anchor runs labelled
        # "Story Codes:" on the page. Capture them for the EPUB
        # metadata header.
        codes: list[str] = []
        for a in soup.find_all(
            "a", href=re.compile(r"search\.php\?selectedcode", re.I),
        ):
            text = a.get_text(" ", strip=True)
            if text and text not in codes:
                codes.append(text)

        # Synopsis appears in italic text above the chapter table;
        # there's no class hook, so look for the first <i> with prose
        # length > 60 chars.
        summary = ""
        for italic in soup.find_all(["i", "em"]):
            text = italic.get_text(" ", strip=True)
            if len(text) >= 60:
                summary = text
                break

        chap_list = BDSMLibraryScraper._chapter_links(soup, story_id)
        if chap_list:
            num_chapters = len(chap_list)
            chapter_titles = {str(idx): t for idx, _cid, t in chap_list}
            chapter_ids = {str(idx): cid for idx, cid, _t in chap_list}
        else:
            num_chapters = 1
            chapter_titles = {"1": title}
            chapter_ids = {}

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "extra": {
                "tags": codes,
                "chapter_ids": chapter_ids,
            },
        }

    @staticmethod
    def _parse_chapter_html(page_html: str) -> str:
        """Extract the prose from a chapter.php page.

        BDSM Library wraps the body in ``<div class="storyblock">``,
        which itself contains an embedded mini-document
        (``<html><head>...</head><body>...</body></html>``) — leftovers
        from the site's RTF-to-HTML converter.

        ``lxml`` strips nested ``<html>``/``<body>`` tags during parse,
        which would leave us with the DOCTYPE / ``<title>`` / ``<style>``
        chrome inlined into the EPUB. We re-parse the page with
        ``html.parser`` (which preserves the nested document) so the
        inner ``<body>`` is reachable, then pull its decoded contents.
        Takes the raw HTML string rather than a pre-built soup so the
        caller can't accidentally pass us an lxml-flattened tree.
        """
        soup = BeautifulSoup(page_html, "html.parser")
        block = soup.find("div", class_="storyblock")
        if block is None:
            raise ValueError("Could not find BDSM Library storyblock.")
        inner = block.find("body")
        if inner is not None:
            return inner.decode_contents()
        # Fallback if the converter ever stops wrapping in <html><body>.
        return block.decode_contents()

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        html = self._fetch(self._story_url(story_id))
        soup = BeautifulSoup(html, "lxml")
        chap_list = self._chapter_links(soup, story_id)
        return len(chap_list) if chap_list else 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        story_id = self.parse_story_id(url_or_id)
        story_url = self._story_url(story_id)

        logger.info("Fetching BDSM Library story %s...", story_id)
        page_html = self._fetch(story_url)
        soup = BeautifulSoup(page_html, "lxml")

        meta = self._parse_metadata(soup, story_id)
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]
        chapter_ids = meta["extra"].get("chapter_ids", {})
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

        for chap_num in range(max(1, skip_chapters + 1), num_chapters + 1):
            if not chapter_in_spec(chap_num, chapters):
                continue
            ch_title = chapter_titles.get(str(chap_num), f"Chapter {chap_num}")

            cached = self._load_chapter_cache(story_id, chap_num)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(chap_num, num_chapters, cached.title, True)
                continue

            cid = chapter_ids.get(str(chap_num))
            if cid is None:
                # Single-chapter story with no chapter list — the
                # story.php page itself doesn't carry prose, but a
                # rare single-chapter shape has a chapterid linkable
                # from the listing. Skip if we don't have an id.
                logger.warning(
                    "BDSM Library story %s has no chapter %d link",
                    story_id, chap_num,
                )
                continue

            self._delay()
            url = self._chapter_url(story_id, int(cid))
            logger.debug("Fetching BDSM Library chapter %d/%d", chap_num, num_chapters)
            page = self._fetch(url)
            html = self._parse_chapter_html(page)

            ch = Chapter(number=chap_num, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(chap_num, num_chapters, ch_title, False)

        return story
