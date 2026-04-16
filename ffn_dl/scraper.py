"""HTTP fetching, HTML parsing, and rate-limit handling for fanfiction.net."""

import json
import logging
import random
import re
import time
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .models import Chapter, Story

logger = logging.getLogger(__name__)

BASE_URL = "https://www.fanfiction.net"

# Impersonation targets for curl_cffi — rotated on rate-limit retries
BROWSERS = ["chrome", "chrome", "safari", "edge"]


class RateLimitError(Exception):
    """Raised when rate-limit retries are exhausted."""


class StoryNotFoundError(Exception):
    """Raised when the story does not exist."""


class CloudflareBlockError(Exception):
    """Raised when Cloudflare blocks the request."""


def _default_cache_dir():
    """Return ~/.cache/ffn-dl, creating it if needed."""
    path = Path.home() / ".cache" / "ffn-dl"
    path.mkdir(parents=True, exist_ok=True)
    return path


class FFNScraper:
    """Scraper for fanfiction.net with rate-limit handling and CF bypass."""

    def __init__(
        self,
        delay_range=(2.0, 5.0),
        max_retries=5,
        timeout=30,
        cache_dir=None,
        use_cache=True,
    ):
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.timeout = timeout
        self.use_cache = use_cache
        self.cache_dir = (
            (Path(cache_dir) if cache_dir else _default_cache_dir())
            if use_cache
            else None
        )
        self._browser = "chrome"
        self.session = curl_requests.Session(impersonate=self._browser)

    def _rotate_browser(self):
        self._browser = random.choice(BROWSERS)
        self.session = curl_requests.Session(impersonate=self._browser)
        logger.debug("Rotated to browser impersonation: %s", self._browser)

    def _check_for_blocks(self, html):
        """Detect Cloudflare challenge pages or soft blocks."""
        lower = html[:2000].lower()
        if "just a moment" in lower and "cloudflare" in lower:
            raise CloudflareBlockError(
                "Cloudflare challenge detected. "
                "Try increasing delays or waiting before retrying."
            )
        if "<title>Story Not Found</title>" in html:
            raise StoryNotFoundError("Story does not exist or has been removed.")

    def _fetch(self, url):
        """Fetch a URL with retries and exponential backoff on rate limits."""
        backoff = 30
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except curl_requests.errors.ConnectionError as exc:
                logger.warning(
                    "Connection error (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries,
                    exc,
                )
                time.sleep(backoff + random.uniform(0, 5))
                backoff = min(backoff * 2, 300)
                continue
            except curl_requests.errors.Timeout:
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
                self._rotate_browser()
                continue

            if resp.status_code == 404:
                raise StoryNotFoundError(f"Story not found: {url}")

            if resp.status_code == 403:
                # Short wait, keep the same session (cookies survive).
                # Only rotate browser as a last resort — new sessions lose
                # Cloudflare cookies and make blocking worse.
                wait = 5 + random.uniform(0, 5)
                if attempt >= self.max_retries - 2:
                    self._rotate_browser()
                    wait = 30
                logger.warning(
                    "Forbidden (HTTP 403), retrying in %.0fs (attempt %d/%d)",
                    wait,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(wait)
                continue

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

    # ── Cache helpers ─────────────────────────────────────────────

    def _story_cache_dir(self, story_id):
        if not self.use_cache:
            return None
        d = self.cache_dir / str(story_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_meta_cache(self, story_id, meta):
        if not self.use_cache:
            return
        path = self._story_cache_dir(story_id) / "meta.json"
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _load_meta_cache(self, story_id):
        if not self.use_cache:
            return None
        path = self._story_cache_dir(story_id) / "meta.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _save_chapter_cache(self, story_id, chapter):
        if not self.use_cache:
            return
        path = self._story_cache_dir(story_id) / f"ch_{chapter.number:04d}.html"
        path.write_text(
            json.dumps({"title": chapter.title, "html": chapter.html}),
            encoding="utf-8",
        )

    def _load_chapter_cache(self, story_id, chap_num):
        if not self.use_cache:
            return None
        path = self._story_cache_dir(story_id) / f"ch_{chap_num:04d}.html"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return Chapter(number=chap_num, title=data["title"], html=data["html"])
        return None

    def clean_cache(self, story_id):
        """Remove cached data for a story after successful export."""
        if not self.use_cache:
            return
        import shutil

        d = self.cache_dir / str(story_id)
        if d.exists():
            shutil.rmtree(d)
            logger.debug("Cleaned cache for story %d", story_id)

    # ── Parsing ───────────────────────────────────────────────────

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
                cleaned = re.sub(r"^\d+\.\s*", "", label)
                chapter_titles[num] = cleaned if cleaned else f"Chapter {num}"
        else:
            num_chapters = 1
            chapter_titles = {1: title}

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
            # Convert int keys to str for JSON serialization
            "chapter_titles": {str(k): v for k, v in chapter_titles.items()},
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_html(soup):
        """Extract the story text HTML from a chapter page."""
        storytext = soup.find("div", id="storytext")
        if not storytext:
            raise ValueError("Could not find story text on page.")
        return storytext.decode_contents()

    # ── Main download ─────────────────────────────────────────────

    def download(self, url_or_id, progress_callback=None):
        """Download a complete story, resuming from cache when possible.

        progress_callback(current, total, chapter_title, cached) is called
        after each chapter is loaded.  ``cached`` is True when loaded from
        disk rather than fetched from the network.
        """
        story_id = self.parse_story_id(url_or_id)
        story_url = f"{BASE_URL}/s/{story_id}"

        # Try loading metadata from cache first; always re-fetch ch1 to get
        # up-to-date chapter count (story might still be publishing).
        ch1_url = f"{story_url}/1"
        logger.info("Fetching story metadata...")
        page = self._fetch(ch1_url)
        soup = BeautifulSoup(page, "lxml")

        meta = self._parse_metadata(soup)
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]
        self._save_meta_cache(story_id, meta)

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            metadata=meta["extra"],
        )

        # Chapter 1 — just parsed from the page we already have
        html = self._parse_chapter_html(soup)
        ch1_title = chapter_titles.get("1", "Chapter 1")
        ch1 = Chapter(number=1, title=ch1_title, html=html)
        self._save_chapter_cache(story_id, ch1)
        story.chapters.append(ch1)
        if progress_callback:
            progress_callback(1, num_chapters, ch1_title, False)

        # Remaining chapters — use cache when available
        for chap_num in range(2, num_chapters + 1):
            ch_title = chapter_titles.get(str(chap_num), f"Chapter {chap_num}")

            cached = self._load_chapter_cache(story_id, chap_num)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(chap_num, num_chapters, cached.title, True)
                continue

            self._delay()
            url = f"{story_url}/{chap_num}"
            logger.debug("Fetching chapter %d/%d", chap_num, num_chapters)
            page = self._fetch(url)
            soup = BeautifulSoup(page, "lxml")
            html = self._parse_chapter_html(soup)

            ch = Chapter(number=chap_num, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(chap_num, num_chapters, ch_title, False)

        return story
