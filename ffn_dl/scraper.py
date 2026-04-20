"""Base scraper with HTTP fetching, caching, and rate-limit handling."""

import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Optional, Union

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .models import Chapter, Story

logger = logging.getLogger(__name__)

BROWSERS = ["chrome", "chrome", "safari", "edge"]

# Rate-limit / retry tunables, centralised so they're easy to find.
INITIAL_BACKOFF_S = 30
"""Seconds to wait on the first 429/503 or connection-error retry."""

MAX_BACKOFF_S = 300
"""Upper bound on the doubling backoff — 5 min is long enough that any
server bucket has reset, short enough to not look hung."""

TIMEOUT_RETRY_SLEEP_S = 10
"""Fixed wait after a request-level timeout (not rate-limit) before retry."""

FORBIDDEN_QUICK_RETRY_S = 5
"""Initial wait on HTTP 403 — the site is usually gating a single fetch,
not rate-limiting, so quick retry with browser rotation is fine."""

CONNECTION_ERROR_JITTER_S = 5
"""Extra random jitter (0..N seconds) added to the connection-error
backoff so concurrent workers don't retry in lockstep."""

FORBIDDEN_SLOW_RETRY_S = 30
"""Wait on the last two 403 retries, paired with a browser rotation —
gives Cloudflare fingerprints time to age out."""

RATE_LIMIT_JITTER_FRAC = 0.1
"""Jitter added to each 429/503 backoff (fraction of the backoff)."""

AIMD_DECAY_FACTOR = 0.9
"""Per-success multiplicative decay toward ``delay_floor``. 10% per
chapter recovers from a rate-limit bump over ~20 fetches."""

AIMD_BUMP_FLOOR_S = 2.0
"""Minimum post-429 delay even when AIMD's current delay was zero —
'0 × 2 = 0' would otherwise strand us at floor=0 forever."""

BLOCK_CHECK_PREFIX_BYTES = 2000
"""How much of a response body to scan for Cloudflare/404 markers.
The challenge page always puts its signature in the first kilobyte;
AO3 has its own larger prefix (see ao3.py) for the adult-gate check."""

DIAGNOSTIC_BODY_PREFIX_BYTES = 300
"""How much of a non-200 body to emit in the debug diagnostic line.
Enough to catch Cloudflare challenge markers, Turnstile widgets, and
FFN's own denial templates without flooding the log."""


class RateLimitError(Exception):
    """Raised when rate-limit retries are exhausted."""


class StoryNotFoundError(Exception):
    """Raised when the story does not exist."""


class CloudflareBlockError(Exception):
    """Raised when Cloudflare blocks the request."""


def _default_cache_dir() -> Path:
    # Frozen Windows builds keep their chapter cache inside the
    # portable folder so uninstall is still "delete the folder".
    try:
        from . import portable
        if portable.is_frozen():
            path = portable.cache_dir()
            path.mkdir(parents=True, exist_ok=True)
            return path
    except Exception:
        pass
    path = Path.home() / ".cache" / "ffn-dl"
    path.mkdir(parents=True, exist_ok=True)
    return path


class BaseScraper:
    """Shared HTTP, retry, and cache logic for all site scrapers."""

    site_name = "unknown"

    def __init__(
        self,
        delay_range: Optional[tuple[float, float]] = None,
        delay_floor: float = 0.0,
        delay_start: float = 0.0,
        delay_ceiling: float = 60.0,
        max_retries: int = 5,
        timeout: int = 30,
        cache_dir: Optional[Union[str, Path]] = None,
        use_cache: bool = True,
        chunk_size: int = 0,
        chunk_delay_range: tuple[float, float] = (60.0, 75.0),
        use_wayback: bool = False,
        concurrency: int = 1,
    ) -> None:
        # Two rate-limit modes:
        #   * delay_range set → static random.uniform(*delay_range) between
        #     fetches. The CLI's --delay-min/--delay-max lands here.
        #   * delay_range None → AIMD: start at delay_start, decay 10% per
        #     successful fetch toward delay_floor, double on 429/503 up to
        #     delay_ceiling. Sites that don't rate-limit end up at floor=0.
        self.delay_range = delay_range
        self.delay_floor = max(0.0, float(delay_floor))
        self.delay_ceiling = max(self.delay_floor, float(delay_ceiling))
        self._current_delay = min(
            self.delay_ceiling,
            max(self.delay_floor, float(delay_start)),
        )
        self.max_retries = max_retries
        self.timeout = timeout
        self.use_cache = use_cache
        self.cache_dir = (
            (Path(cache_dir) if cache_dir else _default_cache_dir())
            if use_cache
            else None
        )
        self.chunk_size = chunk_size
        self.chunk_delay_range = chunk_delay_range
        self.use_wayback = use_wayback
        # Parallel chapter fetching. AIMD applies here too: we start at the
        # subclass's configured concurrency and halve it whenever a batch
        # trips a 429/503 (detected via `_current_delay` bumping up). FFN
        # stays at 1 — it captcha-bans on bulk, parallel or not.
        self.concurrency = max(1, int(concurrency))
        self._fetch_count = 0
        self._browser = "chrome"
        # curl_cffi sessions wrap a libcurl easy handle that is NOT safe to
        # share across threads. We keep one session per thread via
        # ``_tls.session`` so the same scraper can be reused by a
        # thread-pooled probe/download loop without races. ``self.session``
        # is the main-thread session and stays exposed for legacy callers
        # (and tests that monkey-patch it).
        self._tls = threading.local()
        # ``_state_lock`` guards the shared AIMD state (``_current_delay``,
        # ``_fetch_count``) and the ``_browser`` rotation. curl session
        # objects themselves are not shared, so they don't need the lock.
        self._state_lock = threading.Lock()
        self.session = curl_requests.Session(impersonate=self._browser)
        self._tls.session = self.session

    def _session(self):
        """Return the curl_cffi session for the current thread, lazily
        created on first use. Worker threads in a shared-scraper probe/
        download pool call this instead of touching ``self.session``."""
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = curl_requests.Session(impersonate=self._browser)
            self._tls.session = sess
        return sess

    def _rotate_browser(self) -> None:
        # Pick a new impersonation profile (shared state) and swap out
        # the *current thread's* session. Other threads keep their
        # existing sessions until they naturally create a fresh one —
        # rotating every thread in lockstep would throw away useful
        # HTTP/2 connection reuse on threads that weren't rate-limited.
        with self._state_lock:
            self._browser = random.choice(BROWSERS)
        new_sess = curl_requests.Session(impersonate=self._browser)
        self._tls.session = new_sess
        if threading.current_thread() is threading.main_thread():
            self.session = new_sess
        logger.debug("Rotated to browser impersonation: %s", self._browser)

    def _check_for_blocks(self, html: str) -> None:
        lower = html[:BLOCK_CHECK_PREFIX_BYTES].lower()
        if "just a moment" in lower and "cloudflare" in lower:
            raise CloudflareBlockError(
                "Cloudflare challenge detected. "
                "Try increasing delays or waiting before retrying."
            )

    def _log_fetch_diagnostic(self, resp, sess, label: str, url: str) -> None:
        """Emit a DEBUG line describing a response, for 403 root-causing.

        Captures the fields needed to tell apart cookie-jar drift,
        Cloudflare gating, and impersonation-profile mismatches:
        current browser profile, cookie-jar contents, response headers,
        and (for non-200s) a body prefix where CF challenge pages put
        their signature.
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        try:
            headers = dict(resp.headers.items())
        except Exception:
            headers = {}
        try:
            cookie_names = sorted({c.name for c in sess.cookies})
        except Exception:
            cookie_names = []
        body_prefix = ""
        if resp.status_code != 200:
            body_prefix = resp.text[:DIAGNOSTIC_BODY_PREFIX_BYTES].replace(
                "\n", " ",
            )
        logger.debug(
            "%s url=%s profile=%s status=%d jar=%s headers=%s body[:%d]=%r",
            label, url, self._browser, resp.status_code, cookie_names,
            headers, DIAGNOSTIC_BODY_PREFIX_BYTES, body_prefix,
        )

    def _try_wayback(self, url: str) -> Optional[str]:
        """Ask archive.org for the latest snapshot of `url` and return its
        HTML body, or None if nothing is archived. The Wayback toolbar
        gets injected into the page but the original DOM is preserved,
        so scraper selectors still match.
        """
        sess = self._session()
        try:
            avail = sess.get(
                f"https://archive.org/wayback/available?url={url}",
                timeout=self.timeout,
            )
            if avail.status_code != 200:
                return None
            data = avail.json()
            snap = (data.get("archived_snapshots") or {}).get("closest") or {}
            if not snap.get("available") or snap.get("status") != "200":
                return None
            snap_url = snap["url"]
            logger.info("Falling back to Wayback snapshot: %s", snap_url)
            page = sess.get(snap_url, timeout=self.timeout)
            if page.status_code == 200:
                return page.text
        except Exception as exc:
            logger.debug("Wayback fallback failed: %s", exc)
        return None

    def _fetch(self, url: str, session=None) -> str:
        """Fetch ``url`` with retry + rate-limit handling.

        Retry policy:
          * 200: success; on a success *after* a prior 429/503 we call
            ``_bump_delay_up`` so the AIMD delay reflects the new throttle.
          * 429/503: doubling backoff (``INITIAL_BACKOFF_S`` →
            ``MAX_BACKOFF_S``) with jitter, plus browser-impersonation
            rotation.
          * 404: raise ``StoryNotFoundError`` (with Wayback fallback if
            ``use_wayback``).
          * 403: short retry with browser rotation on the last two
            attempts — usually the site has fingerprinted us, and a
            fresh curl-cffi session fixes it.
          * Connection errors / timeouts: retry with the same doubling
            backoff as 429s.

        Args:
            url: Absolute URL to fetch.
            session: Optional per-request curl session. Parallel workers
                pass their own so they don't race on the shared one.

        Returns:
            The response body as text.

        Raises:
            RateLimitError: retries exhausted without a 200.
            StoryNotFoundError: upstream returned 404.
            CloudflareBlockError: a Cloudflare challenge page was served.
        """
        sess = session if session is not None else self._session()
        backoff = INITIAL_BACKOFF_S
        hit_rate_limit = False
        last_was_403 = False
        for attempt in range(self.max_retries):
            try:
                resp = sess.get(url, timeout=self.timeout)
            except curl_requests.errors.ConnectionError as exc:
                logger.warning(
                    "Connection error (attempt %d/%d): %s",
                    attempt + 1, self.max_retries, exc,
                )
                time.sleep(backoff + random.uniform(0, CONNECTION_ERROR_JITTER_S))
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            except curl_requests.errors.Timeout:
                logger.warning(
                    "Request timed out (attempt %d/%d)",
                    attempt + 1, self.max_retries,
                )
                time.sleep(TIMEOUT_RETRY_SLEEP_S)
                continue

            if resp.status_code == 200:
                self._check_for_blocks(resp.text)
                if last_was_403:
                    self._log_fetch_diagnostic(
                        sess=sess, resp=resp, url=url,
                        label="200-after-403",
                    )
                if hit_rate_limit:
                    self._bump_delay_up()
                return resp.text

            if resp.status_code in (429, 503):
                hit_rate_limit = True
                jitter = random.uniform(0, backoff * RATE_LIMIT_JITTER_FRAC)
                wait = backoff + jitter
                logger.warning(
                    "Rate limited (HTTP %d), waiting %.0fs (attempt %d/%d)",
                    resp.status_code, wait, attempt + 1, self.max_retries,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                self._rotate_browser()
                continue

            if resp.status_code == 404:
                if self.use_wayback:
                    archived = self._try_wayback(url)
                    if archived:
                        return archived
                raise StoryNotFoundError(f"Not found: {url}")

            if resp.status_code == 403:
                self._log_fetch_diagnostic(
                    sess=sess, resp=resp, url=url, label="403",
                )
                last_was_403 = True
                wait = FORBIDDEN_QUICK_RETRY_S + random.uniform(
                    0, FORBIDDEN_QUICK_RETRY_S,
                )
                if attempt >= self.max_retries - 2:
                    self._rotate_browser()
                    wait = FORBIDDEN_SLOW_RETRY_S
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
            backoff = min(backoff * 2, MAX_BACKOFF_S)

        if self.use_wayback:
            archived = self._try_wayback(url)
            if archived:
                logger.warning("Live site failed; served from Wayback.")
                return archived
        raise RateLimitError(f"Failed after {self.max_retries} retries: {url}")

    def _delay(self) -> None:
        with self._state_lock:
            self._fetch_count += 1
            chunk_hit = (
                self.chunk_size
                and self._fetch_count % self.chunk_size == 0
            )
            current = self._current_delay
            if not chunk_hit and self.delay_range is None:
                # AIMD: decay the shared counter *before* sleeping so
                # concurrent workers see the updated value immediately.
                self._current_delay = max(
                    self.delay_floor, current * AIMD_DECAY_FACTOR,
                )
        if chunk_hit:
            wait = random.uniform(*self.chunk_delay_range)
            logger.info(
                "Pausing %.0fs after %d chapters to stay under rate limits...",
                wait, self.chunk_size,
            )
            time.sleep(wait)
            return
        if self.delay_range is not None:
            time.sleep(random.uniform(*self.delay_range))
            return
        if current > 0:
            jitter = random.uniform(0, current * 0.2)
            time.sleep(current + jitter)

    def _bump_delay_up(self) -> None:
        """AIMD multiplicative increase after a rate-limit hit."""
        with self._state_lock:
            prev = self._current_delay
            new_delay = max(prev * 2, AIMD_BUMP_FLOOR_S)
            self._current_delay = min(self.delay_ceiling, new_delay)
            bumped = self._current_delay != prev
            current = self._current_delay
        if bumped:
            logger.info(
                "Rate-limit recovery: raising per-fetch delay %.1fs → %.1fs",
                prev, current,
            )

    def _fetch_parallel(self, urls: list[str]) -> list[str]:
        """Fetch a list of URLs concurrently, returning bodies in input order.

        Uses a thread pool sized to ``self.concurrency`` (each worker
        gets its own curl session so concurrent libcurl handles don't
        race). Falls through to sequential ``_fetch`` calls when
        concurrency is 1 or only one URL is provided, so subclasses
        can call this unconditionally.

        AIMD concurrency control: after each batch, compare the scraper's
        ``_current_delay`` to its value before the batch — if it rose,
        some request got a 429/503 and triggered ``_bump_delay_up``, so
        we halve the pool size for the next batch. This mirrors the
        per-request AIMD delay shape one layer up: multiplicative
        decrease on throttle, no explicit recovery (subsequent batches
        stay at the reduced concurrency until the scraper is re-created).
        FFN uses concurrency=1 regardless because it captcha-bans on
        bulk regardless of pacing.

        Args:
            urls: Chapter / resource URLs to fetch, in order.

        Returns:
            List of response bodies indexed parallel to ``urls``.
        """
        if not urls:
            return []
        if self.concurrency <= 1 or len(urls) == 1:
            return [self._fetch(u) for u in urls]

        import concurrent.futures

        results = [None] * len(urls)
        concurrency = self.concurrency
        i = 0
        while i < len(urls):
            batch = urls[i:i + concurrency]
            delay_before = self._current_delay

            def fetch_one(url):
                # Each worker gets its own session so concurrent libcurl
                # handles don't race on the shared one.
                session = curl_requests.Session(impersonate=self._browser)
                return self._fetch(url, session=session)

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(batch),
            ) as executor:
                future_to_local = {
                    executor.submit(fetch_one, u): local_i
                    for local_i, u in enumerate(batch)
                }
                for future in concurrent.futures.as_completed(future_to_local):
                    local_i = future_to_local[future]
                    results[i + local_i] = future.result()

            if self._current_delay > delay_before and concurrency > 1:
                concurrency = max(1, concurrency // 2)
                logger.info(
                    "Parallel fetch backing off to concurrency=%d "
                    "after rate-limit response.", concurrency,
                )
            i += len(batch)

        return results

    # ── Cache ─────────────────────────────────────────────────────

    def _story_cache_dir(self, story_id) -> Optional[Path]:
        if not self.use_cache:
            return None
        d = self.cache_dir / f"{self.site_name}_{story_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_meta_cache(self, story_id, meta: dict) -> None:
        if not self.use_cache:
            return
        path = self._story_cache_dir(story_id) / "meta.json"
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _load_meta_cache(self, story_id) -> Optional[dict]:
        if not self.use_cache:
            return None
        path = self._story_cache_dir(story_id) / "meta.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            logger.warning("Corrupt meta cache %s (%s); will refetch", path, exc)
            path.unlink(missing_ok=True)
            return None

    def _save_chapter_cache(self, story_id, chapter: Chapter) -> None:
        if not self.use_cache:
            return
        path = self._story_cache_dir(story_id) / f"ch_{chapter.number:04d}.html"
        path.write_text(
            json.dumps({"title": chapter.title, "html": chapter.html}),
            encoding="utf-8",
        )

    def _load_chapter_cache(self, story_id, chap_num: int) -> Optional[Chapter]:
        if not self.use_cache:
            return None
        path = self._story_cache_dir(story_id) / f"ch_{chap_num:04d}.html"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Chapter(number=chap_num, title=data["title"], html=data["html"])
        except (ValueError, UnicodeDecodeError, OSError, KeyError) as exc:
            logger.warning("Corrupt chapter cache %s (%s); will refetch", path, exc)
            path.unlink(missing_ok=True)
            return None

    def clean_cache(self, story_id) -> None:
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

    @staticmethod
    def is_author_url(url):
        """True if ``url`` is an author / user profile page on this site.

        Default implementation returns False — scrapers that want the
        CLI and GUI to offer "download all stories by this author"
        should override with a site-specific check."""
        return False

    @staticmethod
    def is_series_url(url):
        """True if ``url`` is a series / universe page grouping multiple
        stories. Default False — AO3, Literotica, and StoriesOnline
        override; the rest have no series concept."""
        return False

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        raise NotImplementedError

    def get_chapter_count(self, url_or_id):
        """Return the current chapter count on the site in one cheap request.

        Used by update-mode to decide whether to fetch anything else.
        Subclasses must override with a site-specific implementation that
        does not pull full chapter bodies.
        """
        raise NotImplementedError


# ── FFN ───────────────────────────────────────────────────────────

FFN_BASE = "https://www.fanfiction.net"

_FFN_RATING_ID_TO_LABEL = {
    "1": "K", "2": "K+", "3": "T", "4": "M",
}

_FFN_CHAP_SELECT_RE = re.compile(
    r'<select[^>]*\bid=["\']?chap_select["\']?[^>]*>(.*?)</select>',
    re.IGNORECASE | re.DOTALL,
)
_FFN_OPTION_RE = re.compile(r"<option\b", re.IGNORECASE)


def _ffn_chapter_count_from_select(html: str) -> Optional[int]:
    """Cheap regex probe for FFN's chapter count.

    Scans for the ``chap_select`` dropdown and counts its ``<option>``
    tags. Returns ``None`` when the dropdown is absent so callers fall
    back to the full metadata parse — single-chapter works don't render
    a dropdown, and any future markup change should also degrade safely.
    """
    match = _FFN_CHAP_SELECT_RE.search(html)
    if not match:
        return None
    count = len(_FFN_OPTION_RE.findall(match.group(1)))
    return count if count > 0 else None


def _ffn_row_to_work(row, story_id, section):
    """Build a detailed work dict from an FFN author-page z-list row.

    The row carries data-* attributes we can lift without reparsing the
    meta text. Author name falls back to empty for own-stories rows —
    callers know the author from the page — and to the /u/ link text
    for favorites.
    """
    import datetime as _dt

    def _parse_epoch(val):
        if not val:
            return ""
        try:
            return _dt.datetime.fromtimestamp(
                int(val), tz=_dt.timezone.utc,
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return ""

    status_id = row.get("data-statusid") or ""
    status = "Complete" if status_id == "2" else "In-Progress"

    stitle = row.find("a", class_="stitle")
    title = row.get("data-title") or (
        stitle.get_text(" ", strip=True) if stitle else ""
    )

    author = ""
    if section == "favorites":
        u_tag = row.find("a", href=re.compile(r"^/u/\d+"))
        if u_tag:
            author = u_tag.get_text(strip=True)

    meta_text = ""
    meta_div = row.find("div", class_="z-padtop2")
    if meta_div:
        meta_text = meta_div.get_text(" ", strip=True)
    rating = ""
    if meta_text:
        m = re.search(r"Rated:\s*(\S+)", meta_text)
        if m:
            rating = m.group(1)
    if not rating:
        rating = _FFN_RATING_ID_TO_LABEL.get(row.get("data-ratingid", ""), "")

    # Summary lives in z-padtop with the meta div (z-padtop2) nested
    # inside it. Walk the top-level children and skip the meta div so
    # we get just the blurb text.
    summary = ""
    summary_div = row.find("div", class_="z-padtop")
    if summary_div:
        parts = []
        for child in summary_div.children:
            if getattr(child, "get", None) and "z-padtop2" in (
                child.get("class") or []
            ):
                continue
            if hasattr(child, "get_text"):
                text = child.get_text(" ", strip=True)
            else:
                text = str(child).strip()
            if text:
                parts.append(text)
        summary = " ".join(parts).strip()

    return {
        "title": title,
        "url": f"{FFN_BASE}/s/{story_id}",
        "author": author,
        "summary": summary,
        "words": row.get("data-wordcount", "") or "",
        "chapters": row.get("data-chapters", "") or "1",
        "rating": rating,
        "fandom": row.get("data-category", "") or "",
        "status": status,
        "updated": _parse_epoch(row.get("data-dateupdate")),
        "section": section,
    }


class FFNScraper(BaseScraper):
    """Scraper for fanfiction.net."""

    site_name = "ffn"

    def __init__(self, **kwargs):
        # Match FanFicFare's defaults.ini for www.fanfiction.net:
        # `slow_down_sleep_time: 6` applied to every request, jittered.
        # A steady ~6s/chapter is what's been proven safe against
        # Cloudflare for 10+ years; the old "fast-burst then 60s pause"
        # pattern is closer to what bot-detection actually flags.
        kwargs.setdefault("chunk_size", 0)
        kwargs.setdefault("delay_floor", 6.0)
        kwargs.setdefault("delay_start", 6.0)
        super().__init__(**kwargs)

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

        FFN author pages have separate sections: #st_inside (the author's
        own stories), #fs_inside (favourite stories), and #fa (favourite
        authors). Scope the search to the own-stories section so we don't
        accidentally download the author's favourites — a recurring
        complaint in the downloader community.
        """
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            if "|" in title_text:
                author_name = title_text.split("|")[0].strip()

        # Preferred: scope to the own-stories container.
        own_container = soup.find("div", id="st_inside")
        container = own_container or soup

        seen_ids = set()
        story_urls = []
        for a_tag in container.find_all("a", href=re.compile(r"^/s/\d+")):
            match = re.search(r"/s/(\d+)", a_tag["href"])
            if match:
                story_id = match.group(1)
                if story_id not in seen_ids:
                    seen_ids.add(story_id)
                    story_urls.append(f"{FFN_BASE}/s/{story_id}")

        return author_name, story_urls

    def scrape_author_works(self, url, *, include_favorites=False):
        """Fetch an FFN author page and return (author_name, [work_dict]).

        Each dict has keys: title, url, author, words, chapters, rating,
        fandom, status, updated (YYYY-MM-DD from data-dateupdate), section
        ("own" or "favorites"). FFN's author-page rows carry these as
        data-* attributes — no extra HTTP calls.

        When include_favorites is True, rows from #fs_inside are appended
        after the author's own stories, tagged with section="favorites".
        """
        html = self._fetch(url)
        soup = BeautifulSoup(html, "lxml")

        author_name = "Unknown Author"
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            if "|" in title_text:
                author_name = title_text.split("|")[0].strip()

        sections = [("st_inside", "own")]
        if include_favorites:
            sections.append(("fs_inside", "favorites"))

        works = []
        seen_ids = set()
        for container_id, section in sections:
            container = soup.find("div", id=container_id)
            if not container:
                continue
            for row in container.find_all("div", class_="z-list"):
                story_id = row.get("data-storyid")
                if not story_id or story_id in seen_ids:
                    continue
                seen_ids.add(story_id)
                works.append(_ffn_row_to_work(row, story_id, section))
        return author_name, works

    def get_chapter_count(self, url_or_id):
        story_id = self.parse_story_id(url_or_id)
        page = self._fetch(f"{FFN_BASE}/s/{story_id}/1")
        count = _ffn_chapter_count_from_select(page)
        if count is not None:
            return count
        # Fallback: full soup parse. Reached when chap_select is absent
        # (single-chapter work) or FFN changes the markup.
        soup = BeautifulSoup(page, "lxml")
        meta = self._parse_metadata(soup)
        return meta["num_chapters"]

    def download(self, url_or_id, progress_callback=None, skip_chapters=0, chapters=None):
        """Download a story. If skip_chapters > 0, only fetch metadata
        and chapters beyond that count (for update mode).
        If chapters is a list of (lo, hi) tuples, only fetch chapters
        that fall inside one of the ranges."""
        from .models import chapter_in_spec

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
        if skip_chapters < 1 and chapter_in_spec(1, chapters):
            html = self._parse_chapter_html(soup)
            ch1_title = chapter_titles.get("1", "Chapter 1")
            ch1 = Chapter(number=1, title=ch1_title, html=html)
            self._save_chapter_cache(story_id, ch1)
            story.chapters.append(ch1)
            if progress_callback:
                progress_callback(1, num_chapters, ch1_title, False)

        for chap_num in range(max(2, skip_chapters + 1), num_chapters + 1):
            if not chapter_in_spec(chap_num, chapters):
                continue
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
