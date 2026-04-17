"""Base scraper with HTTP fetching, caching, and rate-limit handling."""

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

BROWSERS = ["chrome", "chrome", "safari", "edge"]


class RateLimitError(Exception):
    """Raised when rate-limit retries are exhausted."""


class StoryNotFoundError(Exception):
    """Raised when the story does not exist."""


class CloudflareBlockError(Exception):
    """Raised when Cloudflare blocks the request."""


def _default_cache_dir():
    path = Path.home() / ".cache" / "ffn-dl"
    path.mkdir(parents=True, exist_ok=True)
    return path


class BaseScraper:
    """Shared HTTP, retry, and cache logic for all site scrapers."""

    site_name = "unknown"

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
        lower = html[:2000].lower()
        if "just a moment" in lower and "cloudflare" in lower:
            raise CloudflareBlockError(
                "Cloudflare challenge detected. "
                "Try increasing delays or waiting before retrying."
            )

    def _fetch(self, url):
        backoff = 30
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except curl_requests.errors.ConnectionError as exc:
                logger.warning(
                    "Connection error (attempt %d/%d): %s",
                    attempt + 1, self.max_retries, exc,
                )
                time.sleep(backoff + random.uniform(0, 5))
                backoff = min(backoff * 2, 300)
                continue
            except curl_requests.errors.Timeout:
                logger.warning(
                    "Request timed out (attempt %d/%d)",
                    attempt + 1, self.max_retries,
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
                    resp.status_code, wait, attempt + 1, self.max_retries,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 300)
                self._rotate_browser()
                continue

            if resp.status_code == 404:
                raise StoryNotFoundError(f"Not found: {url}")

            if resp.status_code == 403:
                wait = 5 + random.uniform(0, 5)
                if attempt >= self.max_retries - 2:
                    self._rotate_browser()
                    wait = 30
                logger.warning(
                    "Forbidden (HTTP 403), retrying in %.0fs (attempt %d/%d)",
                    wait, attempt + 1, self.max_retries,
                )
                time.sleep(wait)
                continue

            logger.warning(
                "Unexpected HTTP %d (attempt %d/%d)",
                resp.status_code, attempt + 1, self.max_retries,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

        raise RateLimitError(f"Failed after {self.max_retries} retries: {url}")

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    # ── Cache ─────────────────────────────────────────────────────

    def _story_cache_dir(self, story_id):
        if not self.use_cache:
            return None
        d = self.cache_dir / f"{self.site_name}_{story_id}"
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
        if not self.use_cache:
            return
        import shutil
        d = self.cache_dir / f"{self.site_name}_{story_id}"
        if d.exists():
            shutil.rmtree(d)
            logger.debug("Cleaned cache for story %s", story_id)

    # ── Abstract interface ────────────────────────────────────────

    @staticmethod
    def parse_story_id(url_or_id):
        raise NotImplementedError

    def download(self, url_or_id, progress_callback=None):
        raise NotImplementedError


# ── FFN ───────────────────────────────────────────────────────────

FFN_BASE = "https://www.fanfiction.net"


class FFNScraper(BaseScraper):
    """Scraper for fanfiction.net."""

    site_name = "ffn"

    def _check_for_blocks(self, html):
        super()._check_for_blocks(html)
        if "<title>Story Not Found</title>" in html:
            raise StoryNotFoundError("Story does not exist or has been removed.")

    @staticmethod
    def parse_story_id(url_or_id):
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
        profile = soup.find("div", id="profile_top")
        if not profile:
            raise ValueError(
                "Could not find story profile. The page may be blocked or malformed."
            )

        title_tag = profile.find("b", class_="xcontrast_txt")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        author_tag = profile.find("a", class_="xcontrast_txt", href=re.compile(r"/u/"))
        author = author_tag.get_text(strip=True) if author_tag else "Unknown Author"
        author_url = ""
        if author_tag and author_tag.get("href"):
            author_url = FFN_BASE + author_tag["href"]

        # Category / fandom from breadcrumb links above the profile
        pre_links = soup.find(id="pre_story_links")
        category = ""
        if pre_links:
            cat_parts = [a.get_text(strip=True) for a in pre_links.find_all("a")]
            category = " > ".join(cat_parts) if cat_parts else ""

        summary_div = profile.find("div", class_="xcontrast_txt", style=True)
        summary = summary_div.get_text(strip=True) if summary_div else ""

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

        cover_url = None
        cover_img = profile.find("img", class_="cimage")
        if cover_img:
            src = cover_img.get("data-original") or cover_img.get("src")
            if src:
                cover_url = src if src.startswith("http") else FFN_BASE + src

        extra = {}
        if cover_url:
            extra["cover_url"] = cover_url
        if category:
            extra["category"] = category

        meta_span = profile.find("span", class_="xgray")
        if meta_span:
            meta_text = meta_span.get_text()
            extra["raw"] = meta_text.strip()

            segments = [s.strip() for s in meta_text.split(" - ")]
            bare = []
            for seg in segments:
                if seg.startswith("Rated:"):
                    rated = seg.replace("Rated:", "").replace("Fiction", "").strip()
                    extra["rating"] = rated
                elif re.match(r"^(Words|Chapters|Reviews|Favs|Follows):", seg):
                    key, _, val = seg.partition(":")
                    extra[key.strip().lower()] = val.strip().rstrip()
                elif re.match(r"^(Updated|Published):", seg):
                    key, _, val = seg.partition(":")
                    extra[key.strip().lower()] = val.strip()
                elif re.match(r"^Status:", seg):
                    extra["status"] = seg.partition(":")[2].strip()
                elif re.match(r"^id:", seg):
                    pass
                else:
                    bare.append(seg)

            if len(bare) >= 1:
                extra["language"] = bare[0]
            if len(bare) >= 2:
                extra["genre"] = bare[1]
            if len(bare) >= 3:
                extra["characters"] = bare[2]

            time_spans = meta_span.find_all("span", attrs={"data-xutime": True})
            if len(time_spans) >= 2:
                extra["date_updated"] = int(time_spans[0]["data-xutime"])
                extra["date_published"] = int(time_spans[1]["data-xutime"])
            elif len(time_spans) == 1:
                extra["date_published"] = int(time_spans[0]["data-xutime"])

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": {str(k): v for k, v in chapter_titles.items()},
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_html(soup):
        storytext = soup.find("div", id="storytext")
        if not storytext:
            raise ValueError("Could not find story text on page.")
        return storytext.decode_contents()

    @staticmethod
    def is_author_url(url):
        """Return True if the URL is an FFN author page.

        Accepts both the canonical form (/u/<id>[/<name>]) and the
        short vanity form (/~<name>), which FFN redirects to /u/<id>/<name>.
        """
        return bool(
            re.search(r"fanfiction\.net/(?:u/\d+|~[\w.-]+)", str(url))
        )

    def scrape_author_stories(self, url):
        """Fetch an FFN author page and return (author_name, [story_urls]).

        The author page lists all stories as links matching /s/{id}/...
        """
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        # Author name from the page title or the span#content_wrapper_inner
        author_name = "Unknown Author"
        # FFN author pages have the author name in a span inside the content area
        title_tag = soup.find("title")
        if title_tag:
            # Title format: "AuthorName | FanFiction"
            title_text = title_tag.get_text(strip=True)
            if "|" in title_text:
                author_name = title_text.split("|")[0].strip()

        # Find all story links — they match /s/{id}
        seen_ids = set()
        story_urls = []
        for a_tag in soup.find_all("a", href=re.compile(r"^/s/\d+")):
            match = re.search(r"/s/(\d+)", a_tag["href"])
            if match:
                story_id = match.group(1)
                if story_id not in seen_ids:
                    seen_ids.add(story_id)
                    story_urls.append(f"{FFN_BASE}/s/{story_id}")

        return author_name, story_urls

    def download(self, url_or_id, progress_callback=None, skip_chapters=0):
        """Download a story. If skip_chapters > 0, only fetch metadata
        and chapters beyond that count (for update mode)."""
        story_id = self.parse_story_id(url_or_id)
        story_url = f"{FFN_BASE}/s/{story_id}"

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
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters:
            return story  # nothing new

        # Chapter 1 — always parsed from the metadata page we already have
        if skip_chapters < 1:
            html = self._parse_chapter_html(soup)
            ch1_title = chapter_titles.get("1", "Chapter 1")
            ch1 = Chapter(number=1, title=ch1_title, html=html)
            self._save_chapter_cache(story_id, ch1)
            story.chapters.append(ch1)
            if progress_callback:
                progress_callback(1, num_chapters, ch1_title, False)

        for chap_num in range(max(2, skip_chapters + 1), num_chapters + 1):
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
