"""Webnovel (webnovel.com) scraper.

webnovel.com — Cloudary / China Literature's English web-novel platform —
serves chapter text through a JSON API rather than in page HTML:

* ``go/pcm/chapter/getContent?bookId=<id>&chapterId=0&...`` — book metadata
  (``data.bookInfo``).
* ``go/pcm/chapter/getContent?bookId=<id>&chapterId=<cid>&...`` — one
  chapter (``data.chapterInfo`` with a ``contents`` paragraph list and an
  ``isAuth`` flag).
* ``book/<id>/catalog`` — an HTML page; the only place the full ordered
  chapter list (with chapter ids) is exposed.

Every API call needs a ``_csrfToken`` query parameter whose value the
server hands out as a cookie on any page load, so we prime it once.

Lock / paywall model
    ``chapterInfo.isAuth`` is the authoritative readable flag: ``1`` means
    the current session may read the chapter, ``0`` means it's locked
    behind coins / fast-pass. A locked chapter STILL returns a few teaser
    paragraphs in ``contents``, so content-presence is NOT a reliable lock
    test — we key off ``isAuth`` only. Logged out, only free chapters are
    ``isAuth==1``; with a logged-in session cookie, chapters the account
    has unlocked also come back ``isAuth==1``. We never spend coins — a
    locked chapter degrades to a placeholder stub, the same way the
    Wattpad scraper handles Paid-Stories parts.

Request shapes were cross-checked against lightnovel-crawler's
``sources/en/w/webnovel.py`` (github.com/lncrawl/lightnovel-crawler) and
verified live 2026-06-22. These endpoints are undocumented and drift, so
the fixture-backed tests pin the response shape this parser expects.
"""

import json
import logging
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .models import Chapter, Story, chapter_in_spec
from .scraper import BaseScraper, StoryNotFoundError

logger = logging.getLogger(__name__)

WN_BASE = "https://www.webnovel.com"
WN_CONTENT_API = f"{WN_BASE}/go/pcm/chapter/getContent"

# Constant query params the content API expects. encryptType=3 selects the
# plaintext response variant (no client-side decryption); _fsae=0 is the
# anti-bot telemetry flag the web client always sends as 0. Cross-checked
# against lightnovel-crawler; verified live 2026-06-22.
_API_ENCRYPT_TYPE = 3
_API_FSAE = 0

# webnovel.com/book/<id> or /book/<slug>_<id>; the numeric tail is the
# bookId in both shapes.
_WN_BOOK_RE = re.compile(r"webnovel\.com/book/(?:[^/?#]*_)?(\d+)", re.I)

# bookInfo.actionStatus → human status. 50 == finished (verified on a
# completed title); 30 == still updating. Unknown values fall through to
# the universal "In-Progress" default rather than guess "Complete".
_ACTION_STATUS = {30: "In-Progress", 50: "Complete"}

# chapterInfo.isAuth value that means the current session may read the body.
_AUTH_READABLE = 1

_LOCKED_NOTICE = (
    '<p class="webnovel-locked-notice"><em>'
    "This chapter is locked behind webnovel.com's coins / fast-pass "
    "paywall and was not downloaded. Unlock it in a logged-in browser "
    "and supply that session cookie to fetch it."
    "</em></p>"
)

# webnovel injects an anti-piracy boilerplate into some chapter bodies:
# a "Find authorized novels in Webnovel … for visiting." sentence and/or a
# ``<pirate>…</pirate>`` wrapper. Strip both — the Royal Road scraper does
# the equivalent for its injected anti-theft paragraphs.
_PIRATE_RE = re.compile(
    r"<pirate>.*?</pirate>"
    r"|Find authorized novels in Webnovel.*?for visiting\.",
    re.I | re.S,
)


class WebnovelLockedStoryError(Exception):
    """Raised when every requested chapter is behind the paywall."""


class WebnovelScraper(BaseScraper):
    """Scraper for webnovel.com books."""

    site_name = "webnovel"

    def __init__(self, session_cookie: str = "", **kwargs):
        # New site: stay polite and serial until parallel fetches are
        # confirmed not to trip webnovel's bot protection.
        kwargs.setdefault("delay_floor", 0.5)
        kwargs.setdefault("delay_start", 0.5)
        kwargs.setdefault("concurrency", 1)
        super().__init__(**kwargs)
        # Optional auth cookies let a logged-in user pull chapters they've
        # personally unlocked; without them only free chapters read.
        self._auth_cookies = self._parse_cookie_header(session_cookie)
        self._csrf_token = ""
        self._csrf_primed = False
        if self._auth_cookies:
            self._apply_auth_cookies(self.session)

    # ── auth / session ────────────────────────────────────────────

    @staticmethod
    def _parse_cookie_header(raw: str) -> list[tuple[str, str]]:
        """Parse a raw ``Cookie:`` header (``a=1; b=2``) into (name, value)
        pairs. Accepts the whole header string a user copies from browser
        devtools — forward-compatible if webnovel renames or adds auth
        cookies, since we seed whatever is supplied verbatim.
        """
        pairs = []
        for chunk in (raw or "").split(";"):
            chunk = chunk.strip()
            name, sep, value = chunk.partition("=")
            if sep and name.strip():
                pairs.append((name.strip(), value.strip()))
        return pairs

    def _apply_auth_cookies(self, sess) -> None:
        for name, value in self._auth_cookies:
            sess.cookies.set(
                name=name, value=value, domain=".webnovel.com", path="/",
            )

    def _new_session(self, browser=None):
        # Re-seed auth cookies onto every session the base class spins up
        # (main thread plus any parallel workers) so the logged-in state
        # isn't confined to the first session. Guard with getattr: the base
        # __init__ builds the first session before our __init__ has set the
        # attribute.
        sess = super()._new_session(browser)
        if getattr(self, "_auth_cookies", None):
            self._apply_auth_cookies(sess)
        return sess

    # ── URL parsing ───────────────────────────────────────────────

    @staticmethod
    def parse_story_id(url_or_id):
        text = str(url_or_id).strip()
        if text.isdigit():
            return int(text)
        match = _WN_BOOK_RE.search(text)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Cannot parse webnovel book ID from: {url_or_id!r}\n"
            "Expected https://www.webnovel.com/book/<id> "
            "(or /book/<slug>_<id>) or a bare numeric ID."
        )

    # ── CSRF + API ────────────────────────────────────────────────

    def _csrf_cookie(self) -> str:
        for cookie in list(self._session().cookies.jar):
            if cookie.name == "_csrfToken":
                return cookie.value or ""
        return ""

    def _ensure_csrf(self) -> str:
        """Return the ``_csrfToken``, priming it once by loading a webnovel
        page so the server sets the cookie. Re-primable: callers clear
        ``_csrf_primed`` to force a refresh after a token rejection."""
        if self._csrf_primed and self._csrf_token:
            return self._csrf_token
        self._fetch(f"{WN_BASE}/stories/novel")
        self._csrf_token = self._csrf_cookie()
        self._csrf_primed = True
        return self._csrf_token

    def _api_get_content(self, book_id, chapter_id) -> dict:
        """Call getContent for ``(book_id, chapter_id)`` and return the
        ``data`` object. ``chapterId=0`` returns book metadata. Re-primes
        the CSRF token once and retries on a non-zero response code so a
        mid-download token rotation self-heals.
        """
        for attempt in (1, 2):
            token = self._ensure_csrf()
            params = urlencode({
                "_csrfToken": token,
                "bookId": book_id,
                "chapterId": chapter_id,
                "encryptType": _API_ENCRYPT_TYPE,
                "_fsae": _API_FSAE,
            })
            body = self._fetch(f"{WN_CONTENT_API}?{params}")
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise ValueError(
                    f"webnovel returned non-JSON for book {book_id} "
                    f"chapter {chapter_id}: {body[:200]!r}"
                ) from exc
            data = payload.get("data")
            if payload.get("code") == 0 and isinstance(data, dict):
                return data
            # code != 0 is a genuine error (bad book / stale token), not a
            # lock — locked chapters still return code 0. Re-prime once.
            if attempt == 1:
                logger.debug(
                    "webnovel API code=%s (%s); re-priming csrf token",
                    payload.get("code"), payload.get("msg"),
                )
                self._csrf_primed = False
                continue
            raise StoryNotFoundError(
                f"webnovel API error for book {book_id} "
                f"(code={payload.get('code')}, msg={payload.get('msg')!r})"
            )
        raise StoryNotFoundError(f"webnovel API gave no data for book {book_id}.")

    # ── Metadata / catalog ────────────────────────────────────────

    @staticmethod
    def _build_metadata(book_info: dict) -> dict:
        """Translate webnovel's ``bookInfo`` into our Story/meta shape."""
        title = book_info.get("bookName") or "Untitled"
        author = book_info.get("authorName") or ""
        if not author:
            items = book_info.get("authorItems") or []
            author = ", ".join(x.get("name") for x in items if x.get("name"))
        summary = (book_info.get("description") or "").strip()

        extra = {}
        book_id = book_info.get("bookId")
        if book_id:
            extra["cover_url"] = (
                f"https://book-pic.webnovel.com/bookcover/{book_id}"
            )
        status = _ACTION_STATUS.get(book_info.get("actionStatus"))
        if status:
            extra["status"] = status
        if book_info.get("categoryName"):
            extra["genre"] = book_info["categoryName"]
        total = book_info.get("totalChapterNum")
        if total:
            extra["num_chapters"] = int(total)

        return {
            "title": title,
            "author": author or "Unknown Author",
            "author_url": "",
            "summary": summary,
            "extra": extra,
        }

    @staticmethod
    def _parse_catalog(html: str) -> list[dict]:
        """Return an ordered list of ``{id, title, locked}`` chapter dicts
        from the ``/catalog`` HTML page.

        ``locked`` reflects the lock icon webnovel renders on coin /
        fast-pass chapters — a cheap hint used to skip a doomed request
        when logged out. ``isAuth`` from getContent stays authoritative.
        """
        soup = BeautifulSoup(html, "lxml")
        chapters = []
        for volume in soup.select(".j_catalog_list .volume-item"):
            for li in volume.select("li"):
                cid = li.get("data-report-cid")
                anchor = li.find("a", href=True)
                if not cid or not anchor:
                    continue
                title = (
                    anchor.get("title") or anchor.get_text(strip=True) or ""
                ).strip()
                chapters.append({
                    "id": cid,
                    "title": title,
                    "locked": anchor.select_one("svg._icon") is not None,
                })
        return chapters

    # ── Chapter content ───────────────────────────────────────────

    @classmethod
    def _format_paragraph(cls, text: str) -> str:
        """Wrap one paragraph of chapter text in ``<p>``, escaping stray
        angle brackets when the text isn't already HTML, and stripping
        webnovel's injected anti-piracy boilerplate. Mirrors
        lightnovel-crawler's ``_format_content``.
        """
        if not text:
            return ""
        # Strip injected boilerplate from the raw text first: a paragraph
        # that is nothing but boilerplate collapses to "" instead of a
        # stray empty <p></p>, and the <pirate> wrapper is removed before
        # the escaping below would turn its angle brackets into entities.
        text = _PIRATE_RE.sub("", text)
        if not text.strip():
            return ""
        if "<p>" in text or "</p>" in text:
            return text.strip()  # already HTML (isRichFormat); don't re-wrap
        text = text.replace("\r", "")
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        text = "</p><p>".join(
            line.strip() for line in text.split("\n") if line.strip()
        )
        return f"<p>{text}</p>".strip()

    @classmethod
    def _chapter_html(cls, chapter_info: dict) -> str:
        """Assemble raw chapter HTML from a chapterInfo dict, or "" when no
        readable body is present."""
        contents = chapter_info.get("contents")
        if contents:
            paragraphs = [
                cls._format_paragraph(p.get("content", "")) for p in contents
            ]
        else:
            paragraphs = [cls._format_paragraph(chapter_info.get("content", ""))]
        return "".join(p for p in paragraphs if p)

    def _fetch_chapter(self, book_id, chapter_id) -> tuple[str, bool]:
        """Return ``(html, is_locked)`` for one chapter. A locked chapter
        (``isAuth != 1``) or one that returns no body degrades to the
        placeholder stub."""
        data = self._api_get_content(book_id, chapter_id)
        info = data.get("chapterInfo") or {}
        if info.get("isAuth") != _AUTH_READABLE:
            return _LOCKED_NOTICE, True
        html = self._chapter_html(info)
        if not html:
            return _LOCKED_NOTICE, True
        return html, False

    # ── Public API ────────────────────────────────────────────────

    def get_chapter_count(self, url_or_id):
        book_id = self.parse_story_id(url_or_id)
        data = self._api_get_content(book_id, 0)
        total = (data.get("bookInfo") or {}).get("totalChapterNum")
        if total:
            return int(total)
        # Fall back to the catalog when the count field is absent.
        html = self._fetch(f"{WN_BASE}/book/{book_id}/catalog")
        return len(self._parse_catalog(html))

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        book_id = self.parse_story_id(url_or_id)
        book_url = f"{WN_BASE}/book/{book_id}"
        logger.info("Fetching webnovel book %s...", book_id)

        meta_data = self._api_get_content(book_id, 0)
        book_info = meta_data.get("bookInfo") or {}
        if not book_info.get("bookName"):
            raise StoryNotFoundError(f"webnovel book {book_id} not found.")
        meta = self._build_metadata(book_info)

        chapter_list = self._parse_catalog(
            self._fetch(f"{book_url}/catalog")
        )
        if not chapter_list:
            raise StoryNotFoundError(
                f"No chapters found in webnovel catalog for book {book_id}."
            )
        num_chapters = len(chapter_list)
        meta["extra"]["num_chapters"] = num_chapters
        self._save_meta_cache(book_id, {**meta, "num_chapters": num_chapters})

        story = Story(
            id=book_id,
            title=meta["title"],
            author=meta["author"],
            summary=meta["summary"],
            url=book_url,
            author_url=meta.get("author_url", ""),
            metadata=meta["extra"],
        )

        if skip_chapters >= num_chapters:
            return story  # update mode: nothing new

        has_auth = bool(self._auth_cookies)
        locked_count = 0
        for idx, entry in enumerate(chapter_list, 1):
            if idx <= skip_chapters or not chapter_in_spec(idx, chapters):
                continue
            title = entry["title"] or f"Chapter {idx}"

            cached = self._load_chapter_cache(book_id, idx)
            if cached is not None:
                story.chapters.append(cached)
                if progress_callback:
                    progress_callback(idx, num_chapters, cached.title, True)
                continue

            # Logged out, a catalog-locked chapter can't be read — stub it
            # without spending a request. Logged in, always try: the
            # account may have unlocked it.
            if entry["locked"] and not has_auth:
                html, is_locked = _LOCKED_NOTICE, True
            else:
                self._delay()
                html, is_locked = self._fetch_chapter(book_id, entry["id"])
            if is_locked:
                locked_count += 1

            chapter = Chapter(number=idx, title=title, html=html)
            # Never cache a stub: a later authenticated run (after the user
            # unlocks the chapter) should fetch the real body.
            if not is_locked:
                self._save_chapter_cache(book_id, chapter)
            story.chapters.append(chapter)
            if progress_callback:
                progress_callback(idx, num_chapters, title, False)

        if locked_count and locked_count == len(story.chapters):
            raise WebnovelLockedStoryError(
                f"All {locked_count} requested chapters are locked behind "
                "webnovel.com's paywall. Supply a logged-in session cookie "
                "(--webnovel-cookie) to fetch chapters you've unlocked, or "
                "use --chapters to grab the free early chapters."
            )
        return story
