"""GreatFeet (greatfeet.com) scraper — dedicated foot fetish archive.

GreatFeet has been running continuously since 1997 as a foot-fetish
photo + story archive. Individual stories live at
``/stories/ts<N>.htm`` where ``N`` is a sequential integer (1735+ as
of April 2026). Each URL is a single page of prose — no pagination,
no chapters — so every download is a one-chapter story.

Story metadata is sparse. Authors are almost always anonymous, tags
aren't used (the whole archive IS the "feet" tag), and submission
dates live in an ``<i>``-wrapped "Published on <date>" block rather
than a proper ``<meta>`` tag. We lift what we can from the ``<title>``
element and the first-level heading.

Adding a dedicated feet archive alongside the generalist Literotica /
Lushstories / MCStories / SOL coverage gives the feet kink the same
"one-click tag search → actual dedicated community" path that MC
gets from MCStories and TG gets from Fictionmania.
"""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

GF_BASE = "https://www.greatfeet.com"

GF_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?greatfeet\.com/stories/ts(\d+)\.htm", re.I,
)


class GreatFeetScraper(BaseScraper):
    """Scraper for greatfeet.com foot fetish stories."""

    site_name = "greatfeet"

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        m = GF_STORY_URL_RE.search(text)
        if m:
            return int(m.group(1))
        raise ValueError(
            f"Cannot parse GreatFeet story id from: {text!r}\n"
            "Expected e.g. https://www.greatfeet.com/stories/ts1735.htm "
            "or a bare numeric id."
        )

    @staticmethod
    def _story_url(story_id: int) -> str:
        return f"{GF_BASE}/stories/ts{story_id}.htm"

    @staticmethod
    def _parse_metadata(soup, story_id: int) -> dict:
        title = ""
        title_tag = soup.find("title")
        if title_tag:
            raw = title_tag.get_text(" ", strip=True)
            # "Our Feet Need To Be Worshiped at greatfeet.com - A Great
            # Feet Foot Fetish Story Publication" — strip the archive
            # boilerplate so only the story name remains.
            title = re.sub(
                r"\s*at\s*greatfeet\.com\s*-?\s*.*$", "", raw, flags=re.I,
            ).strip()
        if not title:
            # Title also appears in a top-of-page <b><font size="+4">.
            big = soup.find("font", attrs={"size": "+4"})
            if big:
                title = big.get_text(" ", strip=True)
        if not title:
            title = f"GreatFeet story {story_id}"

        # Attribution: GreatFeet puts "submitted anonymously." or
        # "submitted by <Handle>" in an <i><font> run near the top.
        author = "Anonymous"
        body_text = soup.get_text(" ", strip=True)
        m = re.search(
            r"submitted\s+by\s+([A-Za-z0-9][A-Za-z0-9 _-]{1,40})",
            body_text, re.I,
        )
        if m:
            author = m.group(1).strip()

        summary = ""
        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            summary = desc["content"].strip()

        return {
            "title": title,
            "author": author,
            "author_url": "",
            "summary": summary,
            "num_chapters": 1,
            "chapter_titles": {"1": title},
            "extra": {"tags": ["feet"]},
        }

    @staticmethod
    def _parse_chapter_html(soup) -> str:
        """GreatFeet's 90s-HTML layout doesn't mark the story body with
        a class or id — the prose is the long run of ``<p><font>`` tags
        after a ``<hr>`` divider that separates the header block from
        the submission. Collect every ``<p>`` after the first ``<hr>``
        whose rendered text has any real length."""
        hrs = soup.find_all("hr")
        if not hrs:
            # No divider — collect all <p> from the body.
            candidates = soup.find_all("p")
        else:
            first_hr = hrs[0]
            candidates = []
            for sib in first_hr.find_all_next("p"):
                candidates.append(sib)
        pieces: list[str] = []
        for p in candidates:
            text = p.get_text(" ", strip=True)
            if len(text) < 20:
                continue
            # Drop GreatFeet's standard "Please help support our site..."
            # trailer that closes every submission.
            if re.search(
                r"(please visit|support our site|continue reading|back to main)",
                text, re.I,
            ):
                continue
            pieces.append(str(p))
        if not pieces:
            raise ValueError("Could not find GreatFeet story body.")
        return "\n".join(pieces)

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
        story_url = self._story_url(story_id)

        logger.info("Fetching GreatFeet story %s...", story_id)
        page_html = self._fetch(story_url)
        soup = BeautifulSoup(page_html, "lxml")

        meta = self._parse_metadata(soup, story_id)
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            author_url="",
            metadata=meta["extra"],
        )

        if skip_chapters >= 1 or not chapter_in_spec(1, chapters):
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
