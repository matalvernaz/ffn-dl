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

    # Royal Road injects anti-piracy paragraphs ("if you spot this
    # narrative on amazon, know that it has been stolen — report the
    # violation", and rotating variants) into chapter HTML. Each
    # injected element carries a random class that's hidden via a
    # display:none rule in the same page's <style> block. Real browsers
    # never show them; curl_cffi doesn't render CSS, so the text ends up
    # in the EPUB unless we strip at scrape time. FanFicFare and
    # Aivean/royalroad-downloader both solve this the same way: collect
    # the hidden classes from CSS, drop elements that use them. That's
    # survived ~2 years of RR rotating both class names and phrasing.
    _HIDDEN_RULE_RE = re.compile(
        r"\.([A-Za-z0-9_-]+)\s*\{[^}]*"
        r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0|"
        r"font-size\s*:\s*0|speak\s*:\s*never)"
        r"[^}]*\}",
        re.IGNORECASE,
    )

    @classmethod
    def _hidden_classes(cls, soup) -> set:
        """Collect CSS class names that any inline <style> block hides.

        Royal Road's anti-piracy injection attaches one of these classes
        to the paragraph it wants hidden. We only look at <style> tags
        on the page itself — external stylesheets are normal site CSS
        unrelated to per-request injection.
        """
        classes = set()
        for style in soup.find_all("style"):
            css = style.string or style.get_text() or ""
            if not css:
                continue
            for match in cls._HIDDEN_RULE_RE.finditer(css):
                classes.add(match.group(1))
        return classes

    @classmethod
    def _parse_chapter_html(cls, soup):
        content = soup.find("div", class_="chapter-inner")
        if content is None:
            content = soup.find("div", class_="chapter-content")
        if content is None:
            raise ValueError("Could not locate chapter content on Royal Road page.")

        hidden = cls._hidden_classes(soup)
        if hidden:
            # Collect first, then decompose: mutating the tree mid-iteration
            # leaves orphaned descendants whose `attrs` becomes None, which
            # then crashes the next `tag.get("class")` call.
            doomed = [
                tag for tag in content.find_all(True)
                if any(c in hidden for c in (tag.get("class") or []))
            ]
            for tag in doomed:
                tag.decompose()
            removed = len(doomed)
            if removed:
                logger.debug(
                    "Stripped %d element(s) hidden by page CSS (likely "
                    "Royal Road anti-piracy injection)", removed,
                )
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

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from a RR profile page."""
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title = soup.find("title")
        if title:
            t = title.get_text(strip=True)
            if " |" in t:
                author_name = t.split(" |")[0].strip()

        seen = set()
        works = []
        for a in soup.find_all(
            "a", href=re.compile(r"^/fiction/\d+(?:/[^/]+)?/?$")
        ):
            match = re.search(r"/fiction/(\d+)", a["href"])
            if not match:
                continue
            fid = match.group(1)
            if fid in seen:
                continue
            seen.add(fid)
            works.append({
                "title": a.get_text(strip=True) or f"Fiction {fid}",
                "url": f"{RR_BASE}/fiction/{fid}",
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

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        from .models import chapter_in_spec

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
            if not chapter_in_spec(i, chapters):
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
