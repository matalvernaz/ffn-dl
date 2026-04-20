"""Nifty (nifty.org) scraper.

Nifty is the long-running LGBT+ erotica archive. Its data model is
radically simple: chapters are plain-text files served verbatim from a
directory tree like ``/nifty/<category>/<subcategory>/<slug>/<slug>-<n>``.
No HTML, no CSS, no JavaScript — the raw text from the ancient Usenet
alt.* posts reproduced as-is, including email header blocks ("Date:",
"From:", "Subject:") on each chapter.

Story URLs we accept:
    https://www.nifty.org/nifty/gay/college/the-brotherhood/

A "story" is a directory. Chapters are the files inside it, usually
named ``<slug>-<n>`` (no extension) or ``<slug>N.txt``. We fetch the
directory index first, scrape chapter filenames from the ``<a>`` tags,
sort them numerically, and fetch each as plain text. Because the body
is plain text we wrap it in ``<pre>`` so downstream HTML/EPUB exports
preserve formatting.
"""

import hashlib
import html as html_module
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import Chapter, Story, chapter_in_spec
from ..scraper import BaseScraper

logger = logging.getLogger(__name__)

NIFTY_BASE = "https://www.nifty.org"

NIFTY_STORY_URL_RE = re.compile(
    r"^https?://(?:www\.)?nifty\.org/(nifty/[a-z0-9/_-]+?)/?$", re.I,
)
"""Matches a Nifty story directory (trailing slash optional). Capture
group 1 is the path from "nifty/..." through the slug, without a
leading slash."""


def _story_path(url_or_id: str) -> str:
    """Return the "nifty/<cat>/.../<slug>" path portion of a Nifty URL.

    Accepts either a full URL or a bare path like
    ``nifty/gay/college/the-brotherhood``. Normalises trailing
    slashes away so two URL variants produce the same id.
    """
    text = str(url_or_id).strip().rstrip("/")
    m = NIFTY_STORY_URL_RE.search(text)
    if m:
        return m.group(1).strip("/")
    if text.startswith("nifty/"):
        return text.strip("/")
    raise ValueError(
        f"Cannot parse Nifty story URL from: {text!r}\n"
        "Expected e.g. https://www.nifty.org/nifty/gay/college/the-brotherhood/"
    )


def _path_to_id(path: str) -> int:
    """Stable integer id derived from the story's path.

    Nifty has no numeric ids — paths are the identity. Hash
    deterministically so the Story model's numeric-id contract is
    honoured and the same path always yields the same cache key."""
    h = hashlib.md5(path.encode("utf-8")).hexdigest()[:12]
    return int(h, 16)


_CHAPTER_NUM_RE = re.compile(r"[-_]?(\d+)(?:\.[a-z0-9]+)?$", re.I)


class NiftyScraper(BaseScraper):
    """Scraper for nifty.org story directories."""

    site_name = "nifty"

    @staticmethod
    def parse_story_id(url_or_id):
        """Return the "nifty/..." path. Callers that want a numeric
        id should feed the path through :func:`_path_to_id`."""
        return _story_path(url_or_id)

    @staticmethod
    def _story_url(path: str) -> str:
        return f"{NIFTY_BASE}/{path.strip('/')}/"

    @staticmethod
    def _parse_chapter_filenames(soup, story_url: str) -> list[tuple[int, str, str]]:
        """Return ``[(n, filename, absolute_url), ...]`` sorted by n.

        Directory indexes on Nifty are simple ``<a>`` lists. We keep
        only links that stay inside the story directory (i.e. whose
        resolved absolute URL starts with the story's URL and whose
        path has one extra segment). Filenames that end with a number
        get that number as their sort key; everything else falls back
        to insertion order so the author's naming wins on edge cases.
        """
        story_split = urlsplit(story_url)
        story_prefix = story_split.path if story_split.path.endswith("/") else story_split.path + "/"
        results: list[tuple[int, str, str]] = []
        fallback_n = 0
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("?"):
                continue
            abs_url = urljoin(story_url, href)
            abs_split = urlsplit(abs_url)
            if abs_split.netloc.lower() not in (story_split.netloc.lower(), ""):
                continue
            path = abs_split.path
            if not path.startswith(story_prefix) or path == story_prefix:
                continue
            tail = path[len(story_prefix):].strip("/")
            if not tail or "/" in tail:
                continue
            # Skip obvious navigation files (index.html, readme.txt).
            lower = tail.lower()
            if lower in ("index.html", "readme.txt", "parent-directory"):
                continue
            m = _CHAPTER_NUM_RE.search(tail)
            if m:
                n = int(m.group(1))
            else:
                fallback_n += 1
                n = 10_000 + fallback_n  # sort after numbered chapters
            results.append((n, tail, abs_url))
        # Dedupe by filename while keeping the first occurrence.
        seen = set()
        dedup = []
        for n, name, url in results:
            if name in seen:
                continue
            seen.add(name)
            dedup.append((n, name, url))
        dedup.sort(key=lambda t: (t[0], t[1]))
        return dedup

    @staticmethod
    def _parse_meta_from_index(soup, path: str) -> dict:
        """Extract title / author / summary from a Nifty story directory
        index, falling back to the URL slug when the index page has
        no human-authored metadata (common for older story dirs)."""
        slug = path.rstrip("/").rsplit("/", 1)[-1] if path else "nifty-story"
        slug_title = slug.replace("-", " ").replace("_", " ").strip().title()

        title_tag = soup.find(["h1", "h2", "title"])
        raw = title_tag.get_text(" ", strip=True) if title_tag else ""
        # Directory indexes often say "Index of /nifty/...". Strip that.
        if raw.lower().startswith("index of "):
            raw = ""
        title = raw or slug_title

        # Author / summary are rarely on the index; leave defaults.
        author = "Nifty archive"
        summary = ""
        body_text = soup.get_text(" ", strip=True)
        if body_text and len(body_text) > 40:
            # Keep the first ~280 chars as a coarse description.
            summary = body_text[:280]

        return {
            "title": title,
            "author": author,
            "summary": summary,
        }

    @staticmethod
    def _wrap_plaintext(text: str) -> str:
        """Wrap a raw-text Nifty chapter in ``<pre>`` so HTML/EPUB
        exports preserve linebreaks. We HTML-escape first so angle
        brackets in the body aren't re-interpreted as tags."""
        return "<pre>" + html_module.escape(text) + "</pre>"

    @staticmethod
    def _strip_nifty_header(text: str) -> str:
        """Drop the Usenet/email-style header block that each Nifty
        chapter begins with ("Date:", "From:", "Subject:" lines, then a
        blank line, then the story). We keep the *first* genuine prose
        line so the chapter doesn't start with the Patreon/donation
        boilerplate that many authors now prepend."""
        lines = text.splitlines()
        if len(lines) < 3:
            return text
        if not any(line.startswith(("Date:", "From:", "Subject:")) for line in lines[:6]):
            return text
        # Skip until first blank line, then the first non-empty line.
        idx = 0
        while idx < len(lines) and lines[idx].strip():
            idx += 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        return "\n".join(lines[idx:]) if idx < len(lines) else text

    def get_chapter_count(self, url_or_id):
        path = self.parse_story_id(url_or_id)
        html = self._fetch(self._story_url(path))
        soup = BeautifulSoup(html, "lxml")
        chapters = self._parse_chapter_filenames(soup, self._story_url(path))
        return len(chapters) or 1

    def download(
        self,
        url_or_id,
        progress_callback=None,
        skip_chapters: int = 0,
        chapters: Optional[list] = None,
    ):
        path = self.parse_story_id(url_or_id)
        story_url = self._story_url(path)
        story_id = _path_to_id(path)

        logger.info("Fetching Nifty story %s...", path)
        index_html = self._fetch(story_url)
        soup = BeautifulSoup(index_html, "lxml")

        meta = self._parse_meta_from_index(soup, path)
        chapter_list = self._parse_chapter_filenames(soup, story_url)
        if not chapter_list:
            raise ValueError(
                f"No chapter files found at {story_url}. "
                "The directory may be empty or the URL may be off by one level."
            )
        num_chapters = len(chapter_list)
        self._save_meta_cache(
            story_id,
            {**meta, "path": path, "num_chapters": num_chapters},
        )

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            author_url="",
            metadata={"path": path},
        )

        for i, (_, filename, chap_url) in enumerate(chapter_list, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue
            cached = self._load_chapter_cache(story_id, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue

            if story.chapters:
                self._delay()
            logger.debug("Fetching Nifty chapter %d/%d (%s)", i, num_chapters, filename)
            raw = self._fetch(chap_url)
            body = self._strip_nifty_header(raw)
            wrapped = self._wrap_plaintext(body)

            title = f"Chapter {i}"
            ch = Chapter(number=i, title=title, html=wrapped)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, title, False)

        return story
