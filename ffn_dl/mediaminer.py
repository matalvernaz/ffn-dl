"""MediaMiner (mediaminer.org) scraper.

MediaMiner is one of the older multi-fandom fanfiction archives (heavy
on anime/manga). Most open-source downloaders dropped it or never
covered it — FFF has an adapter that's often broken because MediaMiner
redesigns periodically. Structure as of the current layout:

* Story:   https://www.mediaminer.org/fanfic/view_st.php/<sid>
           https://www.mediaminer.org/fanfic/s/<cat>/<slug>/<sid>
* Chapter: https://www.mediaminer.org/fanfic/c/<cat>/<slug>/<sid>/<cid>
* Author:  https://www.mediaminer.org/fanfic/src.php/u/<name>
"""

import logging
import re

from bs4 import BeautifulSoup

from .models import Story
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

MM_BASE = "https://www.mediaminer.org"

# Glyphs MediaMiner has used (and likely would use) as a breadcrumb
# separator between fandom and story title. ❯ is the current one
# (HEAVY RIGHT-POINTING ANGLE-BRACKET ORNAMENT); the others are
# defensive — a font/CSS refresh could swap them in without warning.
_MM_BREADCRUMB_SEPARATORS = "❯›→»>"
"""Characters treated as fandom/title separators in the page H1.

* ``\\u276F`` — HEAVY RIGHT-POINTING ANGLE BRACKET ORNAMENT (current).
* ``\\u203A`` — SINGLE RIGHT-POINTING ANGLE QUOTATION MARK.
* ``\\u2192`` — RIGHTWARDS ARROW.
* ``\\u00BB`` — RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK.
* ``>``      — the ASCII-safe ``>`` chevron, just in case.
"""

_MM_CHAPTER_LABEL_RE = re.compile(
    r"(?:Chapter|Ch\.?)\s+\d+\S*", re.IGNORECASE,
)
"""Extract the ``Chapter N``/``Ch. N`` slug from a chapter anchor's
text. Fall back to the full cleaned label when the regex misses so
named chapters (``"The Beginning"``) still make it through."""


def _split_mm_breadcrumb_title(raw_title: str) -> tuple[str, str]:
    """Split an ``h1#post-title`` breadcrumb into ``(title, category)``.

    MediaMiner's current H1 looks like ``"Anime/Manga ❯ Story Name"``,
    where ❯ is U+276F. We split on any glyph in
    :data:`_MM_BREADCRUMB_SEPARATORS` so a font/CSS refresh that swaps
    it for a similar-looking chevron doesn't silently leave the fandom
    stuck to the title. Empty segments (``"❯ Story"`` or ``"Fandom ❯"``)
    are discarded.

    Returns the full ``raw_title`` as the title and an empty category
    when no separator is found.
    """
    pattern = f"[{re.escape(_MM_BREADCRUMB_SEPARATORS)}]"
    parts = [p.strip() for p in re.split(pattern, raw_title) if p.strip()]
    if not parts:
        return raw_title.strip(), ""
    if len(parts) == 1:
        # Either no separator, or a stray leading/trailing separator
        # (``"Fandom ❯"`` / ``"❯ Story"``). Either way the sole segment
        # is what we want as the title — not the raw glyph-bearing
        # string, which would leak the separator into the EPUB title.
        return parts[0], ""
    # Last segment is the story title; everything prior is fandom hierarchy.
    return parts[-1], " / ".join(parts[:-1])


class MediaMinerScraper(BaseScraper):
    """Scraper for mediaminer.org fanfiction."""

    site_name = "mediaminer"

    def __init__(self, **kwargs):
        # Fetch chapters in parallel by default; AIMD halves this on any
        # rate-limit response from the server.
        kwargs.setdefault("concurrency", 3)
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        # /fanfic/view_st.php/<sid>
        match = re.search(r"mediaminer\.org/fanfic/view_st\.php/(\d+)", text)
        if match:
            return int(match.group(1))
        # /fanfic/s/<cat>/<slug>/<sid>
        match = re.search(
            r"mediaminer\.org/fanfic/s/[^?#]+?/(\d+)(?:[/?#]|$)", text
        )
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse MediaMiner story ID from: {text!r}\n"
            "Expected a URL like https://www.mediaminer.org/fanfic/view_st.php/<id> "
            "or /fanfic/s/<category>/<slug>/<id>."
        )

    @staticmethod
    def is_author_url(url):
        return bool(
            re.search(r"mediaminer\.org/fanfic/src\.php/u/[\w.-]+", str(url))
            or re.search(r"mediaminer\.org/user_info\.php/\d+", str(url))
        )

    @staticmethod
    def _parse_metadata(soup, story_id):
        article = soup.find("article")
        if not article:
            raise StoryNotFoundError(
                f"MediaMiner story {story_id} not found (no <article>)."
            )

        h1 = article.find("h1", id="post-title")
        raw_title = h1.get_text(" ", strip=True) if h1 else f"Story {story_id}"
        title, category = _split_mm_breadcrumb_title(raw_title)

        meta_div = article.find("div", class_="post-meta")
        author = "Unknown Author"
        author_url = ""
        summary = ""
        extra = {"category": category} if category else {}

        if meta_div:
            author_link = meta_div.find(
                "a", href=re.compile(r"/user_info\.php/\d+")
            )
            if author_link:
                author = author_link.get_text(strip=True)
                href = author_link["href"]
                author_url = MM_BASE + href if href.startswith("/") else href

            # Summary: text nodes between the author <br> and the first
            # <b>Anime/Manga:</b>-style label. Walk the direct children
            # of meta_div and gather free text before hitting a known
            # label.
            collecting = False
            summary_parts = []
            for child in meta_div.children:
                name = getattr(child, "name", None)
                if name == "br" and collecting:
                    summary_parts.append(" ")
                    continue
                if name is None:
                    text = str(child).strip()
                    if text:
                        if collecting:
                            summary_parts.append(text)
                elif name == "a" and child is author_link:
                    collecting = True
                elif name == "b":
                    label = child.get_text(strip=True).rstrip(":").lower()
                    if label in (
                        "anime/manga", "books", "movies", "tv shows", "genre(s)",
                        "genre", "type", "uploaded on", "pages", "words",
                        "visits", "status", "chapters", "rating",
                    ):
                        break
            summary = re.sub(r"\s+", " ", "".join(summary_parts)).strip()

            # Labelled metadata fields
            meta_text = meta_div.get_text(" ", strip=True)
            for label, key in [
                ("Words", "words"),
                ("Status", "status"),
                ("Pages", "pages"),
                ("Uploaded On", "published"),
                ("Visits", "visits"),
            ]:
                match = re.search(
                    rf"{re.escape(label)}:\s*([^|]+)", meta_text
                )
                if match:
                    value = match.group(1).strip()
                    if key == "status":
                        extra["status"] = (
                            "Complete" if value.lower().startswith("complet")
                            else value
                        )
                    else:
                        extra[key] = value

            genre_links = meta_div.find_all(
                "a", href=re.compile(r"/fanfic/src\.php/g/\d+")
            )
            if genre_links:
                extra["genre"] = ", ".join(
                    a.get_text(strip=True) for a in genre_links
                )

            rating_div = article.find("div", id="post-rating")
            if rating_div:
                rating_text = rating_div.get_text(strip=True)
                rating_match = re.search(r"\[\s*([A-Z][^-\]]*)", rating_text)
                if rating_match:
                    extra["rating"] = rating_match.group(1).strip()

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _parse_chapter_list(soup):
        """Extract chapter links in appearance order from the story page."""
        article = soup.find("article")
        if not article:
            return []
        chapters = []
        seen = set()
        for a in article.find_all(
            "a", href=re.compile(r"/fanfic/c/[^?#]+?/\d+/\d+(?:/|$)")
        ):
            href = a["href"]
            match = re.search(r"/fanfic/c/([^?#]+?)/(\d+)/(\d+)", href)
            if not match:
                continue
            cid = match.group(3)
            if cid in seen:
                continue
            seen.add(cid)
            label = a.get_text(" ", strip=True)
            # Label shapes we've seen: "Story Title Chapter N ( Chapter N )",
            # "Chapter 1 - Awakening", bare "Chapter 12", or the occasional
            # "Ch. 3". Strip the parenthesised self-reference, pull the
            # Chapter-N slug if present, and fall back to the whole label
            # for named-chapter sites so titles like "The Beginning"
            # still land in the metadata.
            clean = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
            chapter_m = _MM_CHAPTER_LABEL_RE.search(clean)
            title = (
                chapter_m.group(0) if chapter_m
                else (clean or f"Chapter {len(chapters) + 1}")
            )
            full_url = MM_BASE + href if href.startswith("/") else href
            chapters.append({"id": int(cid), "url": full_url, "title": title})
        return chapters

    @staticmethod
    def _parse_chapter_html(soup):
        body = soup.find("div", id="fanfic-text")
        if body is None:
            raise ValueError("Could not locate #fanfic-text on MediaMiner page.")
        return body.decode_contents()

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        html = self._fetch(f"{MM_BASE}/fanfic/view_st.php/{story_id}")
        soup = BeautifulSoup(html, "lxml")
        return len(self._parse_chapter_list(soup))

    def scrape_author_stories(self, url):
        match = re.search(r"/user_info\.php/(\d+)", str(url))
        if match:
            # /user_info.php/<uid> redirects to /fanfic/src.php/u/<name>
            html = self._fetch(str(url))
            soup = BeautifulSoup(html, "lxml")
            name_link = soup.find("a", href=re.compile(r"/fanfic/src\.php/u/"))
            if name_link:
                url = MM_BASE + name_link["href"]
                html = self._fetch(url)
                soup = BeautifulSoup(html, "lxml")
        else:
            html = self._fetch(str(url))
            soup = BeautifulSoup(html, "lxml")

        # Author display name from the heading
        author_name = "Unknown Author"
        h = soup.find(["h1", "h2", "h3"])
        if h:
            txt = h.get_text(strip=True)
            if txt:
                author_name = re.sub(r"^Fan Fiction by\s+", "", txt, flags=re.I) or txt

        # Pattern matches both /fanfic/s/<cat>/<slug>/<sid> forms and
        # /fanfic/view_st.php/<sid>.
        seen = set()
        story_urls = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m1 = re.search(r"/fanfic/view_st\.php/(\d+)", href)
            m2 = re.search(r"/fanfic/s/[^?#]+?/(\d+)(?:/|$)", href)
            sid = (m1.group(1) if m1 else None) or (m2.group(1) if m2 else None)
            if sid and sid not in seen:
                seen.add(sid)
                story_urls.append(f"{MM_BASE}/fanfic/view_st.php/{sid}")
        return author_name, story_urls

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from a MediaMiner user page.

        MediaMiner's listings don't carry much per-row metadata; we take
        the title from the link text and leave word/chapter counts blank.
        """
        name, urls = self.scrape_author_stories(url)
        html = self._fetch(str(url))
        soup = BeautifulSoup(html, "lxml")
        titles_by_id = {}
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m1 = re.search(r"/fanfic/view_st\.php/(\d+)", href)
            m2 = re.search(r"/fanfic/s/[^?#]+?/(\d+)(?:/|$)", href)
            sid = (m1.group(1) if m1 else None) or (m2.group(1) if m2 else None)
            if not sid or sid in titles_by_id:
                continue
            text = a.get_text(strip=True)
            if text:
                titles_by_id[sid] = text
        works = []
        for u in urls:
            sid_m = re.search(r"view_st\.php/(\d+)", u)
            sid = sid_m.group(1) if sid_m else ""
            works.append({
                "title": titles_by_id.get(sid, f"Story {sid}"),
                "url": u,
                "author": name,
                "words": "",
                "chapters": "",
                "rating": "",
                "fandom": "",
                "status": "",
                "updated": "",
                "section": "own",
            })
        return name, works

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        story_id = self.parse_story_id(url_or_id)
        story_url = f"{MM_BASE}/fanfic/view_st.php/{story_id}"

        logger.info("Fetching MediaMiner story %s...", story_id)
        html = self._fetch(story_url)
        soup = BeautifulSoup(html, "lxml")

        meta = self._parse_metadata(soup, story_id)
        chapter_list = self._parse_chapter_list(soup)

        if not chapter_list:
            # Single-chapter story: the "chapter" is the story page itself.
            # Follow the "Read" link if present — MediaMiner still renders
            # the chapter body on a /fanfic/c/ URL even for oneshots.
            read_link = soup.find("a", href=re.compile(r"/fanfic/c/"))
            if read_link:
                full = read_link["href"]
                match = re.search(r"/(\d+)$", full.split("?")[0])
                if match:
                    chapter_list = [{
                        "id": int(match.group(1)),
                        "url": MM_BASE + full if full.startswith("/") else full,
                        "title": meta["title"],
                    }]
        if not chapter_list:
            raise StoryNotFoundError(
                f"No chapters found for MediaMiner story {story_id}."
            )

        self._save_meta_cache(story_id, {
            **meta, "num_chapters": len(chapter_list),
        })

        story = Story(
            id=story_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=story_url,
            author_url=meta.get("author_url", ""),
            metadata=meta.get("extra", {}),
        )

        story.chapters.extend(self._materialise_chapters(
            story_id=story_id,
            chapter_list=chapter_list,
            skip_chapters=skip_chapters,
            chapter_spec=chapters,
            parse_chapter=self._parse_chapter_html,
            progress_callback=progress_callback,
        ))
        return story
