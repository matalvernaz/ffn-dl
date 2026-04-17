"""FicWad scraper — chapter discovery, metadata parsing, and download."""

import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

FICWAD_BASE = "https://ficwad.com"


class FicWadScraper(BaseScraper):
    """Scraper for ficwad.com."""

    site_name = "ficwad"

    def __init__(self, **kwargs):
        # FicWad has no Cloudflare — shorter delays are fine
        kwargs.setdefault("delay_range", (1.0, 3.0))
        super().__init__(**kwargs)

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = re.search(r"ficwad\.com/story/(\d+)", text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse FicWad story ID from: {text!r}\n"
            "Expected a URL like https://ficwad.com/story/76962 or a numeric ID."
        )

    @staticmethod
    def _parse_metadata(soup, story_id):
        """Parse title, author, summary, and extra metadata."""
        storylist = soup.find("div", class_="storylist")
        if not storylist:
            raise StoryNotFoundError(f"Story {story_id} not found on FicWad.")

        title_tag = storylist.find("h4")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Title"

        author_span = storylist.find("span", class_="author")
        author = "Unknown Author"
        author_url = ""
        if author_span:
            a_tag = author_span.find("a")
            author = a_tag.get_text(strip=True) if a_tag else author_span.get_text(strip=True)
            if author.lower().startswith("by"):
                author = author[2:].strip()
            if a_tag and a_tag.get("href"):
                author_url = FICWAD_BASE + a_tag["href"]

        summary_bq = storylist.find("blockquote", class_="summary")
        summary = summary_bq.get_text(strip=True) if summary_bq else ""

        meta_div = storylist.find("div", class_="meta")
        extra = {}
        if meta_div:
            meta_text = meta_div.get_text()
            extra["raw"] = meta_text.strip()

            cat_link = meta_div.find("a", href=re.compile(r"/category/"))
            if cat_link:
                extra["category"] = cat_link.get_text(strip=True)

            rating_match = re.search(r"Rating:\s*(\S+)", meta_text)
            if rating_match:
                extra["rating"] = rating_match.group(1)

            genre_match = re.search(r"Genres?:\s*([^-]+?)(?:\s*-|$)", meta_text)
            if genre_match:
                extra["genre"] = genre_match.group(1).strip().rstrip("-").strip()

            char_span = meta_div.find("span", class_="story-characters")
            if char_span:
                char_text = char_span.get_text(strip=True)
                char_text = re.sub(r"^Characters:\s*", "", char_text)
                extra["characters"] = char_text

            words_match = re.search(r"([\d,]+)\s+words", meta_text)
            if words_match:
                extra["words"] = words_match.group(1)

            if "Complete" in meta_text:
                extra["status"] = "Complete"

            time_spans = meta_div.find_all("span", attrs={"data-ts": True})
            if len(time_spans) >= 2:
                extra["date_published"] = int(time_spans[0]["data-ts"])
                extra["date_updated"] = int(time_spans[1]["data-ts"])
            elif len(time_spans) == 1:
                extra["date_published"] = int(time_spans[0]["data-ts"])

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _discover_chapters_from_dropdown(soup):
        """Extract chapter IDs and titles from the chapter dropdown.

        The dropdown on any chapter page lists ALL chapters with their
        actual story IDs — even those hidden by rating filters on the
        listing page.
        """
        select = soup.find("select", attrs={"name": "goto"})
        if not select:
            return []

        chapters = []
        for opt in select.find_all("option"):
            val = opt.get("value", "")
            text = opt.get_text(strip=True)
            match = re.search(r"/story/(\d+)", val)
            if not match:
                continue
            # Skip "Story Index" entry
            if text.lower().startswith("story index"):
                continue
            cid = int(match.group(1))
            # Strip leading "N. " from chapter title
            title = re.sub(r"^\d+\.\s*", "", text)
            chapters.append({"id": cid, "title": title or f"Chapter {len(chapters) + 1}"})

        return chapters

    @staticmethod
    def _parse_chapter_html(soup):
        storytext = soup.find(id="storytext")
        if not storytext:
            raise ValueError("Could not find story text on page.")
        return storytext.decode_contents()

    @staticmethod
    def is_author_url(url):
        """Return True if the URL is a FicWad author page."""
        return bool(re.search(r"ficwad\.com/a/", str(url)))

    def scrape_author_stories(self, url):
        """Fetch a FicWad author page and return (author_name, [story_urls]).

        The author page lists all stories as links matching /story/{id}.
        """
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        # Author name: FicWad author pages typically have it in <h2> or <title>
        author_name = "Unknown Author"
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            # Title format varies; try to extract the name portion
            # Common: "Stories by AuthorName - FicWad"
            if " - " in title_text:
                part = title_text.split(" - ")[0].strip()
                part = re.sub(r"^Stories\s+by\s+", "", part, flags=re.IGNORECASE)
                if part:
                    author_name = part
            elif title_text:
                author_name = title_text

        # Also try the <h2> which often has the author name
        h2 = soup.find("h2")
        if h2:
            h2_text = h2.get_text(strip=True)
            cleaned = re.sub(r"^Stories\s+by\s+", "", h2_text, flags=re.IGNORECASE)
            if cleaned:
                author_name = cleaned

        # Find all story links — they match /story/{id}
        seen_ids = set()
        story_urls = []
        for a_tag in soup.find_all("a", href=re.compile(r"/story/\d+")):
            match = re.search(r"/story/(\d+)", a_tag["href"])
            if match:
                story_id = match.group(1)
                if story_id not in seen_ids:
                    seen_ids.add(story_id)
                    story_urls.append(f"{FICWAD_BASE}/story/{story_id}")

        return author_name, story_urls

    def scrape_author_works(self, url):
        """Return (author_name, [work_dict]) from a FicWad author page.

        FicWad author-page anchors carry the story title as link text, so
        the picker can display something readable without a second fetch.
        Other fields (words, chapters, rating) would require visiting each
        story and are left blank.
        """
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            if " - " in title_text:
                part = title_text.split(" - ")[0].strip()
                part = re.sub(r"^Stories\s+by\s+", "", part, flags=re.IGNORECASE)
                if part:
                    author_name = part

        seen_ids = set()
        works = []
        for a_tag in soup.find_all("a", href=re.compile(r"/story/\d+")):
            match = re.search(r"/story/(\d+)", a_tag["href"])
            if not match:
                continue
            story_id = match.group(1)
            if story_id in seen_ids:
                continue
            seen_ids.add(story_id)
            works.append({
                "title": a_tag.get_text(strip=True) or f"Story {story_id}",
                "url": f"{FICWAD_BASE}/story/{story_id}",
                "author": "",
                "words": "",
                "chapters": "",
                "rating": "",
                "fandom": "",
                "status": "",
                "updated": "",
                "section": "own",
            })
        return author_name, works

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        page = self._fetch(f"{FICWAD_BASE}/story/{story_id}/1")
        soup = BeautifulSoup(page, "lxml")
        chapter_list = self._discover_chapters_from_dropdown(soup)
        if chapter_list:
            return len(chapter_list)
        # Single-chapter work: fall back to presence of storytext on the page
        return 1 if soup.find(id="storytext") else 0

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        from .models import chapter_in_spec

        story_id = self.parse_story_id(url_or_id)
        story_url = f"{FICWAD_BASE}/story/{story_id}"

        # Fetch the listing page for metadata
        logger.info("Fetching story metadata from FicWad...")
        listing_url = f"{story_url}/1"
        page = self._fetch(listing_url)
        soup = BeautifulSoup(page, "lxml")

        meta = self._parse_metadata(soup, story_id)

        # Discover chapters: check listing page for a chapter dropdown,
        # or look for a chapters list, or fall back to single-chapter.
        chapter_list = self._discover_chapters_from_dropdown(soup)

        if not chapter_list:
            # Listing page might itself be a single-chapter story
            # Try fetching the first visible chapter link from the listing
            chapters_div = soup.find(id="chapters")
            if chapters_div:
                first_link = chapters_div.find("a", href=re.compile(r"/story/\d+"))
                if first_link:
                    match = re.search(r"/story/(\d+)", first_link["href"])
                    if match:
                        first_id = int(match.group(1))
                        self._delay()
                        ch1_page = self._fetch(f"{FICWAD_BASE}/story/{first_id}")
                        ch1_soup = BeautifulSoup(ch1_page, "lxml")
                        chapter_list = self._discover_chapters_from_dropdown(ch1_soup)

        if not chapter_list:
            # Truly single-chapter: the story page has the content
            storytext = soup.find(id="storytext")
            if storytext:
                chapter_list = [{"id": story_id, "title": meta["title"]}]
            else:
                raise StoryNotFoundError(
                    f"No chapters found for FicWad story {story_id}."
                )

        num_chapters = len(chapter_list)
        self._save_meta_cache(story_id, {
            **meta,
            "num_chapters": num_chapters,
            "chapter_list": chapter_list,
        })

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

        for i, ch_info in enumerate(chapter_list, 1):
            if i <= skip_chapters:
                continue
            if not chapter_in_spec(i, chapters):
                continue

            ch_id = ch_info["id"]
            ch_title = ch_info["title"]

            cached = self._load_chapter_cache(story_id, i)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(i, num_chapters, cached.title, True)
                continue

            if story.chapters:
                self._delay()
            ch_url = f"{FICWAD_BASE}/story/{ch_id}"
            logger.debug("Fetching chapter %d/%d (id=%d)", i, num_chapters, ch_id)
            ch_page = self._fetch(ch_url)
            ch_soup = BeautifulSoup(ch_page, "lxml")
            html = self._parse_chapter_html(ch_soup)

            ch = Chapter(number=i, title=ch_title, html=html)
            self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(i, num_chapters, ch_title, False)

        return story
