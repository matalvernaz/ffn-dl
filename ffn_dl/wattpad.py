"""Wattpad (wattpad.com) scraper.

Wattpad publishes each chapter as a "part" (numeric id). A story is
``/story/<id>[-<slug>]`` and each part lives at ``/<part_id>[-<slug>]``.
There is no public search page we can scrape, but Wattpad's own mobile
app uses a JSON API that is reachable without authentication:

* ``api.wattpad.com/v4/stories?query=...&limit=...``  — search
* ``api.wattpad.com/v4/parts/<id>?fields=...``        — part metadata
* ``api.wattpad.com/v4/users/<name>/stories/published`` — author stories
* ``www.wattpad.com/api/v3/story_parts/<id>``         — part→story lookup
* ``www.wattpad.com/apiv2/?m=storytext&id=<id>&page=N`` — chapter HTML

Story-level metadata (full parts list, paid flag, cover) comes from the
public story page, which embeds a JSON blob containing the same shape
the mobile app sees. We bracket-match that blob rather than parsing the
HTML with selectors — Wattpad's Next.js markup class names change
between builds.

Paid-stories program:
    Parts past the free preview return a bilingual "This story is part
    of the Paid Stories program..." stub from the storytext endpoint.
    We detect that marker and emit a placeholder chapter rather than
    silently skipping or crashing — the reader should know why the
    chapter is short.
"""

import json
import logging
import re

from bs4 import BeautifulSoup

from .models import Chapter, Story, chapter_in_spec
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

WP_BASE = "https://www.wattpad.com"
WP_API = "https://api.wattpad.com"

# Story URLs: /story/<digits>[-slug]. Part URLs: /<digits>[-slug] where
# the digits are *not* followed by /something — that would be a site
# section like /login or /stories.  We anchor on digit boundaries and
# screen known non-part path prefixes explicitly.
_WP_STORY_RE = re.compile(
    r"wattpad\.com/story/(\d+)", re.IGNORECASE,
)
_WP_PART_RE = re.compile(
    r"wattpad\.com/(\d{4,})(?:[-/?#]|$)", re.IGNORECASE,
)
_WP_USER_RE = re.compile(
    r"wattpad\.com/user/([^/?#]+)", re.IGNORECASE,
)

# Bilingual paid-story stub served on paywalled parts — both halves
# (English + Spanish) appear in the same HTML response.
_PAID_MARKER = "Paid Stories program"
_PAID_MARKER_ES = "Historias Pagadas"


def _normalise_url(text: str) -> str:
    """Strip the m. subdomain prefix and any trailing fragment/query so
    regex matches don't depend on client-side canonicalisation. Wattpad
    bounces m.wattpad.com through its own redirector, which our regexes
    don't recognise, so rewrite it to www first."""
    s = str(text).strip()
    s = re.sub(r"https?://m\.wattpad\.com", "https://www.wattpad.com", s, flags=re.I)
    return s


class WattpadPaidStoryError(Exception):
    """Raised when every requested chapter is behind the paid-stories paywall."""


class WattpadScraper(BaseScraper):
    """Scraper for wattpad.com stories."""

    site_name = "wattpad"

    def __init__(self, **kwargs):
        # Wattpad's API is permissive but we still start conservatively:
        # a half-second floor avoids hammering the text endpoint when
        # multi-page parts stack up, and keeps us friendly on 100+ part
        # stories without dragging out the common 10-part case.
        kwargs.setdefault("delay_floor", 0.5)
        kwargs.setdefault("delay_start", 0.5)
        # Per project convention: every new site launches at
        # concurrency=1 until we've confirmed parallel fetches don't
        # trigger rate-limits. Wattpad hasn't been stress-tested yet.
        kwargs.setdefault("concurrency", 1)
        super().__init__(**kwargs)

    # ── URL parsing ───────────────────────────────────────────

    @staticmethod
    def parse_story_id(url_or_id):
        """Return a numeric ID from any of these inputs:

        * ``12345`` — bare numeric id (returned as-is, assumed story)
        * ``https://www.wattpad.com/story/12345[-slug]`` — story id
        * ``https://m.wattpad.com/story/12345[-slug]`` — story id
        * ``https://www.wattpad.com/<part_id>[-slug]`` — part id

        Part and story ids live in the same numeric namespace but can't
        be disambiguated without a network call. The scraper instance
        maps part ids to their owning story inside ``download``; the
        static parser only claims that whatever it returns is a
        reachable Wattpad identifier.
        """
        text = _normalise_url(url_or_id)
        s = text.strip()
        if s.isdigit():
            return int(s)
        m = _WP_STORY_RE.search(s)
        if m:
            return int(m.group(1))
        m = _WP_PART_RE.search(s)
        if m:
            return int(m.group(1))
        raise ValueError(
            f"Cannot parse Wattpad story ID from: {url_or_id!r}\n"
            "Expected https://www.wattpad.com/story/<id> or "
            "https://www.wattpad.com/<part_id> or a bare numeric ID."
        )

    @staticmethod
    def _looks_like_part_url(url_or_id):
        """True if the input is unambiguously a part URL (``/<id>[-slug]``
        and not ``/story/<id>``). A bare integer or a story URL returns
        False — ``download`` uses this to decide whether to kick off a
        part→story resolution request."""
        text = _normalise_url(url_or_id).strip()
        if text.isdigit():
            return False
        if _WP_STORY_RE.search(text):
            return False
        return bool(_WP_PART_RE.search(text))

    @staticmethod
    def is_author_url(url):
        return bool(_WP_USER_RE.search(_normalise_url(url)))

    @staticmethod
    def is_series_url(url):  # pragma: no cover — no series concept on Wattpad
        return False

    # ── API helpers ───────────────────────────────────────────

    def _api_get_json(self, url):
        """Fetch a Wattpad JSON endpoint and parse. Raises
        StoryNotFoundError on 404/NotFound responses so callers can
        differentiate missing stories from other errors.
        """
        body = self._fetch(url)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ValueError(
                f"Wattpad returned non-JSON at {url}: {body[:200]!r}"
            ) from exc
        if isinstance(data, dict) and data.get("error_type") == "NotFound":
            raise StoryNotFoundError(
                f"Wattpad: {data.get('message') or 'not found'}"
            )
        return data

    def _resolve_part_to_story(self, part_id):
        """Look up a part's owning story via the v3 storypart endpoint."""
        url = f"{WP_BASE}/api/v3/story_parts/{part_id}?fields=groupId"
        data = self._api_get_json(url)
        group_id = data.get("groupId")
        if not group_id:
            raise StoryNotFoundError(
                f"Wattpad part {part_id} has no parent story."
            )
        return int(group_id)

    def _fetch_story_page_meta(self, story_id):
        """Fetch the public story page and bracket-parse the embedded
        story object from the server-rendered state blob. Returns the
        dict as Wattpad's app sees it, which contains the full parts
        list plus tags/cover/description/paidModel/etc.

        We don't try to walk a specific DOM path: class names in
        Wattpad's Next.js bundle rotate between builds. Instead we
        find the unique ``"paidModel":`` key and bracket-match backwards
        to the enclosing JSON object.
        """
        url = f"{WP_BASE}/story/{story_id}"
        html = self._fetch(url)

        # The server renders two objects with "paidModel" — one in a
        # related-stories card and the primary story object. The primary
        # is the largest and has ``"numParts":N`` and a full
        # ``"parts":[...]`` array; we anchor on that combination.
        obj = self._bracket_match_story(html, story_id)
        if obj is None:
            raise ValueError(
                f"Couldn't locate story JSON in {url} — "
                "Wattpad may have changed its page layout."
            )
        return obj

    @staticmethod
    def _bracket_match_story(html, story_id):
        """Find and return the primary story JSON object. The page
        contains several Wattpad story dicts (related, recommended,
        etc.); the one we want has the matching ``id`` and the longest
        ``parts`` list of any candidate."""
        target = f'"id":"{story_id}"'
        candidates = []
        search_from = 0
        while True:
            i = html.find(target, search_from)
            if i < 0:
                break
            search_from = i + len(target)

            obj_start, obj_end = _enclosing_json_object(html, i)
            if obj_start is None:
                continue
            try:
                obj = json.loads(html[obj_start:obj_end])
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            # The primary story object is the one that carries
            # ``numParts`` AND a ``parts`` list.
            if "numParts" in obj and isinstance(obj.get("parts"), list):
                candidates.append(obj)
        if not candidates:
            return None
        # Pick the largest parts list — related-story cards occasionally
        # carry a short parts preview too.
        candidates.sort(key=lambda o: len(o.get("parts") or []), reverse=True)
        return candidates[0]

    # ── Metadata / count ──────────────────────────────────────

    def get_chapter_count(self, url_or_id):
        story_id = self._resolve_story_id(url_or_id)
        # One story-page fetch gives us the full parts list, but
        # numParts is actually embedded in the <meta> / SSR payload
        # multiple times. Do a full fetch so we can reuse the same
        # Cloudflare-friendly session the download path uses.
        obj = self._fetch_story_page_meta(story_id)
        # numParts is the server-authoritative count; len(parts) is a
        # sanity check.
        n = int(obj.get("numParts") or 0)
        if not n:
            n = len(obj.get("parts") or [])
        return n

    def _resolve_story_id(self, url_or_id):
        """Normalise any input form into a numeric story id, doing a
        part→story lookup when needed."""
        parsed = self.parse_story_id(url_or_id)
        if self._looks_like_part_url(url_or_id):
            return self._resolve_part_to_story(int(parsed))
        return int(parsed)

    def _build_metadata(self, obj):
        """Translate Wattpad's story JSON into our Story/meta shape."""
        title = obj.get("title") or "Untitled"
        user = obj.get("user") or {}
        author = user.get("name") or user.get("fullname") or "Unknown Author"
        author_url = f"{WP_BASE}/user/{author}" if author and author != "Unknown Author" else ""

        # Wattpad's description is plain text with newlines — preserve
        # them as-is; exporters decide how to render paragraphs.
        summary = (obj.get("description") or "").strip()

        parts = obj.get("parts") or []
        # Drop drafts (not-yet-published) but keep ``isBlocked`` parts:
        # Wattpad sets isBlocked on paid-story chapters the current
        # (unauth) session can't read, and we want chapter numbering to
        # still match the site so readers see a placeholder instead of
        # silent gaps. The paid stub returned by the storytext endpoint
        # is detected downstream in ``_fetch_part_text``.
        fetchable_parts = [p for p in parts if not p.get("draft")]
        chapter_titles = {
            str(idx): (p.get("title") or f"Part {idx}")
            for idx, p in enumerate(fetchable_parts, 1)
        }
        num_chapters = len(fetchable_parts)

        extra = {}
        cover = obj.get("cover")
        if cover:
            extra["cover_url"] = cover
        tags = obj.get("tags") or []
        if tags:
            extra["tags"] = ", ".join(tags)
        language = (obj.get("language") or {}).get("name")
        if language:
            extra["language"] = language
        extra["status"] = "Complete" if obj.get("completed") else "In-Progress"
        if obj.get("mature"):
            extra["mature"] = True
        paid_model = obj.get("paidModel") or ""
        if paid_model:
            extra["paid_model"] = paid_model
        if obj.get("isPaywalled"):
            extra["paywalled"] = True
        length = obj.get("length")
        if length:
            # Wattpad reports ``length`` as character count, not words;
            # convert to a rough word count (avg 5 chars/word) so the
            # universal fallback in exporters has something useful.
            extra["words"] = f"{max(1, int(length) // 5):,}"
        extra["num_chapters"] = num_chapters

        return {
            "title": title,
            "author": author,
            "author_url": author_url,
            "summary": summary,
            "num_chapters": num_chapters,
            "chapter_titles": chapter_titles,
            "extra": extra,
            "_parts": fetchable_parts,
        }

    # ── Chapter fetching ──────────────────────────────────────

    def _fetch_part_text(self, part_id):
        """Fetch all pages of a part and return the concatenated HTML
        body, plus a ``paid`` flag when the paid-stories stub is the
        only thing we got back.

        The pagination is walked until an empty page is returned — the
        story_parts API exposes a ``pages`` count, but fetching that
        first would double the request count on every chapter. An
        extra empty-probe per chapter is cheaper.
        """
        pieces = []
        page = 1
        paid_detected = False
        while True:
            if page > 1:
                self._delay()
            url = (
                f"{WP_BASE}/apiv2/?m=storytext&id={part_id}&page={page}"
            )
            body = self._fetch(url)
            stripped = body.strip()
            if not stripped:
                break
            if _PAID_MARKER in body and _PAID_MARKER_ES in body and page == 1:
                # Whole-part paywall: the only content is the bilingual
                # notice. Record it as a chapter but flag it.
                paid_detected = True
                pieces.append(stripped)
                break
            pieces.append(stripped)
            # Guard: a successful mid-story page is always thousands of
            # bytes. If we somehow get a tiny non-empty response that
            # isn't the paid stub, treat it as end-of-part so we don't
            # loop forever on a malformed response.
            if len(stripped) < 64:
                break
            page += 1
            if page > 200:  # defensive: no real chapter has 200 pages
                logger.warning(
                    "Wattpad part %s: stopping after 200 pages (safety cap).",
                    part_id,
                )
                break
        html = "\n".join(pieces)
        if paid_detected:
            html = (
                '<p class="wattpad-paid-notice"><em>'
                'This chapter is part of Wattpad\'s Paid Stories program '
                'and cannot be downloaded.'
                '</em></p>' + html
            )
        return html, paid_detected

    # ── Main download ─────────────────────────────────────────

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        story_id = self._resolve_story_id(url_or_id)
        story_url = f"{WP_BASE}/story/{story_id}"

        logger.info("Fetching Wattpad story %s...", story_id)
        obj = self._fetch_story_page_meta(story_id)
        meta = self._build_metadata(obj)
        parts = meta.pop("_parts")
        num_chapters = meta["num_chapters"]
        chapter_titles = meta["chapter_titles"]

        # Don't persist the _parts list in the cached meta; it's only
        # used as a transient during this download.
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
            return story  # update mode: nothing new

        paid_count = 0
        for idx, part in enumerate(parts, 1):
            if idx <= skip_chapters:
                continue
            if not chapter_in_spec(idx, chapters):
                continue
            ch_title = chapter_titles.get(str(idx), f"Part {idx}")

            cached = self._load_chapter_cache(story_id, idx)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(idx, num_chapters, cached.title, True)
                continue

            part_id = part.get("id")
            if not part_id:
                logger.warning(
                    "Wattpad part %d of story %s has no id; skipping.",
                    idx, story_id,
                )
                continue

            # Pre-flagged paid parts from SSR JSON get a free ride to
            # the paid-handling branch — saves us a storytext fetch
            # just to rediscover the paywall, though we still do the
            # request so a paywall that's been lifted upstream (author
            # opened preview) shows the real content.
            self._delay()
            try:
                html, is_paid = self._fetch_part_text(part_id)
            except StoryNotFoundError:
                logger.warning(
                    "Wattpad part %d (%s) disappeared; skipping.",
                    idx, part_id,
                )
                continue
            if is_paid:
                paid_count += 1

            ch = Chapter(number=idx, title=ch_title, html=html)
            # Don't cache paywall stubs — if the user buys the story or
            # the site lifts the preview window later, we want a real
            # fetch next time.
            if not is_paid:
                self._save_chapter_cache(story_id, ch)
            story.chapters.append(ch)
            if progress_callback:
                progress_callback(idx, num_chapters, ch_title, False)

        if paid_count and paid_count == len(story.chapters):
            # Every requested chapter was paywalled; signal clearly.
            raise WattpadPaidStoryError(
                f"All {paid_count} requested chapters are behind Wattpad's "
                "Paid Stories paywall. Try --chapters to grab the free "
                "preview parts (usually the first 1-3)."
            )

        return story

    # ── Author pages ──────────────────────────────────────────

    def scrape_author_stories(self, url):
        """Return ``(author_name, [story_urls])`` for a Wattpad user page."""
        name = self._author_from_url(url)
        endpoint = (
            f"{WP_API}/v4/users/{name}/stories/published"
            "?fields=stories(id,title,url)&limit=100"
        )
        data = self._api_get_json(endpoint)
        stories = data.get("stories") or []
        urls = []
        for s in stories:
            story_id = s.get("id")
            if not story_id:
                continue
            urls.append(s.get("url") or f"{WP_BASE}/story/{story_id}")
        return name, urls

    def scrape_author_works(self, url):
        """Return ``(author_name, [work_dict])`` for a Wattpad user page.

        The published-stories endpoint doesn't expose rating or status
        per story so most fields come out empty. Word count is derived
        from Wattpad's character ``length`` using the same 5-char/word
        heuristic as ``_build_metadata``.
        """
        name = self._author_from_url(url)
        endpoint = (
            f"{WP_API}/v4/users/{name}/stories/published"
            "?fields=stories(id,title,url,numParts,completed,mature,length,description)"
            "&limit=100"
        )
        data = self._api_get_json(endpoint)
        works = []
        for s in data.get("stories") or []:
            story_id = s.get("id")
            if not story_id:
                continue
            length = s.get("length") or 0
            words = f"{max(1, int(length) // 5):,}" if length else ""
            works.append({
                "title": s.get("title") or f"Story {story_id}",
                "url": s.get("url") or f"{WP_BASE}/story/{story_id}",
                "author": name,
                "summary": (s.get("description") or "")[:400],
                "words": words,
                "chapters": str(s.get("numParts") or ""),
                "rating": "Mature" if s.get("mature") else "",
                "fandom": "",
                "status": "Complete" if s.get("completed") else "In-Progress",
                "updated": "",
                "section": "own",
            })
        return name, works

    @staticmethod
    def _author_from_url(url):
        m = _WP_USER_RE.search(_normalise_url(url))
        if not m:
            raise ValueError(f"Not a Wattpad user URL: {url}")
        return m.group(1)


# ── bracket-matching helper ───────────────────────────────────

def _enclosing_json_object(text, idx):
    """Return ``(start, end)`` of the innermost balanced JSON object that
    contains position ``idx``, or ``(None, None)`` if no such span exists.

    String- and escape-aware: braces inside JSON string literals don't
    affect the depth count. A forward, single-pass scan keeps a stack
    of ``{`` positions; each ``}`` pops the matching open. The first
    time a pop produces a span that covers ``idx``, we've found the
    innermost enclosing object (inner objects close before outer ones
    on a forward scan), so we can return immediately.

    The naive version this replaces counted raw braces without string
    awareness. Wattpad's SSR payload escapes braces inside strings as
    ``\\u007b`` so the naive counter held in practice, but any format
    change — user-supplied titles with raw braces, a different JSON
    serialiser — would miscount and either strand the caller or slice
    an unparseable span. This version is correct for any JSON.
    """
    stack = []
    in_string = False
    escape_next = False
    for i, c in enumerate(text):
        if in_string:
            if escape_next:
                escape_next = False
            elif c == "\\":
                escape_next = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            stack.append(i)
        elif c == "}":
            if stack:
                start = stack.pop()
                end = i + 1
                if start <= idx < end:
                    return start, end
    return None, None
