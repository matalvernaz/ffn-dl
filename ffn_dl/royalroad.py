"""Royal Road (royalroad.com) scraper.

Fiction landing page lists every chapter in `table#chapters` with direct
links; per-chapter content lives in `div.chapter-inner.chapter-content`.
Cleaner than FFN — no captcha wall, chapter list is already complete
without pagination.
"""

import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

RR_BASE = "https://www.royalroad.com"


class RoyalRoadScraper(BaseScraper):
    """Scraper for royalroad.com."""

    site_name = "royalroad"

    def __init__(self, **kwargs):
        kwargs.setdefault("delay_range", (1.0, 3.0))
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"royalroad\.com/fiction/(\d+)", text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse Royal Road fiction ID from: {text!r}\n"
            "Expected a URL like https://www.royalroad.com/fiction/12345 "
            "or a numeric ID."
        )

    @staticmethod
    def is_author_url(url):
        return bool(re.search(r"royalroad\.com/profile/\d+", str(url)))

    @staticmethod
    def _parse_metadata(soup):
        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        author = "Unknown Author"
        author_url = ""
        author_link = soup.find("a", href=re.compile(r"/profile/\d+"))
        if author_link:
            author = author_link.get_text(strip=True)
            href = author_link["href"]
            author_url = RR_BASE + href if href.startswith("/") else href

        desc = soup.find("div", class_="description")
        summary = desc.get_text(" ", strip=True) if desc else ""

        extra = {}
        cover = soup.find("img", class_="thumbnail")
        if cover and cover.get("src"):
            extra["cover_url"] = cover["src"]

        # "Original / Fanfiction", "ONGOING / COMPLETED / HIATUS / STUB", "N Chapters"
        status = None
        for label in soup.find_all("span", class_="label"):
            text = label.get_text(strip=True).upper()
            if text in ("ONGOING", "COMPLETED", "HIATUS", "STUB", "DROPPED"):
                status = "Complete" if text == "COMPLETED" else text.title()
        if status:
            extra["status"] = status

        # Tags from the fiction's tag list — stored as "genre"
        tag_links = soup.select("span.tags a.fiction-tag")
        if tag_links:
            extra["genre"] = ", ".join(
                a.get_text(strip=True) for a in tag_links[:12]
            )

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_list(soup):
        """Return a list of {id, title, url} dicts from the chapters table."""
        table = soup.find("table", id="chapters")
        if not table:
            return []
        tbody = table.find("tbody") or table
        chapters = []
        for row in tbody.find_all("tr"):
            link = row.find(
                "a", href=re.compile(r"/fiction/\d+/[^/]+/chapter/\d+")
            )
            if not link:
                continue
            href = link["href"]
            match = re.search(r"/chapter/(\d+)", href)
            if not match:
                continue
            chapters.append({
                "id": int(match.group(1)),
                "title": link.get_text(strip=True),
                "url": RR_BASE + href if href.startswith("/") else href,
            })
        return chapters

    @staticmethod
    def _parse_chapter_html(soup):
        content = soup.find("div", class_="chapter-inner")
        if content is None:
            content = soup.find("div", class_="chapter-content")
        if content is None:
            raise ValueError("Could not locate chapter content on Royal Road page.")
        return content.decode_contents()

    def get_chapter_count(self, url_or_id):
        fiction_id = self.parse_story_id(url_or_id)
        html = self._fetch(f"{RR_BASE}/fiction/{fiction_id}")
        soup = BeautifulSoup(html, "lxml")
        return len(self._parse_chapter_list(soup))

    def scrape_author_stories(self, url):
        """Author page lists fictions they've written under 'Fictions'."""
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title = soup.find("title")
        if title:
            t = title.get_text(strip=True)
            if " |" in t:
                author_name = t.split(" |")[0].strip()

        seen = set()
        story_urls = []
        for a in soup.find_all(
            "a", href=re.compile(r"^/fiction/\d+(?:/[^/]+)?/?$")
        ):
            match = re.search(r"/fiction/(\d+)", a["href"])
            if match and match.group(1) not in seen:
                seen.add(match.group(1))
                story_urls.append(f"{RR_BASE}/fiction/{match.group(1)}")

        return author_name, story_urls

    def download(self, url_or_id, progress_callback=None, skip_chapters=0):
        fiction_id = self.parse_story_id(url_or_id)
        fiction_url = f"{RR_BASE}/fiction/{fiction_id}"

        logger.info("Fetching Royal Road fiction %s...", fiction_id)
        html = self._fetch(fiction_url)
        soup = BeautifulSoup(html, "lxml")

        meta = self._parse_metadata(soup)
        chapter_list = self._parse_chapter_list(soup)
        if not chapter_list:
            raise StoryNotFoundError(
                f"No chapters found on Royal Road fiction {fiction_id}."
            )

        self._save_meta_cache(fiction_id, {**meta, "num_chapters": len(chapter_list)})

        story = Story(
            id=fiction_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=fiction_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        total = len(chapter_list)
        for i, ch_info in enumerate(chapter_list, 1):
            if i <= skip_chapters:
                continue

            cached = self._load_chapter_cache(fiction_id, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, total, cached.title, True)
                continue

            if story.chapters:
                self._delay()
            page = self._fetch(ch_info["url"])
            ch_soup = BeautifulSoup(page, "lxml")
            html_content = self._parse_chapter_html(ch_soup)

            ch = Chapter(number=i, title=ch_info["title"], html=html_content)
            self._save_chapter_cache(fiction_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, total, ch_info["title"], False)

        return story
