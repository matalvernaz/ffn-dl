"""Archive of Our Own (archiveofourown.org) scraper.

AO3 hosts the full work on one page via view_full_work=true, so we
fetch the whole story in a single HTTP request rather than paging
through chapters like FFN. view_adult=true skips the adult-content
interstitial for Explicit-rated works (we're not logged in, so the
setting persists only for the session).
"""

import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

AO3_BASE = "https://archiveofourown.org"

# AO3's adult-content gate and login-required markers appear a bit
# further into the body than Cloudflare's challenge signature, so we
# read a larger prefix here than BaseScraper's default.
_BLOCK_CHECK_PREFIX_BYTES = 4000


class AO3LockedError(Exception):
    """Raised when a work requires an AO3 login to view."""


class AO3Scraper(BaseScraper):
    """Scraper for archiveofourown.org."""

    site_name = "ao3"

    def __init__(self, **kwargs):
        # AO3 fetches the whole work in a single request, so the inter-
        # chapter delay barely matters. AIMD defaults (floor 0) let us
        # back off only if AO3 ever actually pushes back.
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"archiveofourown\.org/works/(\d+)", text)
        if match:
            return int(match.group(1))
        # Also accept the mirror hostname used in some links
        match = re.search(r"ao3\.org/works/(\d+)", text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse AO3 work ID from: {text!r}\n"
            "Expected a URL like https://archiveofourown.org/works/12345 "
            "or a numeric ID."
        )

    def _check_for_blocks(self, html):
        super()._check_for_blocks(html)
        lower = html[:_BLOCK_CHECK_PREFIX_BYTES].lower()
        if "this work could have adult content" in lower and "proceed" in lower:
            # Should not happen when view_adult=true is set, but guard anyway
            raise AO3LockedError(
                "Adult-content gate was not bypassed. Try again — AO3 may "
                "require the view_adult=true parameter to be on the URL."
            )
        if "users must be logged in to access" in lower or (
            "sorry, you don't have permission" in lower
        ):
            raise AO3LockedError(
                "This work requires an AO3 login to view."
            )

    @staticmethod
    def is_author_url(url):
        """Return True if the URL is an AO3 user (author) page."""
        return bool(re.search(r"archiveofourown\.org/users/[\w.-]+", str(url)))

    @staticmethod
    def is_series_url(url):
        """Return True if the URL is an AO3 series page."""
        return bool(
            re.search(r"archiveofourown\.org/series/\d+", str(url))
        )

    @staticmethod
    def _parse_metadata(soup):
        """Extract title, author, summary, and stats from a work page."""
        title_tag = soup.find("h2", class_="title")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        byline = soup.find("h3", class_="byline")
        author = "Unknown Author"
        author_url = ""
        if byline:
            author_link = byline.find(
                "a", href=re.compile(r"^/users/[^/]+/pseuds/")
            )
            if author_link:
                author = author_link.get_text(strip=True)
                author_url = AO3_BASE + author_link["href"]
            else:
                # Anonymous or orphan_account works
                author = byline.get_text(strip=True) or "Anonymous"

        summary_tag = None
        # AO3 summary lives inside a preface module: div.summary.module > h3 + blockquote.userstuff
        preface = soup.find("div", class_="preface") or soup
        summary_mod = preface.find("div", class_="summary")
        if summary_mod:
            bq = summary_mod.find("blockquote")
            summary_tag = bq or summary_mod
        summary = summary_tag.get_text(strip=True) if summary_tag else ""

        extra = {}
        meta_dl = soup.select_one("dl.work.meta")
        if meta_dl:
            def dd_text(cls):
                dd = meta_dl.find("dd", class_=cls)
                return dd.get_text(" ", strip=True) if dd else None

            def dd_tags(cls):
                dd = meta_dl.find("dd", class_=cls)
                if not dd:
                    return []
                return [a.get_text(strip=True) for a in dd.find_all("a")]

            fandoms = dd_tags("fandom")
            if fandoms:
                extra["category"] = " / ".join(fandoms)
            rating = dd_text("rating")
            if rating:
                extra["rating"] = rating
            language = dd_text("language")
            if language:
                extra["language"] = language
            relationships = dd_tags("relationship")
            if relationships:
                extra["relationships"] = ", ".join(relationships)
            characters = dd_tags("character")
            if characters:
                extra["characters"] = ", ".join(characters)
            freeform = dd_tags("freeform")
            if freeform:
                extra["tags"] = ", ".join(freeform)
            warnings = dd_tags("warning")
            if warnings:
                extra["warnings"] = ", ".join(warnings)
            categories = dd_tags("category")
            if categories:
                extra["pairing_category"] = ", ".join(categories)

        stats = soup.find("dl", class_="stats")
        if stats:
            for dt, dd in zip(stats.find_all("dt"), stats.find_all("dd")):
                key = dt.get_text(strip=True).rstrip(":").lower().replace(" ", "_")
                val = dd.get_text(strip=True)
                if key == "words":
                    extra["words"] = val
                elif key == "chapters":
                    extra["chapter_ratio"] = val  # e.g. "5/10" or "5/?"
                elif key == "published":
                    extra["published"] = val
                elif key in ("completed", "updated"):
                    extra["date_updated_text"] = val
                elif key == "kudos":
                    extra["kudos"] = val
                elif key == "hits":
                    extra["hits"] = val
                elif key == "bookmarks":
                    extra["bookmarks"] = val
                elif key == "comments":
                    extra["comments"] = val

        # Derive "status" from the chapter ratio (5/5 = complete, 5/? = WIP)
        ratio = extra.get("chapter_ratio", "")
        if ratio:
            parts = ratio.split("/")
            if len(parts) == 2 and parts[0] == parts[1]:
                extra["status"] = "Complete"
            else:
                extra["status"] = "In-Progress"

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapters(soup, fallback_title):
        """Extract chapters from the full-work page.

        Multi-chapter works have <div id="chapter-N"> blocks with an
        inner h3.title and a div.userstuff. Single-chapter works have
        a single div.userstuff under #workskin with no chapter wrapper.
        """
        chapters = []
        numbered = soup.find_all(
            "div", id=re.compile(r"^chapter-\d+$")
        )

        if numbered:
            for idx, div in enumerate(numbered, 1):
                title_tag = div.find("h3", class_="title")
                title = (
                    title_tag.get_text(strip=True) if title_tag
                    else f"Chapter {idx}"
                )
                title = re.sub(r"^Chapter\s+\d+\s*:\s*", "", title).strip() or f"Chapter {idx}"
                userstuff = div.find("div", class_="userstuff")
                if userstuff is None:
                    continue
                # Strip the "Chapter Text" landmark AO3 injects
                for landmark in userstuff.find_all(
                    "h3", class_="landmark heading"
                ):
                    landmark.decompose()
                html = userstuff.decode_contents()
                chapters.append(Chapter(number=idx, title=title, html=html))
        else:
            # Single-chapter work
            workskin = soup.find(id="workskin")
            userstuff = workskin.find("div", class_="userstuff") if workskin else None
            if userstuff is None:
                raise ValueError(
                    "Could not locate chapter content on AO3 work page."
                )
            for landmark in userstuff.find_all(
                "h3", class_="landmark heading"
            ):
                landmark.decompose()
            chapters.append(
                Chapter(number=1, title=fallback_title, html=userstuff.decode_contents())
            )

        return chapters

    @staticmethod
    def _parse_chapter_count_from_stats(soup):
        """Extract the 'posted/planned' chapter count from dl.stats."""
        stats = soup.select_one("dl.stats")
        if not stats:
            return None
        chapters_dd = stats.find("dd", class_="chapters")
        if not chapters_dd:
            return None
        ratio = chapters_dd.get_text(strip=True)  # "4/4" or "5/?"
        match = re.match(r"(\d+)", ratio)
        return int(match.group(1)) if match else None

    def get_chapter_count(self, url_or_id):
        work_id = self.parse_story_id(url_or_id)
        # Bare work URL (no view_full_work) loads only chapter 1 + stats —
        # much cheaper than pulling the whole fic to check whether it grew.
        html = self._fetch(f"{AO3_BASE}/works/{work_id}?view_adult=true")
        soup = BeautifulSoup(html, "lxml")
        count = self._parse_chapter_count_from_stats(soup)
        if count is None:
            raise ValueError(
                f"Could not determine chapter count for AO3 work {work_id}."
            )
        return count

    def scrape_series_works(self, url):
        """Fetch an AO3 series page and return (series_name, [work_urls]).

        Works are returned in the order the author set for the series.
        AO3 paginates series at 20 works per page via ``?page=N``;
        walking `rel="next"` picks up the full list — without pagination,
        a series of 30 works would silently drop the last 10.
        """
        match = re.search(r"archiveofourown\.org/series/(\d+)", url)
        if not match:
            raise ValueError(f"Not an AO3 series URL: {url}")
        series_id = match.group(1)

        series_name = "Unknown Series"
        seen = set()
        work_urls = []
        page = 1
        while True:
            page_url = f"{AO3_BASE}/series/{series_id}?page={page}"
            html = self._fetch(page_url)
            soup = BeautifulSoup(html, "lxml")

            if page == 1:
                h2 = soup.find("h2", class_="heading")
                if h2:
                    name = h2.get_text(strip=True)
                    if name:
                        series_name = name

            new_on_page = 0
            # The series page lists works as h4.heading blocks whose
            # first link points at /works/<id>. Scoping to h4.heading
            # excludes the sidebar's "Bookmarks" / "Comments" links,
            # which also target /works/.
            for heading in soup.find_all("h4", class_="heading"):
                link = heading.find("a", href=re.compile(r"^/works/\d+"))
                if not link:
                    continue
                wid_m = re.search(r"/works/(\d+)", link["href"])
                if not wid_m:
                    continue
                wid = wid_m.group(1)
                if wid in seen:
                    continue
                seen.add(wid)
                work_urls.append(f"{AO3_BASE}/works/{wid}")
                new_on_page += 1

            next_link = soup.find("a", attrs={"rel": "next"})
            if not next_link or new_on_page == 0:
                break
            page += 1
            self._delay()

        return series_name, work_urls

    def scrape_author_stories(self, url):
        """Fetch an AO3 user works page (all pages) and return
        (author_name, [story_urls]).
        """
        # Normalise: accept /users/Name, /users/Name/works, /users/Name/pseuds/X
        match = re.search(r"archiveofourown\.org/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 user URL: {url}")
        user = match.group(1)
        author_name = user
        story_urls = []
        seen = set()
        page = 1

        while True:
            page_url = f"{AO3_BASE}/users/{user}/works?page={page}"
            html = self._fetch(page_url)
            soup = BeautifulSoup(html, "lxml")

            if page == 1:
                # Author display name from the heading, if present
                h2 = soup.find("h2", class_="heading")
                if h2:
                    heading = h2.get_text(strip=True)
                    m = re.search(r"Works by (.+)$", heading)
                    if m:
                        author_name = m.group(1).strip()

            new_on_page = 0
            for a in soup.find_all(
                "a", href=re.compile(r"^/works/\d+(?:[?#].*)?$")
            ):
                wid_m = re.search(r"/works/(\d+)", a["href"])
                if not wid_m:
                    continue
                wid = wid_m.group(1)
                if wid in seen:
                    continue
                seen.add(wid)
                story_urls.append(f"{AO3_BASE}/works/{wid}")
                new_on_page += 1

            # Stop when we've paginated past the end
            next_link = soup.find("a", attrs={"rel": "next"})
            if not next_link or new_on_page == 0:
                break
            page += 1
            self._delay()

        return author_name, story_urls

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from an AO3 user's works page.

        AO3 works-list blurbs match the shape emitted by the search
        parser, so we reuse `_parse_ao3_results` — that gives us title,
        summary, word count, chapter count, rating, status, fandom, and
        series membership for each work, with no extra HTTP calls.
        """
        from .search import _parse_ao3_results

        match = re.search(r"archiveofourown\.org/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 user URL: {url}")
        user = match.group(1)
        return self._scrape_ao3_work_list(
            f"{AO3_BASE}/users/{user}/works", fallback_name=user,
        )

    def scrape_bookmark_works(self, url):
        """Return (owner_name, [work_dict]) from an AO3 user's public
        bookmarks page. Same blurb shape as works — reuse the parser.
        """
        match = re.search(r"archiveofourown\.org/users/([\w.-]+)", url)
        if not match:
            raise ValueError(f"Not an AO3 bookmarks URL: {url}")
        user = match.group(1)
        return self._scrape_ao3_work_list(
            f"{AO3_BASE}/users/{user}/bookmarks", fallback_name=user,
        )

    def _scrape_ao3_work_list(self, base_url, *, fallback_name):
        from .search import _parse_ao3_results

        author_name = fallback_name
        works = []
        seen = set()
        page = 1

        while True:
            page_url = f"{base_url}?page={page}"
            html = self._fetch(page_url)
            if page == 1:
                soup = BeautifulSoup(html, "lxml")
                h2 = soup.find("h2", class_="heading")
                if h2:
                    heading = h2.get_text(strip=True)
                    m = re.search(r"(?:Works|Bookmarks) by (.+)$", heading)
                    if m:
                        author_name = m.group(1).strip()
            page_results = _parse_ao3_results(html)
            new_on_page = 0
            for r in page_results:
                m = re.search(r"/works/(\d+)", r.get("url", ""))
                if not m:
                    continue
                wid = m.group(1)
                if wid in seen:
                    continue
                seen.add(wid)
                r["section"] = "own" if "/works" in base_url else "bookmarks"
                r["updated"] = ""
                works.append(r)
                new_on_page += 1
            soup = BeautifulSoup(html, "lxml")
            next_link = soup.find("a", attrs={"rel": "next"})
            if not next_link or new_on_page == 0:
                break
            page += 1
            self._delay()

        return author_name, works

    @staticmethod
    def is_bookmarks_url(url):
        return bool(
            re.search(
                r"(?:archiveofourown\.org|ao3\.org)/users/[\w.-]+/bookmarks",
                str(url),
            )
        )

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        """Download an AO3 work. Skip_chapters is honoured after the
        single-page fetch so update mode and caching still work.
        When `chapters` is a list of (lo, hi) ranges (the chapter-spec
        format), only chapters in those ranges are kept.

        Update-mode optimisation: if a cached meta block exists and the
        caller claims they already have `skip_chapters` chapters, probe
        the cheap landing page (chapter 1 + stats) first to get the
        current chapter count. If it hasn't grown, return a Story with
        no chapters (caller's "no new chapters" signal) without paying
        for the full-work download.
        """
        from .models import chapter_in_spec

        chapter_spec = chapters  # don't shadow the parsed list below
        work_id = self.parse_story_id(url_or_id)
        work_url = f"{AO3_BASE}/works/{work_id}"
        full_url = f"{work_url}?view_adult=true&view_full_work=true"

        cached_meta = self._load_meta_cache(work_id)

        # Cheap probe in update mode
        if skip_chapters > 0 and cached_meta is not None:
            bare_html = self._fetch(f"{work_url}?view_adult=true")
            bare_soup = BeautifulSoup(bare_html, "lxml")
            current_count = self._parse_chapter_count_from_stats(bare_soup)
            if current_count is not None and current_count <= skip_chapters:
                return Story(
                    id=work_id,
                    title=cached_meta["title"],
                    author=cached_meta["author"],
                    summary=cached_meta["summary"],
                    url=work_url,
                    author_url=cached_meta.get("author_url", ""),
                    metadata=cached_meta.get("extra", {}),
                )

        logger.info("Fetching AO3 work %s...", work_id)
        html = self._fetch(full_url)
        fetched_soup = BeautifulSoup(html, "lxml")
        meta = self._parse_metadata(fetched_soup)
        self._save_meta_cache(work_id, meta)

        story = Story(
            id=work_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=work_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        chapters = self._parse_chapters(fetched_soup, meta["title"])
        total = len(chapters)

        for ch in chapters:
            if ch.number <= skip_chapters:
                continue
            if not chapter_in_spec(ch.number, chapter_spec):
                continue
            self._save_chapter_cache(work_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(ch.number, total, ch.title, False)

        return story
