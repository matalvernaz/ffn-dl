"""Unified "Erotic Story Search" — fans out across every erotica site.

The existing per-site SearchFrame pattern (see :mod:`ffn_dl.gui_search`)
gives one window per site. Erotica is different: the user's primary
axis is **the kink/tag**, not the site, so a single search window that
queries every erotica archive at once and returns merged results gives
a better experience than eight sidebar entries.

Public entry points:

* ``search_erotica(query, sites=None, tags=None, ...)`` — fan-out to
  every registered erotica site in parallel, merge the results, tag
  each row with its origin ``site`` so the GUI's "Site" column can
  display where each hit came from.
* ``EROTICA_SITE_SLUGS`` / ``EROTICA_TAG_VOCABULARY`` — metadata the
  GUI binds its dropdowns / tag picker to.

Per-site search functions here are deliberately small — Literotica's
native search already lives in :mod:`ffn_dl.search` (imported below),
and the newer sites all expose a category or tag URL we can parse as
a result list without a real search API. Nothing here tries to be a
full-blown search engine: the point is to give the user a one-stop
discovery surface over all eight archives, not to replicate each
site's native filter set.

Tag search is a first-class input (per user feedback — see
``feedback_erotica_search.md`` in auto-memory). The unified vocabulary
below is the *intersection* of tags that appear meaningfully on ≥3 of
the 8 sites; niche per-site tags still work as free-text entries.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from typing import Callable, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from ..search import search_literotica

logger = logging.getLogger(__name__)

PER_SITE_LIMIT = 8
"""Cap the per-site result batch that fan-out pulls per page. Eight
rows × eight sites gives a first page of ~64 results, which is plenty
for a single view and keeps each site's scrape cheap."""

REQUEST_TIMEOUT_S = 25

EROTICA_SITE_SLUGS: list[str] = [
    "all",
    "literotica",
    "aff",
    "storiesonline",
    "nifty",
    "sexstories",
    "mcstories",
    "lushstories",
    "fictionmania",
]
"""Site-picker options for the unified search window. The first entry
(``all``) triggers fan-out; everything else scopes to a single site."""

EROTICA_SITE_LABELS: dict[str, str] = {
    "all": "All erotica sites",
    "literotica": "Literotica",
    "aff": "Adult-FanFiction.org",
    "storiesonline": "StoriesOnline",
    "nifty": "Nifty",
    "sexstories": "SexStories",
    "mcstories": "MCStories",
    "lushstories": "Lushstories",
    "fictionmania": "Fictionmania",
}

EROTICA_TAG_VOCABULARY: list[str] = [
    # The cross-site common denominator — every tag here appears on
    # at least three of the eight registered sites, so picking one is
    # a predictable way to narrow results. Site-specific kinks can
    # still be entered as free-text in the tag box.
    "anal",
    "bdsm",
    "bondage",
    "bukkake",
    "celebrity",
    "cheating",
    "chastity",
    "cuckold",
    "dominance-submission",
    "exhibitionism",
    "femdom",
    "feet",
    "fisting",
    "futanari",
    "gangbang",
    "gay",
    "group-sex",
    "harem",
    "humiliation",
    "hypnosis",
    "incest",
    "interracial",
    "lactation",
    "lesbian",
    "masturbation",
    "mature",
    "mind-control",
    "non-consent",
    "oral",
    "orgy",
    "polyamory",
    "pregnancy",
    "public-sex",
    "roleplay",
    "rough",
    "spanking",
    "swinging",
    "teen",
    "threesome",
    "transgender",
    "voyeur",
    "watersports",
]
"""Tags exposed to the GUI multi-picker. Kept lowercase and
dash-joined so they drop straight into URL paths like
``/stories/bytag/<tag>``."""


# ── HTTP helper ──────────────────────────────────────────────────

def _fetch(url: str, *, timeout: int = REQUEST_TIMEOUT_S) -> Optional[str]:
    """Plain fetch with curl_cffi browser impersonation.

    Used by the index/category scrapers below — the heavy retrying
    BaseScraper fetch isn't needed for a one-shot search, and the
    fan-out caller wraps every call in a per-site timeout already.
    Returns None on non-200 so fan-out degrades gracefully when one
    site is offline instead of aborting the whole search.
    """
    try:
        sess = curl_requests.Session(impersonate="chrome")
        resp = sess.get(url, timeout=timeout)
        if resp.status_code != 200:
            logger.debug("search fetch %s → HTTP %d", url, resp.status_code)
            return None
        return resp.text
    except Exception as exc:
        logger.debug("search fetch %s failed: %s", url, exc)
        return None


def _matches_query(query: str, *fields: str) -> bool:
    """Case-insensitive substring match used for client-side filtering
    of tag/category listings. Empty ``query`` returns True so tag-only
    browses show every row."""
    if not query:
        return True
    q = query.lower()
    for field in fields:
        if field and q in field.lower():
            return True
    return False


# ── Per-site searches ────────────────────────────────────────────

def search_aff(query: str, *, page: int = 1, fandom: str = "hp",
               **_: object) -> list[dict]:
    """AFF has no site-wide search; each fandom subdomain offers a
    ``story-list.php`` page. We grab the latest story list for the
    chosen fandom and filter client-side by the query."""
    fandom = (fandom or "hp").strip().lower().strip(".")
    url = f"https://{fandom}.adult-fanfiction.org/story-list.php?page={page}"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for a in soup.find_all("a", href=re.compile(r"story\.php\?no=\d+")):
        href = a.get("href", "")
        m = re.search(r"no=(\d+)", href)
        if not m:
            continue
        story_id = m.group(1)
        title = a.get_text(" ", strip=True) or f"AFF {story_id}"
        full = f"https://{fandom}.adult-fanfiction.org/{href.lstrip('/')}"
        if not _matches_query(query, title):
            continue
        out.append({
            "title": title, "author": "", "url": full,
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": fandom, "status": "",
            "site": "aff",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_sol(query: str, *, page: int = 1, tags: Optional[list] = None,
               **_: object) -> list[dict]:
    """StoriesOnline: free-text search is paywalled, but ``/stories/bytag/<tag1:tag2>``
    browses are free and have rich metadata in the result rows. If
    the caller passed one or more tags we join them with ``:`` (SOL's
    AND operator); otherwise we default to ``/library/new_stories.php``
    as a recent-works browse and apply the query as a client-side
    title filter."""
    tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    if tags:
        joined = ":".join(t.replace(" ", "-") for t in tags)
        url = f"https://storiesonline.net/stories/bytag/{joined}"
        if page > 1:
            url += f"/{page}"
    else:
        url = "https://storiesonline.net/library/new_stories.php"
        if page > 1:
            url += f"?p={page}"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen_ids = set()
    for a in soup.find_all("a", href=re.compile(r"^/s/(\d+)/")):
        m = re.match(r"^/s/(\d+)/([^/?#\s]+)", a.get("href", ""))
        if not m:
            continue
        story_id, slug = m.group(1), m.group(2)
        if story_id in seen_ids:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 3:
            continue
        if not _matches_query(query, title, slug):
            continue
        seen_ids.add(story_id)
        out.append({
            "title": title, "author": "",
            "url": f"https://storiesonline.net/s/{story_id}/{slug}",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "storiesonline",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_mcstories(query: str, *, page: int = 1,
                     tags: Optional[list] = None, **_: object) -> list[dict]:
    """MCStories indexes every story by Dublin Core tag codes at
    ``/Tags/<code>.html``. We map the first query-supplied tag to its
    two-letter code (see :data:`_MCS_TAG_CODES`) and read that page
    directly; unmapped tags fall back to the full Titles index, which
    is then filtered client-side by the query."""
    del page  # MCStories pages fit in one listing
    first_tag = next((t for t in (tags or []) if t), "") or ""
    code = _MCS_TAG_CODES.get(first_tag.lower())
    if code:
        url = f"https://mcstories.com/Tags/{code}.html"
    else:
        url = "https://mcstories.com/Titles/index.html"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for row in soup.find_all("tr"):
        a = row.find("a", href=re.compile(r"^\.\./([A-Z][A-Za-z0-9_-]+)/"))
        if a is None:
            a = row.find(
                "a", href=re.compile(r"^([A-Z][A-Za-z0-9_-]+)/"),
            )
        if not a:
            continue
        href = a.get("href", "")
        m = re.match(r"^(?:\.\./)?([A-Z][A-Za-z0-9_-]+)/", href)
        if not m:
            continue
        slug = m.group(1)
        title = a.get_text(" ", strip=True)
        codes = ""
        tds = row.find_all("td")
        if len(tds) >= 2:
            codes = tds[1].get_text(" ", strip=True)
        if not _matches_query(query, title, codes):
            continue
        out.append({
            "title": title, "author": "",
            "url": f"https://mcstories.com/{slug}/",
            "summary": codes, "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "mcstories",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


_MCS_TAG_CODES = {
    # ffn-dl's unified tag vocabulary ↔ MCStories' two-letter codes.
    # Compiled from mcstories.com/Tags/index.html. Only entries present
    # on MCStories map here; queries for tags that don't translate
    # (e.g. "chastity") fall through to the Titles index.
    "bondage": "bd", "bdsm": "bd",
    "cheating": "cb",
    "humiliation": "hu", "exhibitionism": "ex",
    "femdom": "fd", "dominance-submission": "ds",
    "feet": "ft",
    "group-sex": "gr", "orgy": "gr",
    "hypnosis": "hm",
    "incest": "in",
    "gay": "mm", "lesbian": "ff",
    "interracial": "la",
    "mind-control": "mc",
    "non-consent": "nc",
    "transgender": "ma",
    "futanari": "ma",
}


def search_lushstories(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       category: str = "", **_: object) -> list[dict]:
    """Lushstories is category-driven — every URL is ``/stories/<category>/...``.
    Use the first tag/category as the category slug, then filter by
    the query client-side. Defaults to the newest-stories listing
    when no category is given."""
    cat = (
        (category or "").strip().lower().strip("/")
        or (tags[0].lower() if tags else "")
        or "new"
    )
    cat = cat.replace(" ", "-")
    url = f"https://www.lushstories.com/stories/{cat}"
    if page > 1:
        url += f"?page={page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    for m in re.finditer(
        r'href="(/stories/([a-z0-9-]+)/([a-z0-9][a-z0-9-]+))"', html,
    ):
        href, found_cat, slug = m.group(1), m.group(2), m.group(3)
        if slug in seen:
            continue
        if not _matches_query(query, slug):
            continue
        seen.add(slug)
        out.append({
            "title": slug.replace("-", " ").title(),
            "author": "", "url": f"https://www.lushstories.com{href}",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": found_cat, "status": "",
            "site": "lushstories",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_sexstories(query: str, *, page: int = 1,
                      tags: Optional[list] = None, **_: object) -> list[dict]:
    """SexStories has a search endpoint at ``/pornstars/<query>`` for
    authors and ``/tag/<tag>`` for kinks, but both shift a lot. Easier
    to scrape the home page's newest-stories grid and filter."""
    url = "https://www.sexstories.com/"
    if page > 1:
        url += f"?pd_page={page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    for m in re.finditer(
        r'href="(/story/(\d+)/([a-z0-9_-]+))"', html,
    ):
        href, story_id, slug = m.group(1), m.group(2), m.group(3)
        if story_id in seen:
            continue
        if not _matches_query(query, slug):
            continue
        seen.add(story_id)
        title = slug.replace("_", " ").replace("-", " ").title()
        out.append({
            "title": title, "author": "",
            "url": f"https://www.sexstories.com{href}",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "sexstories",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_nifty(query: str, *, page: int = 1,
                 tags: Optional[list] = None,
                 category: str = "", **_: object) -> list[dict]:
    """Nifty doesn't have full-text search. The category directory
    at ``/nifty/<category>/`` is a plain-HTML link list of story
    subdirectories; we parse that and filter by the query."""
    del page
    cat = (category or "").strip().strip("/").lower()
    if not cat and tags:
        cat = {"gay": "gay", "lesbian": "lesbian", "bisexual": "bisexual",
               "transgender": "transgender"}.get(tags[0].lower(), "")
    if not cat:
        cat = "gay"
    url = f"https://www.nifty.org/nifty/{cat}/"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for a in soup.find_all("a", href=re.compile(r"^[a-z0-9_-]+/$", re.I)):
        href = a.get("href", "")
        slug = href.rstrip("/")
        title = a.get_text(" ", strip=True) or slug.replace("-", " ").title()
        if not _matches_query(query, title, slug):
            continue
        out.append({
            "title": title, "author": "",
            "url": f"https://www.nifty.org/nifty/{cat}/{slug}/",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": cat, "status": "",
            "site": "nifty",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_fictionmania(query: str, *, page: int = 1,
                        **_: object) -> list[dict]:
    """Fictionmania search URL. The WebDNA template requires proper
    form params; we approximate with the ``searchdisplay`` endpoint
    and parse any story links that come back."""
    del page
    if not query:
        url = "https://fictionmania.tv/recent.html"
    else:
        url = (
            "https://fictionmania.tv/searchdisplay/display.html"
            f"?searchword={re.sub(r'[^A-Za-z0-9 ]', '', query).replace(' ', '+')}"
            "&Submit=Display+Matching+Stories"
        )
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    for m in re.finditer(
        r'href="/stories/readhtmlstory\.html\?storyID=(\d+)"[^>]*>([^<]+)<',
        html, re.I,
    ):
        story_id, title = m.group(1), m.group(2).strip()
        if story_id in seen:
            continue
        seen.add(story_id)
        out.append({
            "title": title or f"Fictionmania {story_id}",
            "author": "",
            "url": (
                f"https://fictionmania.tv/stories/readhtmlstory.html"
                f"?storyID={story_id}"
            ),
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "fictionmania",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_literotica_wrapped(query: str, *, page: int = 1,
                              tags: Optional[list] = None,
                              **_: object) -> list[dict]:
    """Thin wrapper around :func:`ffn_dl.search.search_literotica` that
    maps our unified ``tags`` input onto Literotica's ``category``
    argument and tags every row with ``site='literotica'``."""
    category = ""
    if tags:
        # Literotica categories are plural, lowercase slugs on tags.literotica.com.
        category = tags[0].strip().lower().replace(" ", "-")
    kwargs: dict = {}
    if category:
        kwargs["category"] = category
    try:
        results = search_literotica(query, page=page, **kwargs)
    except Exception as exc:
        logger.debug("literotica search failed: %s", exc)
        return []
    for r in results[:PER_SITE_LIMIT]:
        r["site"] = "literotica"
    return results[:PER_SITE_LIMIT]


# ── Fan-out ──────────────────────────────────────────────────────

_SITE_FNS: dict[str, Callable[..., list[dict]]] = {
    "literotica": search_literotica_wrapped,
    "aff": search_aff,
    "storiesonline": search_sol,
    "nifty": search_nifty,
    "sexstories": search_sexstories,
    "mcstories": search_mcstories,
    "lushstories": search_lushstories,
    "fictionmania": search_fictionmania,
}


def _normalise_sites(sites, sites_choice) -> Optional[list]:
    """GUI passes ``sites_choice`` (a single string from the dropdown);
    CLI / tests pass ``sites`` (a list). Fold both into the list form
    the fan-out expects, or ``None`` for "search every site"."""
    if sites:
        if isinstance(sites, str):
            sites = [sites]
        return [s for s in sites if s and s != "all"] or None
    if sites_choice and sites_choice not in ("", "all"):
        return [sites_choice]
    return None


def _normalise_tags(tags) -> list[str]:
    """Accept either a Python list or the comma-separated string the
    multi-picker dialog writes into its text control. Drop empties."""
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def search_erotica(
    query: str = "",
    *,
    page: int = 1,
    sites: Optional[list] = None,
    sites_choice: str = "",
    tags: Optional[object] = None,
    tags_picked: Optional[object] = None,
    min_words: str = "",
    category: str = "",
    fandom: str = "",
    **_: object,
) -> list[dict]:
    """Fan-out search across every registered erotica site.

    Args:
        query: Free-text search string. Passed to each site's search
            function; most sites treat it as a case-insensitive title
            filter because they lack a real full-text API.
        page: Result page. Only sites that paginate (Literotica, SOL)
            respect this; the rest ignore it.
        sites: Restrict the fan-out to this list of site slugs. When
            ``None`` or ``["all"]``, every site in :data:`_SITE_FNS`
            is queried.
        tags: Unified tag list. Each site maps tags onto its own
            vocabulary (:data:`_MCS_TAG_CODES` for MCStories, category
            slug for Lushstories, ``bytag`` URL for SOL, etc.).
        min_words: Optional minimum word-count string like ``"5k+"``;
            applied client-side after fetch (sites don't universally
            expose word counts in their listings).
        category, fandom: Passed through to sites that accept them
            (Lushstories, AFF respectively).

    Returns:
        Merged list of result dicts. Every dict carries a ``site`` key
        (one of :data:`EROTICA_SITE_SLUGS`) so the caller can render a
        "Site" column / filter results by origin.
    """
    resolved_sites = _normalise_sites(sites, sites_choice)
    tag_list = _normalise_tags(tags_picked if tags_picked is not None else tags)
    if resolved_sites is None:
        active = list(_SITE_FNS)
    else:
        active = [s for s in resolved_sites if s in _SITE_FNS]
    if not active:
        return []

    # "any" is the no-op label on the GUI min-words dropdown — treat
    # it as an empty filter here so the threshold parser doesn't try
    # to cast it to an int.
    min_words_val = "" if min_words in ("", "any") else min_words

    kwargs = {
        "page": page,
        "tags": tag_list,
        "category": category,
        "fandom": fandom,
    }

    merged: list[dict] = []
    # ThreadPoolExecutor — every site's search is network-bound, so
    # 8 concurrent HTTP requests complete in about as long as the
    # slowest one. Each site function swallows its own errors and
    # returns [] on failure, so a dead archive doesn't sink the batch.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as ex:
        futures = {ex.submit(_SITE_FNS[s], query, **kwargs): s for s in active}
        for fut in concurrent.futures.as_completed(futures):
            site_slug = futures[fut]
            try:
                site_results = fut.result() or []
            except Exception as exc:
                logger.warning("erotica search (%s) failed: %s", site_slug, exc)
                site_results = []
            for r in site_results:
                r.setdefault("site", site_slug)
            merged.extend(site_results)

    if min_words_val:
        merged = _filter_by_min_words(merged, min_words_val)

    # Stable ordering: by site first (alphabetical) then by title — so
    # users can scan results grouped by archive without the run order
    # of the ThreadPool shuffling things between searches.
    merged.sort(key=lambda r: (r.get("site", ""), r.get("title", "").lower()))
    return merged


def _filter_by_min_words(results: list[dict], min_words: str) -> list[dict]:
    """Drop rows whose word-count is known to be under ``min_words``.

    ``min_words`` accepts either a plain integer ("5000") or one of
    the FFN-style shorthand labels ("1k", "5k+", "30k+", "150k+").
    Rows whose ``words`` field is unknown ("?") pass through — we'd
    rather keep a possibly-large story than hide it behind a guess."""
    threshold = _parse_word_threshold(min_words)
    if threshold <= 0:
        return results
    kept = []
    for r in results:
        raw = str(r.get("words") or "").replace(",", "").strip()
        if not raw or raw == "?" or not raw[0].isdigit():
            kept.append(r)
            continue
        try:
            if int(raw) >= threshold:
                kept.append(r)
        except ValueError:
            kept.append(r)
    return kept


def _parse_word_threshold(value: str) -> int:
    """Convert a ``min_words`` input into an integer threshold. Returns
    0 when the input is empty or unparseable."""
    if not value:
        return 0
    s = str(value).strip().lower().rstrip("+")
    if not s:
        return 0
    multiplier = 1
    if s.endswith("k"):
        multiplier = 1000
        s = s[:-1]
    try:
        return int(float(s) * multiplier)
    except ValueError:
        return 0
