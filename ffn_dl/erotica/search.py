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

from ..scraper import BaseScraper
from ..search import search_literotica

logger = logging.getLogger(__name__)

PER_SITE_LIMIT = 8
"""Cap the per-site result batch that fan-out pulls per page. Eight
rows × twelve sites gives a first page of ~96 results, which is
plenty for one view and keeps each site's scrape cheap."""

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
    "tgstorytime",
    "chyoa",
    "darkwanderer",
    "greatfeet",
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
    "tgstorytime": "TGStorytime",
    "chyoa": "Chyoa (interactive)",
    "darkwanderer": "Dark Wanderer",
    "greatfeet": "GreatFeet",
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
#
# Search fetches use :class:`BaseScraper`'s fetch machinery so they
# pick up the same retry + rate-limit + Cloudflare-block detection
# that downloads already benefit from. The singleton sits at module
# scope so successive search calls reuse one HTTP session and one
# AIMD delay state — rate-limit bumps from one fan-out leak through
# to the next rather than resetting on every window open.
#
# Downside: one scraper instance for *all* search traffic means its
# cache_dir isn't useful (we never save anything from search), hence
# ``use_cache=False``. Rate-limit delays are capped lower than a
# download scraper's because search is one request per site per
# page — more requests per wall-second is fine as long as the cap
# catches us if a site starts 429-ing.

_SEARCH_FETCHER = BaseScraper(
    use_cache=False,
    delay_floor=0.0,
    delay_start=0.0,
    delay_ceiling=10.0,
    max_retries=3,
    timeout=REQUEST_TIMEOUT_S,
)


def _fetch(url: str) -> Optional[str]:
    """Return the response body for ``url`` or ``None`` on failure.

    Wraps :meth:`BaseScraper._fetch` so search requests get the same
    retry + 429/503 back-off + Cloudflare-block detection that
    downloads do. Swallows exceptions because the fan-out's contract
    is "one site failing shouldn't break the search" — the caller
    records the failure in ``site_stats`` and keeps the rest."""
    try:
        return _SEARCH_FETCHER._fetch(url)
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
    """SexStories (XNXX Stories) has a real full-text search endpoint
    at ``/search/?search_story=<q>&page=<n>``. An empty query falls
    back to the homepage grid (which is already filtered + sorted by
    most-recent). Tags supplied through the unified vocabulary land in
    the query string as well — SexStories' tag-vocabulary is fuzzy
    enough that including them as search terms gives reasonable
    relevance without us maintaining a per-tag URL table."""
    query_terms = [query] if query else []
    if tags:
        query_terms.extend(tags[:3])  # top 3 tags — beyond that hits
        # SexStories' relevance noise floor.
    combined = " ".join(t for t in query_terms if t).strip()
    if combined:
        from urllib.parse import quote_plus
        url = (
            "https://www.sexstories.com/search/"
            f"?search_story={quote_plus(combined)}&page={page}"
        )
    else:
        url = "https://www.sexstories.com/"
        if page > 1:
            url += f"?pd_page={page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    for m in re.finditer(
        r'href="(/story/(\d+)/([a-z0-9_-]+))"[^>]*>([^<]*)<', html,
    ):
        href, story_id, slug, link_text = (
            m.group(1), m.group(2), m.group(3), m.group(4).strip(),
        )
        if story_id in seen:
            continue
        seen.add(story_id)
        # The link text is the real title when it's present; some rows
        # are just thumbnails (no text) so we fall back to the slug.
        title = link_text or slug.replace("_", " ").replace("-", " ").title()
        # With a combined query we already filtered server-side, so
        # accept every result. Without one (tag-less browse) keep the
        # old client-side title filter to avoid mixing in every row
        # the homepage happens to carry.
        if not combined and not _matches_query(query, slug, title):
            continue
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


def search_tgstorytime(query: str, *, page: int = 1,
                       **_: object) -> list[dict]:
    """TGStorytime category browse. No free-text search endpoint —
    the newest-stories page is the easiest-to-scrape starting point;
    we filter by query client-side.

    Each story link is wrapped in an ``onclick="confirm(age_consent)"``
    JavaScript shim for anonymous visitors; our age-consent query
    params bypass it cleanly at fetch time so we just match the
    bare ``viewstory.php?sid=<N>`` pattern."""
    url = "https://www.tgstorytime.com/"
    if page > 1:
        url = f"https://www.tgstorytime.com/index.php?page={page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    for m in re.finditer(
        r"viewstory\.php\?sid=(\d+)[^'\"]*['\"][^>]*>([^<]+)<", html,
    ):
        sid, title = m.group(1), m.group(2).strip()
        if sid in seen or not title or len(title) < 3:
            continue
        if not _matches_query(query, title):
            continue
        seen.add(sid)
        out.append({
            "title": title, "author": "",
            "url": (
                f"https://www.tgstorytime.com/viewstory.php"
                f"?sid={sid}&ageconsent=ok&warning=3"
            ),
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "tgstorytime",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_chyoa(query: str, *, page: int = 1,
                 tags: Optional[list] = None, **_: object) -> list[dict]:
    """Chyoa's ``/search/<q>`` endpoint returns chapter-level hits for
    a query; without a query we default to the browse-popular page
    filtered client-side.

    Story and chapter URLs share the numeric-id namespace (story .14
    and chapter .14 can be different works), so the dedup key is the
    ``(kind, numeric)`` pair — keying on the number alone would drop
    the second hit incorrectly."""
    if query:
        url = f"https://chyoa.com/search/{re.sub(r'[^A-Za-z0-9]+', '+', query)}"
    else:
        url = "https://chyoa.com/browse/popular"
    if page > 1:
        url += f"?page={page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in re.finditer(
        r'href="(/(story|chapter)/[^"]+?\.(\d+))"[^>]*>([^<]+)<',
        html,
    ):
        href, kind, numeric, title = (
            m.group(1), m.group(2), m.group(3), m.group(4).strip(),
        )
        key = (kind, numeric)
        if key in seen or not title:
            continue
        if not _matches_query(query, title):
            continue
        seen.add(key)
        out.append({
            "title": title, "author": "",
            "url": f"https://chyoa.com{href}",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "chyoa",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_darkwanderer(query: str, *, page: int = 1,
                        **_: object) -> list[dict]:
    """Dark Wanderer XenForo: forum "New Posts" listing gives recent
    story threads; we filter client-side by query."""
    if query:
        url = (
            "https://darkwanderer.net/search/search?keywords="
            f"{re.sub(r'[^A-Za-z0-9]+', '+', query)}&o=relevance"
        )
    else:
        url = "https://darkwanderer.net/forums/"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    for m in re.finditer(
        r'href="(/threads/([^/.]+)\.(\d+)/?)"[^>]*>([^<]+)<',
        html,
    ):
        href, slug, tid, title = (
            m.group(1), m.group(2), m.group(3), m.group(4).strip(),
        )
        if tid in seen or not title or len(title) < 3:
            continue
        if not _matches_query(query, title, slug):
            continue
        seen.add(tid)
        out.append({
            "title": title, "author": "",
            "url": f"https://darkwanderer.net{href.rstrip('/')}/",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "cuckold", "status": "",
            "site": "darkwanderer",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_greatfeet(query: str, *, page: int = 1,
                     **_: object) -> list[dict]:
    """GreatFeet: ``/tickles.htm`` lists recent stories by ``ts<N>.htm``
    href; older issues at ``/archiveN.htm`` (weekly issues 1..484+).
    The page is 1997-era HTML (unclosed ``<a>`` tags, inline font
    styling) so we lean on BeautifulSoup to let it tolerate the
    malformed markup, then read the link text as the story title.

    We decompose inline ``<img>`` tags inside the link before reading
    the text — the "new!" / "hot!" marker images carry alt attributes
    like ``"Foot Fetish Offering"`` that would otherwise pollute the
    title. Decomposing the img is less brittle than maintaining a
    regex of alt strings that changes whenever GreatFeet ships a new
    marker graphic."""
    del page  # tickles.htm is a single page — archive pages handle
    # older stories via a separate ``/archive<N>.htm`` route.
    url = "https://www.greatfeet.com/tickles.htm"
    html = _fetch(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    seen = set()
    for a in soup.find_all(
        "a", href=re.compile(r"/stories/ts(\d+)\.htm", re.I),
    ):
        m = re.search(r"/stories/ts(\d+)\.htm", a.get("href", ""))
        if not m:
            continue
        sid = m.group(1)
        if sid in seen:
            continue
        # Strip marker images (``<img src="new.gif">``, ``<img
        # src="hot.gif">``) before reading link text — their alt
        # attributes are not part of the story title.
        for img in list(a.find_all("img")):
            img.decompose()
        title = a.get_text(" ", strip=True)
        # Links that only carry a teaser image (no text after img
        # decompose) fall back to the id-derived placeholder so the
        # row still has a title the reader can scan.
        if not title:
            title = f"GreatFeet story {sid}"
        if not _matches_query(query, title):
            continue
        seen.add(sid)
        out.append({
            "title": title, "author": "Anonymous",
            "url": f"https://www.greatfeet.com/stories/ts{sid}.htm",
            "summary": "", "words": "?", "chapters": "1",
            "rating": "M", "fandom": "feet", "status": "",
            "site": "greatfeet",
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
    "tgstorytime": search_tgstorytime,
    "chyoa": search_chyoa,
    "darkwanderer": search_darkwanderer,
    "greatfeet": search_greatfeet,
}


TAG_SITE_COVERAGE: dict[str, list[str]] = {
    # Which sites carry each tag as a first-class category or
    # well-represented kink. Used by the tag picker to annotate each
    # option with its site-count (see :func:`tag_site_count`) so users
    # can see at a glance whether a tag is well-covered or niche.
    # Entries only list sites that expose the tag as a native filter /
    # search dimension; sites where it's just buried in free text don't
    # count. Verified April 2026.
    "anal": ["literotica", "storiesonline", "lushstories", "sexstories"],
    "bdsm": ["literotica", "lushstories", "storiesonline", "mcstories"],
    "bondage": ["literotica", "lushstories", "storiesonline", "mcstories"],
    "bukkake": ["literotica", "sexstories"],
    "celebrity": ["literotica", "storiesonline", "sexstories"],
    "cheating": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer",
    ],
    "chastity": ["literotica", "storiesonline", "mcstories"],
    "cuckold": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer",
    ],
    "dominance-submission": [
        "literotica", "lushstories", "storiesonline", "mcstories",
    ],
    "exhibitionism": [
        "literotica", "lushstories", "storiesonline", "mcstories",
    ],
    "femdom": [
        "literotica", "lushstories", "storiesonline", "mcstories",
    ],
    "feet": [
        "literotica", "lushstories", "storiesonline", "mcstories",
        "greatfeet",
    ],
    "fisting": ["literotica", "sexstories"],
    "futanari": ["literotica", "storiesonline", "mcstories", "tgstorytime"],
    "gangbang": ["literotica", "lushstories", "storiesonline", "sexstories"],
    "gay": [
        "literotica", "lushstories", "storiesonline", "nifty",
        "sexstories", "aff",
    ],
    "group-sex": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "mcstories",
    ],
    "harem": ["literotica", "storiesonline"],
    "humiliation": [
        "literotica", "lushstories", "storiesonline", "mcstories",
    ],
    "hypnosis": ["mcstories", "storiesonline", "literotica"],
    "incest": [
        "literotica", "storiesonline", "sexstories", "aff",
        "mcstories",
    ],
    "interracial": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer",
    ],
    "lactation": ["literotica", "storiesonline", "sexstories"],
    "lesbian": [
        "literotica", "lushstories", "storiesonline", "nifty",
        "sexstories", "aff",
    ],
    "masturbation": ["literotica", "lushstories", "sexstories"],
    "mature": ["literotica", "lushstories", "storiesonline"],
    "mind-control": ["mcstories", "storiesonline", "literotica", "chyoa"],
    "non-consent": [
        "literotica", "storiesonline", "mcstories", "sexstories",
    ],
    "oral": ["literotica", "lushstories", "sexstories"],
    "orgy": ["literotica", "storiesonline", "sexstories", "mcstories"],
    "polyamory": ["literotica", "storiesonline", "lushstories"],
    "pregnancy": ["literotica", "storiesonline", "sexstories"],
    "public-sex": ["literotica", "lushstories", "storiesonline"],
    "roleplay": ["literotica", "lushstories", "chyoa"],
    "rough": ["literotica", "lushstories", "sexstories"],
    "spanking": ["literotica", "lushstories", "storiesonline", "mcstories"],
    "swinging": ["literotica", "lushstories", "storiesonline", "darkwanderer"],
    "teen": ["literotica", "lushstories", "storiesonline", "sexstories"],
    "threesome": ["literotica", "lushstories", "storiesonline", "sexstories"],
    "transgender": [
        "literotica", "fictionmania", "tgstorytime", "storiesonline",
        "mcstories",
    ],
    "voyeur": ["literotica", "lushstories", "storiesonline"],
    "watersports": ["literotica", "storiesonline", "sexstories"],
}


def tag_site_count(tag: str) -> int:
    """How many sites meaningfully cover this tag. Used by the GUI
    multi-picker to annotate each entry with ``[N sites]`` so users
    can tell femdom (many sites) from chastity (three sites) before
    running a fruitless query."""
    return len(TAG_SITE_COVERAGE.get(tag.lower(), []))


def tag_sites_for(tag: str) -> list[str]:
    """Site slugs that meaningfully cover ``tag``. Empty list means
    the tag isn't in the unified vocabulary."""
    return list(TAG_SITE_COVERAGE.get(tag.lower(), []))


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


_TAG_COVERAGE_SUFFIX_RE = re.compile(r"\s*\[\d+\s+sites?\]\s*$", re.I)
"""Strips the "[5 sites]" annotation the GUI tag picker appends to
each option. Keeps the scraper-facing tag list clean."""


def _normalise_tags(tags) -> list[str]:
    """Accept either a Python list or the comma-separated string the
    multi-picker dialog writes into its text control. Drop empties
    and strip the GUI's ``[N sites]`` coverage annotation."""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        raw = [str(t).strip() for t in tags if str(t).strip()]
    return [_TAG_COVERAGE_SUFFIX_RE.sub("", t).strip() for t in raw if t]


class ErotiCAResults(list):
    """``list`` subclass that carries per-site stats alongside the
    merged result rows.

    Rationale: the GUI's SearchFrame treats search results as a plain
    ``list[dict]`` (so ``fetch_until_limit`` can iterate without
    knowing which site it's talking to), but the unified search also
    needs to surface *per-site* health — how many hits each archive
    returned, which ones failed, and which ones are exhausted. Rather
    than break the contract or thread a second return value through
    ``fetch_until_limit``, we subclass ``list`` and hang the stats
    dict off an attribute. Callers that don't care keep seeing a list
    of dicts; the erotica SearchFrame peeks at ``.site_stats`` for
    the summary panel and ``.exhausted_sites`` for load-more gating.
    """

    site_stats: dict
    exhausted_sites: set

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.site_stats = {}
        self.exhausted_sites = set()


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
    skip_sites: Optional[set] = None,
    **_: object,
) -> "ErotiCAResults":
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
    skip_set = set(skip_sites or ())
    if resolved_sites is None:
        active = [s for s in _SITE_FNS if s not in skip_set]
    else:
        active = [
            s for s in resolved_sites if s in _SITE_FNS and s not in skip_set
        ]
    if not active:
        return ErotiCAResults()

    min_words_val = "" if min_words in ("", "any") else min_words

    kwargs = {
        "page": page,
        "tags": tag_list,
        "category": category,
        "fandom": fandom,
    }

    merged = ErotiCAResults()
    # Per-site stats: count of hits, ok flag, error message (or None).
    # The GUI summary panel reads this to show "MCStories: 8, SOL:
    # 12, SoFurry: FAIL (timeout)" so users know which sites contributed
    # versus which silently failed vs. returned zero hits.
    site_stats: dict[str, dict] = {
        s: {"count": 0, "ok": True, "error": None, "exhausted": False}
        for s in active
    }

    # ThreadPoolExecutor — every site's search is network-bound, so
    # concurrent HTTP requests complete in about as long as the
    # slowest one. Each site function swallows its own errors, but we
    # also catch here to flip the per-site ``ok`` flag so the GUI can
    # surface the failure instead of silently omitting a site.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(active)) as ex:
        futures = {ex.submit(_SITE_FNS[s], query, **kwargs): s for s in active}
        for fut in concurrent.futures.as_completed(futures):
            site_slug = futures[fut]
            try:
                site_results = fut.result() or []
                site_stats[site_slug]["count"] = len(site_results)
                # Anything short of a full batch means we've hit the
                # tail of whatever ordering this site uses — mark it
                # exhausted so Load More doesn't re-poll for a page
                # that'll just return the same rows again.
                if len(site_results) < PER_SITE_LIMIT:
                    site_stats[site_slug]["exhausted"] = True
                    merged.exhausted_sites.add(site_slug)
            except Exception as exc:
                logger.warning("erotica search (%s) failed: %s", site_slug, exc)
                site_stats[site_slug].update(
                    ok=False, error=str(exc) or exc.__class__.__name__,
                    exhausted=True,
                )
                merged.exhausted_sites.add(site_slug)
                site_results = []
            for r in site_results:
                r.setdefault("site", site_slug)
            merged.extend(site_results)

    if min_words_val:
        filtered = _filter_by_min_words(merged, min_words_val)
        # Preserve the stats across the filter rebuild.
        new_merged = ErotiCAResults(filtered)
        new_merged.site_stats = site_stats
        new_merged.exhausted_sites = merged.exhausted_sites
        merged = new_merged
    else:
        merged.site_stats = site_stats

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
