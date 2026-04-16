"""HTTP fetching, HTML parsing, and rate-limit handling for fanfiction.net."""

import logging
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from .models import Chapter, Story

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fanfiction.net"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


class RateLimitError(Exception):
    """Raised when rate-limit retries are exhausted."""


class StoryNotFoundError(Exception):
    """Raised when the story does not exist."""


class CloudflareBlockError(Exception):
    """Raised when Cloudflare blocks the request."""


class FFNScraper:
    """Scraper for fanfiction.net with rate-limit handling and CF bypass."""

    def __init__(self, delay_range=(2.0, 5.0), max_retries=5, timeout=30):
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = self._create_session()

    def _create_session(self):
        try:
            import cloudscraper

            session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
            logger.debug("Using cloudscraper for Cloudflare bypass")
        except ImportError:
            logger.debug("cloudscraper not installed, using plain requests")
            session = requests.Session()

        session.headers.update(
            {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
        )
        return session

    def _check_for_blocks(self, html):
        """Detect Cloudflare challenge pages or soft blocks."""
        lower = html[:2000].lower()
        if "just a moment" in lower and "cloudflare" in lower:
            raise CloudflareBlockError(
                "Cloudflare challenge detected. Try installing cloudscraper, "
                "increasing delays, or waiting before retrying."
            )
        if "<title>Story Not Found</title>" in html:
            raise StoryNotFoundError("Story does not exist or has been removed.")

    def _fetch(self, url):
        """Fetch a URL with retries and exponential backoff on rate limits."""
        backoff = 30
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.ConnectionError as exc:
                logger.warning(
                    "Connection error (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                time.sleep(backoff + random.uniform(0, 5))
                backoff = min(backoff * 2, 300)
                continue
            except requests.Timeout:
                logger.warning(
                    "Request timed out (attempt %d/%d)", attempt + 1, self.max_retries
                )
                time.sleep(10)
                continue

            if resp.status_code == 200:
                self._check_for_blocks(resp.text)
                return resp.text

            if resp.status_code in (429, 503):
                jitter = random.uniform(0, backoff * 0.1)
                wait = backoff + jitter
                logger.warning(
                    "Rate limited (HTTP %d), waiting %.0fs (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 300)
                self.session.headers["User-Agent"] = random.choice(USER_AGENTS)
                continue

            if resp.status_code == 404:
                raise StoryNotFoundError(f"Story not found: {url}")

            logger.warning(
                "Unexpected HTTP %d (attempt %d/%d)",
                resp.status_code,
                attempt + 1,
                self.max_retries,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

        raise RateLimitError(f"Failed after {self.max_retries} retries: {url}")

    def _delay(self):
        """Random delay between requests to avoid triggering rate limits."""
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    @staticmethod
    def parse_story_id(url_or_id):
        """Extract the numeric story ID from a URL or bare ID string."""
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"fanfiction\.net/s/(\d+)", text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse story ID from: {text!r}\n"
            "Expected a URL like https://www.fanfiction.net/s/12345 or a numeric ID."
        )

    @staticmethod
    def _parse_metadata(soup):
        """Extract title, author, summary, and chapter list from a story page."""
        profile = soup.find("div", id="profile_top")
        if not profile:
            raise ValueError(
                "Could not find story profile. The page may be blocked or malformed."
            )

        title_tag = profile.find("b", class_="xcontrast_txt")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        author_tag = profile.find("a", class_="xcontrast_txt", href=re.compile(r"/u/"))
        author = author_tag.get_text(strip=True) if author_tag else "Unknown Author"

        summary_div = profile.find("div", class_="xcontrast_txt", style=True)
        summary = summary_div.get_text(strip=True) if summary_div else ""

        # Chapter list from the dropdown (absent for one-shots)
        chap_select = soup.find("select", id="chap_select")
        if chap_select:
            options = chap_select.find_all("option")
            num_chapters = len(options)
            chapter_titles = {}
            for opt in options:
                num = int(opt["value"])
                label = opt.get_text(strip=True)
                # Strip leading "N. " that FFN prepends
                cleaned = re.sub(r"^\d+\.\s*", "", label)
                chapter_titles[num] = cleaned if cleaned else f"Chapter {num}"
        else:
            num_chapters = 1
            chapter_titles = {1: title}

        # Grab the raw metadata span for extra info
        extra = {}
        meta_span = profile.find("span", class_="xgray")
        if meta_span:
            meta_text = meta_span.get_text()
            extra["raw"] = meta_text.strip()

            words_match = re.search(r"Words:\s*([\d,]+)", meta_text)
            if words_match:
                extra["words"] = words_match.group(1)
            status_match = re.search(r"Status:\s*(\w+)", meta_text)
            if status_match:
                extra["status"] = status_match.group(1)
            rated_match = re.search(r"Rated:\s*(?:Fiction\s+)?(\S+)", meta_text)
            if rated_match:
                extra["rating"] = rated_match.group(1)
            lang_match = re.search(
                r"(?:Rated:.*?-\s+)(\w+(?:\s+\w+)?)\s+-", meta_text
            )
            if lang_match:
                extra["language"] = lang_match.group(1)

        return {
            "title": title,
            "author": author,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_text(soup):
        """Extract the story text from a chapter page as HTML and plain text."""
        storytext = soup.find("div", id="storytext")
        if not storytext:
            raise ValueError("Could not find story text on page.")

        html = storytext.decode_contents()
        text = storytext.get_text("\n", strip=True)
        return html, text

    def download(self, url_or_id, progress_callback=None):
        """Download a complete story. Returns a Story object.

        progress_callback(current_chapter, total_chapters) is called after
        each chapter is fetched.
        """
        story_id = self.parse_story_id(url_or_id)
        story_url = f"{BASE_URL}/s/{story_id}"

        # Fetch chapter 1 for metadata
        ch1_url = f"{story_url}/1"
        logger.info("Fetching story metadata...")
        page = self._fetch(ch1_url)
        soup = BeautifulSoup(page, "lxml")

        meta = self._parse_metadata(soup)
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            metadata=meta["extra"],
        )

        # Parse chapter 1 (already fetched)
        html, text = self._parse_chapter_text(soup)
        story.chapters.append(
            Chapter(
                number=1,
                title=chapter_titles.get(1, "Chapter 1"),
                html=html,
                text=text,
            )
        )
        if progress_callback:
            progress_callback(1, num_chapters)

        # Fetch remaining chapters
        for chap_num in range(2, num_chapters + 1):
            self._delay()
            url = f"{story_url}/{chap_num}"
            logger.debug("Fetching chapter %d/%d", chap_num, num_chapters)
            page = self._fetch(url)
            soup = BeautifulSoup(page, "lxml")
            html, text = self._parse_chapter_text(soup)

            story.chapters.append(
                Chapter(
                    number=chap_num,
                    title=chapter_titles.get(chap_num, f"Chapter {chap_num}"),
                    html=html,
                    text=text,
                )
            )
            if progress_callback:
                progress_callback(chap_num, num_chapters)

        return story
