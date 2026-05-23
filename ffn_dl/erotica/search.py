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
import unicodedata
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
    "ao3",
    "wattpad",
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
    "bdsmlibrary",
]
"""Site-picker options for the unified search window. The first entry
(``all``) triggers fan-out; everything else scopes to a single site."""

EROTICA_SITE_LABELS: dict[str, str] = {
    "all": "All erotica sites",
    "literotica": "Literotica",
    "ao3": "AO3 (Explicit)",
    "wattpad": "Wattpad",
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
    "bdsmlibrary": "BDSM Library",
}

EROTICA_TAG_VOCABULARY: list[str] = [
    # The unified discovery axis. Most entries are general kinks
    # carried by ≥3 sites; a smaller refining-tag set targets
    # specific sub-interests (foot-worship under feet, pegging /
    # tease-and-denial / cfnm / strap-on / female-led / body-worship
    # under femdom, queening under cunnilingus) so users with a
    # narrower interest aren't forced to wade through the umbrella
    # tag's broader catalogue.
    "anal",
    "bdsm",
    "body-worship",
    "bondage",
    "bukkake",
    "celebrity",
    "cfnm",
    "cheating",
    "chastity",
    "cuckold",
    "cunnilingus",
    "dominance-submission",
    "exhibitionism",
    "face-sitting",
    "female-led",
    "femdom",
    "feet",
    "fisting",
    "foot-worship",
    "footjob",
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
    "pegging",
    "polyamory",
    "pregnancy",
    "public-sex",
    "queening",
    "roleplay",
    "rough",
    "spanking",
    "strap-on",
    "swinging",
    "tease-and-denial",
    "teen",
    "threesome",
    "trampling",
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


class SearchFetchError(RuntimeError):
    """Raised by :func:`_fetch` when the search HTTP fetch fails.

    The fan-out catches this per-site and marks the site as failed in
    ``site_stats``, surfacing the breakage to the user instead of
    silently reporting "0 results from <site>" the way the previous
    swallow-and-return-None contract did.
    """


def _fetch(url: str) -> str:
    """Return the response body for ``url``.

    Wraps :meth:`BaseScraper._fetch` so search requests get the same
    retry + 429/503 back-off + Cloudflare-block detection that
    downloads do. Raises :class:`SearchFetchError` on failure (the
    fan-out catches per-site, so one broken site doesn't kill the
    rest). Logs at WARNING so users debugging "search returns
    nothing" can actually see which sites' URLs are stale.
    """
    try:
        return _SEARCH_FETCHER._fetch(url)
    except Exception as exc:
        logger.warning("erotica search fetch %s failed: %s", url, exc)
        raise SearchFetchError(url) from exc


def _post(url: str, data: dict) -> str:
    """POST helper for the few sites whose search is form-driven
    (Sexstories). Same surface as :func:`_fetch` — raises
    :class:`SearchFetchError` so per-site failures are visible.
    """
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.post(
            url, data=data, impersonate="chrome", timeout=REQUEST_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise SearchFetchError(f"{url} POST -> HTTP {resp.status_code}")
        return resp.text
    except SearchFetchError:
        raise
    except Exception as exc:
        logger.warning("erotica search POST %s failed: %s", url, exc)
        raise SearchFetchError(url) from exc


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


# ── Per-site tag-vocabulary translation ─────────────────────────────
#
# The unified ``EROTICA_TAG_VOCABULARY`` is the user-facing tag list;
# every site uses its own slugs / codes / titles. Each adapter looks
# its tag up here instead of passing the vocab tag through verbatim
# (which would mean every site that doesn't happen to use the same
# slug — most of them — gets a 0-result fallback page silently).
#
# Returning ``None`` from :func:`_translate_tag` is a signal that the
# site has no representation for that tag at all; callers treat that
# as "skip this site for this tag" rather than degrading to a default
# browse that would flood the result set with off-topic rows.
#
# Layout: ``_TAG_SLUGS[site][vocab_tag] = site_specific_slug``.
# Missing entries fall through to :func:`_translate_tag`'s default
# behaviour: vocab tag → passthrough for sites where the slug shape
# usually matches our vocab (literotica), or ``None`` for sites where
# an unmapped tag would never resolve (everything else).

_LITEROTICA_TAG_SLUGS: dict[str, str] = {
    # tags.literotica.com is permissive — most vocab tags are valid
    # slugs verbatim and fall through ``_translate_tag`` via the
    # passthrough path. Only override when Literotica uses a different
    # shape, or when we want to swap to the higher-volume sibling tag.
    # Verified May 2026: ``feet`` returns ~100 cards vs ``foot-fetish``
    # ~34, so we keep the broader slug. All refining tags below
    # confirmed alive with story cards on the live site.
    "body-worship": "body-worship",
    "cfnm": "cfnm",
    "female-led": "female-led-relationship",
    "foot-worship": "foot-worship",
    "footjob": "footjob",
    "pegging": "pegging",
    "queening": "queening",
    "strap-on": "strap-on",
    "tease-and-denial": "tease-and-denial",
    "trampling": "trampling",
}

_LUSH_TAG_SLUGS: dict[str, str] = {
    # Lushstories categories — verified live against
    # /stories/<slug>. Anything not here returns a stub page with
    # zero story anchors, so an explicit allowlist is mandatory.
    "anal": "anal",
    "bdsm": "bdsm",
    "bondage": "bdsm",          # no standalone bondage category
    "cheating": "cheating",
    "cuckold": "cuckold",
    "cunnilingus": "oral-sex",
    "dominance-submission": "bdsm",
    "exhibitionism": "exhibitionism",
    "face-sitting": "facesitting",  # Lush uses no hyphen
    "femdom": "femdom",
    "feet": "fetish",           # no feet-specific category
    "foot-worship": "fetish",   # subset of fetish; no narrower slug
    "footjob": "fetish",        # subset of fetish; no narrower slug
    "gangbang": "group-sex",
    "gay": "gay-male",
    "group-sex": "group-sex",
    "harem": "threesomes",
    "humiliation": "bdsm",
    "incest": "taboo",          # Lush's incest stand-in
    "interracial": "interracial",
    "lesbian": "lesbian",
    "masturbation": "masturbation",
    "mature": "mature",
    "mind-control": "mind-control",
    "non-consent": "reluctance",
    "oral": "oral-sex",
    "pegging": "strap-on-sex",  # closest Lush bucket for pegging
    "polyamory": "wife-lovers",
    "public-sex": "exhibitionism",
    "queening": "facesitting",  # queening is a face-sitting variant
    "roleplay": "fantasy-sci-fi",
    "rough": "hardcore",
    "spanking": "spanking",
    "strap-on": "strap-on-sex",
    "swinging": "swingers",
    "teen": "teen",
    "threesome": "threesomes",
    "transgender": "trans",
    "voyeur": "voyeur",
    "watersports": "watersports",
}

_SOL_TAG_SLUGS: dict[str, str] = {
    # storiesonline.net /stories/bytag/<slug>. SOL uses joined-word
    # slugs (``femaledom``) where most sites use a hyphen, and serves
    # the all-tags index for any unrecognised slug — so passing
    # ``feet`` verbatim returned a 50 KB tag-index page that parsed as
    # zero stories. Refining tags that SOL doesn't carry (foot-worship,
    # tease-and-denial, cfnm, etc.) are absent here; ``_translate_tag``
    # returns ``None`` and the dispatcher skips SOL for those tags.
    "anal": "anal",
    "bdsm": "bdsm",
    "bondage": "bondage",
    "bukkake": "facial",
    "celebrity": "celebrity",
    "cheating": "cheating",
    "chastity": "chastity-belt",
    "cuckold": "cuckold",
    "cunnilingus": "oral-sex",
    "dominance-submission": "domsub",
    "exhibitionism": "exhibitionism",
    "feet": "foot-fetish",
    "foot-worship": "foot-fetish",   # SOL only has the umbrella slug
    "footjob": "foot-fetish",
    "femdom": "femaledom",
    "fisting": "fisting",
    "gangbang": "gangbang",
    "gay": "gay",
    "group-sex": "group-sex",
    "harem": "harem",
    "humiliation": "humiliation",
    "hypnosis": "hypnosis",
    "incest": "incest",
    "interracial": "interracial",
    "lactation": "lactation",
    "lesbian": "lesbian",
    "masturbation": "masturbation",
    "mature": "mature",
    "mind-control": "mind-control",
    "non-consent": "rape",
    "oral": "oral-sex",
    "orgy": "orgy",
    "pegging": "pegging",            # verified live
    "polyamory": "polygamy",
    "pregnancy": "pregnancy",
    "public-sex": "exhibitionism",
    "queening": "oral-sex",          # closest SOL bucket
    "roleplay": "fan-fiction",
    "rough": "rough",
    "spanking": "spanking",
    "swinging": "swinging",
    "teen": "school",
    "threesome": "ménage",
    "transgender": "transgender",
    "voyeur": "voyeur",
    "watersports": "water-sports",
}

_AO3_TAG_SLUGS: dict[str, str] = {
    # AO3 freeform tags are Title-Case with spaces. These are the
    # canonical AO3 tag names — passing them via the ``freeform``
    # filter on :func:`search_ao3` lands on the canonical tag page.
    # All refining tags verified live May 2026 — each /tags/<Name>
    # page returns its canonical 20-work batch.
    "anal": "Anal Sex",
    "bdsm": "BDSM",
    "body-worship": "Body Worship",
    "bondage": "Bondage",
    "bukkake": "Bukkake",
    "celebrity": "Celebrity Crush",
    "cfnm": "CFNM",
    "cheating": "Infidelity",
    "chastity": "Chastity Device",
    "cuckold": "Cuckolding",
    "cunnilingus": "Cunnilingus",
    "dominance-submission": "Dom/sub",
    "exhibitionism": "Exhibitionism",
    "face-sitting": "Face-Sitting",
    "female-led": "Female-Led Relationship",
    "femdom": "Femdom",
    "feet": "Foot Fetish",
    "fisting": "Fisting",
    "foot-worship": "Foot Worship",
    "footjob": "Footjob",
    "futanari": "Futanari",
    "gangbang": "Gangbang",
    "gay": "M/M",
    "group-sex": "Group Sex",
    "harem": "Harem",
    "humiliation": "Humiliation",
    "hypnosis": "Hypnotism",
    "incest": "Incest",
    "interracial": "Interracial Relationship(s)",
    "lactation": "Lactation",
    "lesbian": "F/F",
    "masturbation": "Masturbation",
    "mature": "Mature",
    "mind-control": "Mind Control",
    "non-consent": "Non-Consensual",
    "oral": "Oral Sex",
    "orgy": "Orgy",
    "pegging": "Pegging",
    "polyamory": "Polyamory",
    "pregnancy": "Pregnancy",
    "public-sex": "Public Sex",
    "queening": "Queening",
    "roleplay": "Roleplay",
    "rough": "Rough Sex",
    "spanking": "Spanking",
    "strap-on": "Strap-On Use",
    "swinging": "Swinging",
    "tease-and-denial": "Tease and Denial",
    "teen": "Underage",
    "threesome": "Threesome",
    "trampling": "Trampling",
    "transgender": "Trans Character",
    "voyeur": "Voyeurism",
    "watersports": "Watersports",
}

_WATTPAD_TAG_SLUGS: dict[str, str] = {
    # Wattpad's ``/stories/<slug>`` tag pages — verified live against
    # the JSON-LD ``ListItem`` embed. Wattpad's catalogue skews
    # romance/female-led; coverage is strong on the femdom side
    # (femdom=19, pegging=10, cfnm=4, female-led=5) but absent on the
    # cunnilingus side (every cunnilingus-adjacent slug 404s). Only
    # tags with verified non-empty pages are mapped here so the
    # adapter doesn't waste a fan-out slot on dead URLs.
    "bdsm": "bdsm",
    "body-worship": "body-worship",
    "bondage": "bondage",
    "celebrity": "celebrity",
    "cfnm": "cfnm",
    "cheating": "cheating",
    "chastity": "chastity",
    "cuckold": "cuckold",
    "dominance-submission": "dom-sub",
    "exhibitionism": "exhibitionism",
    "female-led": "female-led",
    "femdom": "femdom",
    "feet": "foot-fetish",
    "foot-worship": "foot-worship",
    "gangbang": "gangbang",
    "gay": "gay",
    "harem": "harem",
    "humiliation": "humiliation",
    "hypnosis": "hypnosis",
    "interracial": "interracial",
    "lesbian": "lesbian",
    "masturbation": "masturbation",
    "mature": "mature",
    "non-consent": "noncon",
    "pegging": "pegging",
    "polyamory": "polyamory",
    "spanking": "spanking",
    "strap-on": "strap-on",
    "tease-and-denial": "tease",
    "teen": "teen",
    "threesome": "threesome",
    "trampling": "trampling",
    "transgender": "trans",
    "voyeur": "voyeur",
}

_BDSMLIB_TAG_CODES: dict[str, str] = {
    # BDSM Library encodes categories as numeric ``codeforstory[N]``
    # radio buttons on the advanced ``/stories/search.php`` form
    # (Yes/No/Maybe — submit ``yes`` for the targeted tag). The
    # numeric IDs are stable code-IDs in the site's DB. Verified May
    # 2026 by scraping the rendered form. Vocab tag → BDSM Library
    # numeric code ID.
    "bdsm": "71",                       # BDSM
    "bondage": "70",                    # bondage
    "chastity": "79",                   # chastity belt
    "dominance-submission": "38",       # D/s
    "exhibitionism": "21",              # exhibition
    "femdom": "13",                     # F/m — one female dominating one male
    "feet": "41",                       # feet
    "fisting": "23",                    # fisting
    "incest": "22",                     # incest
    "interracial": "24",                # interracial
    "lactation": "25",                  # lactation
    "non-consent": "86",                # Rape
    "spanking": "30",                   # spanking
    "teen": "31",                       # teen
    "transgender": "33",                # transgender
    "voyeur": "35",                     # voyeurism
    "watersports": "36",                # WaterSport
}

_SITE_TAG_SLUGS: dict[str, dict[str, str]] = {
    "literotica": _LITEROTICA_TAG_SLUGS,
    "lushstories": _LUSH_TAG_SLUGS,
    "storiesonline": _SOL_TAG_SLUGS,
    "ao3": _AO3_TAG_SLUGS,
    "wattpad": _WATTPAD_TAG_SLUGS,
    "bdsmlibrary": _BDSMLIB_TAG_CODES,
    # ``mcstories`` is wired in below where ``_MCS_TAG_CODES`` is
    # defined (it predates the translation layer).
}


# Sites whose slug shape is permissive enough that any vocab tag the
# user picks tends to resolve. Literotica's ``tags.literotica.com``
# subdomain happily returns ~100 cards for arbitrary slugs (verified:
# ``cheating``, ``cuckold``, ``transgender``, ``futanari``,
# ``chastity``, all → ~100 cards) so we don't need an exhaustive
# override table — anything not in :data:`_LITEROTICA_TAG_SLUGS`
# falls through to the vocab tag verbatim.
_LITEROTICA_PASSTHROUGH = True


def _translate_tag(site: str, vocab_tag: str) -> Optional[str]:
    """Return the site-specific slug for ``vocab_tag``, or ``None``
    when the site has no representation for the tag.

    Callers use ``None`` as a skip signal — don't fall back to a
    default browse (would flood results with off-topic rows) and
    don't fall back to the raw vocab tag (would build a URL the site
    silently 404s into a stub/index page).
    """
    if not vocab_tag:
        return None
    key = vocab_tag.strip().lower()
    if not key:
        return None
    site_map = _SITE_TAG_SLUGS.get(site)
    if site_map and key in site_map:
        return site_map[key]
    if site == "literotica" and _LITEROTICA_PASSTHROUGH:
        return key
    return None


# ── Per-site searches ────────────────────────────────────────────

def search_aff(query: str, *, page: int = 1, fandom: str = "",
               **_: object) -> list[dict]:
    """AFF has no site-wide search; each fandom subdomain offers a
    paginated ``index.php`` story listing. We grab the listing for
    the chosen fandom and filter client-side by the query.

    AFF retired ``story-list.php`` (404s as of 2025) — pagination
    moved to ``index.php?page=N``.

    AFF's catalog is partitioned by fandom subdomain
    (hp.adult-fanfiction.org, sw.adult-fanfiction.org, etc.) — there
    is no aggregated "all fandoms" view. Without an explicit
    ``fandom``, we'd have to guess which subdomain to query and any
    guess silently biases results to whichever fandom we picked. We
    used to default to ``"hp"`` which leaked Harry Potter results
    into every all-sites erotica search; return ``[]`` instead so the
    site stats panel makes it obvious AFF was skipped and the user
    can fill in the Fandom (AFF) field to opt in.
    """
    fandom = (fandom or "").strip().lower().strip(".")
    if not fandom:
        return []
    url = f"https://{fandom}.adult-fanfiction.org/index.php?page={page}"
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
    the caller passed one or more tags we translate them through
    :data:`_SOL_TAG_SLUGS` (SOL uses joined-word slugs like
    ``femaledom`` where most sites use a hyphen) and join with ``:``
    — SOL's AND operator. Tags that don't translate are dropped; if
    the resulting list is empty we return ``[]`` rather than falling
    back to the recent-works browse (which would flood results with
    off-topic rows).

    Without any tags, default to ``/library/new_stories.php`` as a
    recent-works browse and apply the query as a client-side title
    filter.
    """
    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    if vocab_tags:
        translated = [_translate_tag("storiesonline", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        joined = ":".join(translated)
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
    # SOL renders rows like:
    #   <h3 class="sname">N <a href="/n/<id>/<slug>">Title</a>
    #                       by <a href="/a/<author>">Author</a></h3>
    # The chapter URLs that the downloader consumes are at /s/<id>/...,
    # but the listing now uses /n/<id>/<slug> (story-page redirect) —
    # the previous parser only matched /s/ which doesn't appear on
    # this listing anymore, so every search returned 0 rows.
    for h3 in soup.find_all("h3", class_="sname"):
        anchors = h3.find_all("a", href=True)
        if len(anchors) < 1:
            continue
        title_a = anchors[0]
        m = re.match(r"^/n/(\d+)/([^/?#\s]+)", title_a.get("href", ""))
        if not m:
            continue
        story_id, slug = m.group(1), m.group(2)
        if story_id in seen_ids:
            continue
        title = title_a.get_text(" ", strip=True)
        if not title or len(title) < 3:
            continue
        author = ""
        if len(anchors) >= 2:
            a_href = anchors[1].get("href", "")
            if a_href.startswith("/a/"):
                author = anchors[1].get_text(" ", strip=True)
        if not _matches_query(query, title, slug):
            continue
        seen_ids.add(story_id)
        out.append({
            "title": title, "author": author,
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
    ``/Tags/<code>.html``. The first query-supplied tag is translated
    through :data:`_MCS_TAG_CODES` (which mirrors MCStories' 26 real
    codes — earlier revisions silently mapped to wrong codes like
    ``cb`` = comic-book instead of cheating). Tag-only searches for
    tags MCStories doesn't carry return ``[]`` rather than the full
    Titles index — the dispatcher's tag-capability filter already
    drops MCS from a fan-out for unsupported tags, but defending the
    adapter keeps direct callers honest too.
    """
    del page  # MCStories pages fit in one listing
    first_tag = next((t for t in (tags or []) if t), "") or ""
    code = _MCS_TAG_CODES.get(first_tag.lower())
    if code:
        url = f"https://mcstories.com/Tags/{code}.html"
    elif first_tag and not query:
        return []
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
    # Re-verified May 2026 against mcstories.com/Tags/index.html.
    # MCStories has only 26 codes; tags not represented here have no
    # MCStories tag at all and are dropped from the fan-out via
    # :func:`_site_handles_any_tag`.
    #
    # Earlier revisions silently mapped to the wrong code (``cb`` =
    # comic-book NOT cheating, ``ft`` = clothing-fetish NOT feet,
    # ``hu`` = humor NOT humiliation, ``gr`` = growth NOT group-sex,
    # ``hm`` = humiliation NOT hypnosis, ``la`` = lactation NOT
    # interracial, ``ma`` = masturbation NOT TG/futanari). Those
    # mappings sent users to wholly unrelated tag pages.
    "bondage": "bd", "bdsm": "bd",     # bd = bondage and/or discipline
    "dominance-submission": "ds",       # ds = dominance and/or submission
    "exhibitionism": "ex",              # ex = exhibitionism
    "femdom": "fd",                     # fd = female dominant
    "gay": "mm",                        # mm = male/male sex
    "lesbian": "ff",                    # ff = female/female sex
    "humiliation": "hm",                # hm = humiliation
    "hypnosis": "mc",                   # closest MCS tag (no hypnosis-specific code)
    "incest": "in",                     # in = incest
    "lactation": "la",                  # la = lactation
    "masturbation": "ma",               # ma = masturbation
    "mind-control": "mc",               # mc = mind control
    "non-consent": "nc",                # nc = non-consensual
    "watersports": "ws",                # ws = watersports
}

# Late-bind MCStories into the unified translation layer. Predates
# the layer, so it's structured as its own table; this just gives
# ``_translate_tag("mcstories", ...)`` access to the same lookup the
# adapter already uses.
_SITE_TAG_SLUGS["mcstories"] = _MCS_TAG_CODES


def search_lushstories(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       category: str = "", **_: object) -> list[dict]:
    """Lushstories is category-driven — every URL is ``/stories/<category>/...``.

    The unified tag → Lush category translation lives in
    :data:`_LUSH_TAG_SLUGS` (verified live against /stories/<slug>):
    Lush uses ``facesitting`` (no hyphen), ``oral-sex`` (not ``oral``),
    ``fetish`` as the umbrella for feet/foot interest, and has no
    standalone feet category. A vocab tag that doesn't translate
    returns ``[]`` rather than falling through to ``/stories/<vocab>``,
    which serves a 404-shaped 200 page (Next.js stub with no story
    anchors).

    An explicit ``category`` filter overrides tag translation — the
    GUI's Category text box is for users who already know Lush's
    slug. With neither tag nor category, default to the newest-
    stories listing.
    """
    cat = (category or "").strip().lower().strip("/").replace(" ", "-")
    if not cat and tags:
        first_tag = tags[0].strip().lower()
        translated = _translate_tag("lushstories", first_tag)
        if translated is None:
            return []
        cat = translated
    if not cat:
        cat = "new"
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
    """SexStories (XNXX Stories) drives its search via a POST form at
    ``/search/`` (field name ``search``); the previous GET form
    ``?search_story=...`` returns 404 as of 2025. An empty query
    falls back to the homepage grid sorted by most-recent.

    Tags supplied through the unified vocabulary land in the query
    string — SexStories' tag-vocabulary is fuzzy enough that including
    them as search terms gives reasonable relevance without us
    maintaining a per-tag URL table.
    """
    query_terms = [query] if query else []
    if tags:
        query_terms.extend(tags[:3])  # top 3 tags — beyond that hits
        # SexStories' relevance noise floor.
    combined = " ".join(t for t in query_terms if t).strip()
    if combined:
        # POST to /search/ with the form's actual field names. Page
        # navigation on the result set isn't exposed in the form, so
        # we serve only the first page server-side and rely on the
        # fan-out's PER_SITE_LIMIT cap.
        html = _post(
            "https://www.sexstories.com/search/",
            data={"search": combined, "type": "story"},
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


_NIFTY_TAG_CATEGORIES = {
    "gay": "gay", "lesbian": "lesbian", "bisexual": "bisexual",
    "transgender": "transgender",
}


def search_nifty(query: str, *, page: int = 1,
                 tags: Optional[list] = None,
                 category: str = "", **_: object) -> list[dict]:
    """Nifty doesn't have full-text search. The category directory
    at ``/nifty/<category>/`` is a plain-HTML link list of story
    subdirectories. Only a few sexuality categories map cleanly to
    our unified tag vocabulary (see :data:`_NIFTY_TAG_CATEGORIES`);
    other tags return ``[]`` so a tag-only ``bdsm`` search doesn't
    silently default to the ``/gay/`` directory."""
    del page
    cat = (category or "").strip().strip("/").lower()
    if not cat and tags:
        cat = _NIFTY_TAG_CATEGORIES.get(tags[0].lower(), "")
        if not cat and not query:
            return []
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
        # Fictionmania's WebDNA endpoint is fussy about the
        # ``searchword`` payload — ASCII letters, digits, and spaces
        # only — so anything outside that gets stripped. Earlier code
        # used a regex that also wiped non-ASCII letters, turning a
        # query like "café" into "caf" before it reached the network.
        # Normalise to NFKD first so accented letters degrade to
        # their ASCII base instead of disappearing, then strip the
        # rest. Spaces become ``+`` per WebDNA's form-encoded
        # convention; ``urllib.parse.quote_plus`` would emit ``%20``
        # which the endpoint actually rejects.
        flat = (
            unicodedata.normalize("NFKD", query)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        searchword = re.sub(r"[^A-Za-z0-9 ]", "", flat).replace(" ", "+")
        url = (
            "https://fictionmania.tv/searchdisplay/display.html"
            f"?searchword={searchword}"
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
    # The age-consent interstitial gates the homepage and index.php
    # for anonymous visitors too — without these query params the
    # listing HTML is the consent gate itself, not the story rows.
    age_qs = "ageconsent=ok&warning=3"
    if page > 1:
        url = f"https://www.tgstorytime.com/index.php?page={page}&{age_qs}"
    else:
        url = f"https://www.tgstorytime.com/?{age_qs}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    # Use BeautifulSoup so a title wrapped in nested formatting
    # (``<i>``, ``<font>``, etc.) is captured in full. The previous
    # regex truncated at the first ``<``, losing italicised words.
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=re.compile(r"viewstory\.php\?sid=\d+")):
        href = a.get("href") or ""
        m = re.search(r"sid=(\d+)", href)
        if not m:
            continue
        sid = m.group(1)
        title = a.get_text(" ", strip=True)
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
        # Chyoa runs on Symfony — its router expects pagination as a
        # path segment, not a query parameter. The previous ``?page=N``
        # form was silently ignored, so every "page 2+" request returned
        # page 1 and the load-more button looped on identical results.
        # Quote spaces (and other unsafe chars) properly rather than
        # collapsing to ``+``, which Chyoa encodes literally as ``%2B``.
        from urllib.parse import quote
        url = f"https://chyoa.com/search/{quote(query, safe='')}"
    else:
        url = "https://chyoa.com/browse/popular"
    if page > 1:
        url += f"/page/{page}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # Chyoa renders search-result links as full URLs
    # (``href="https://chyoa.com/story/..."``), not relative paths.
    # The previous regex required a leading ``/`` and matched zero
    # rows on every search.
    for m in re.finditer(
        r'href="(?:https?://chyoa\.com)?(/(story|chapter)/[^"]+?\.(\d+))"'
        r'[^>]*>([^<]+)<',
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
    story threads; we filter client-side by query.

    Query encoding mirrors :func:`search_fictionmania` — XenForo's
    keyword field tolerates only ASCII letters/digits/spaces/dashes,
    so we NFKD-fold first to degrade accented letters to their ASCII
    base ("café" → "cafe") instead of stripping them outright. The
    earlier ``re.sub(r'[^A-Za-z0-9]+', '+', query)`` shape silently
    erased the entire query for any non-ASCII input — same bug the
    Fictionmania path was fixed for in 2.3.3.
    """
    if query:
        flat = (
            unicodedata.normalize("NFKD", query)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        keywords = re.sub(r"[^A-Za-z0-9]+", "+", flat).strip("+") or "+"
        url = (
            "https://darkwanderer.net/search/search?keywords="
            f"{keywords}&o=relevance"
        )
    else:
        url = "https://darkwanderer.net/forums/"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    # XenForo emits both bare thread links (``/threads/<slug>.<tid>/``)
    # and per-post permalinks (``/threads/<slug>.<tid>/post-<id>``);
    # search-results pages use the per-post form. Capture both and
    # dedupe by tid so each thread appears once.
    for m in re.finditer(
        r'href="/threads/([a-z0-9-]+)\.(\d+)(?:/(?:post-\d+)?)?"',
        html,
        re.IGNORECASE,
    ):
        slug, tid = m.group(1), m.group(2)
        if tid in seen:
            continue
        # Look for the link's text near this match (XenForo's anchor
        # tags wrap inline elements, so .get_text via BS4 would be
        # cleaner — but on search-results pages the text lives a few
        # tags deep). Use a quick regex to grab the bold title from
        # the result-row title element if present.
        seen.add(tid)
        title = slug.replace("-", " ").title()
        if not _matches_query(query, title, slug):
            continue
        out.append({
            "title": title, "author": "",
            "url": f"https://darkwanderer.net/threads/{slug}.{tid}/",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "cuckold", "status": "",
            "site": "darkwanderer",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_greatfeet(query: str, *, page: int = 1,
                     tags: Optional[list] = None,
                     **_: object) -> list[dict]:
    """GreatFeet: ``/tickles.htm`` lists recent stories by ``ts<N>.htm``
    href; older issues at ``/archiveN.htm`` (weekly issues 1..484+).
    The page is 1997-era HTML (unclosed ``<a>`` tags, inline font
    styling) so we lean on BeautifulSoup to let it tolerate the
    malformed markup, then read the link text as the story title.

    Tag-only searches for anything other than ``feet`` return ``[]``
    — GreatFeet's whole catalogue is the feet tag, so a tag-only
    ``bdsm`` lookup would otherwise leak the entire homepage as
    noise.

    We decompose inline ``<img>`` tags inside the link before reading
    the text — the "new!" / "hot!" marker images carry alt attributes
    like ``"Foot Fetish Offering"`` that would otherwise pollute the
    title."""
    del page  # tickles.htm is a single page — archive pages handle
    # older stories via a separate ``/archive<N>.htm`` route.
    if (
        tags and not query
        and not any(t.strip().lower() == "feet" for t in tags)
    ):
        return []
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
    argument and tags every row with ``site='literotica'``.

    Query + tag together: Literotica's tag-browse endpoint
    (``tags.literotica.com/<tag>``) ignores any free-text query — the
    URL IS the tag listing. ``search_literotica`` therefore drops the
    query whenever ``category`` is set, which silently returned
    unfiltered tag results for a "Harry Potter + bdsm" search.
    Post-filter the tag page on title/author/summary/fandom locally
    so query+tag behaves like "tag-browse narrowed by query".

    Tag translation goes through :func:`_translate_tag` — for
    Literotica this is mostly passthrough (the ``tags.literotica.com``
    subdomain is permissive about slug shape), but
    :data:`_LITEROTICA_TAG_SLUGS` overrides specific vocab tags to
    Literotica's preferred slug where they diverge.
    """
    category = ""
    if tags:
        first_tag = tags[0].strip().lower()
        translated = _translate_tag("literotica", first_tag)
        if translated is None:
            return []
        category = translated
    kwargs: dict = {}
    if category:
        kwargs["category"] = category
    try:
        results = search_literotica(query, page=page, **kwargs)
    except Exception as exc:
        logger.debug("literotica search failed: %s", exc)
        return []
    if query and category:
        results = [
            r for r in results
            if _matches_query(
                query,
                r.get("title", ""), r.get("author", ""),
                r.get("summary", ""), r.get("fandom", ""),
            )
        ]
    for r in results[:PER_SITE_LIMIT]:
        r["site"] = "literotica"
    return results[:PER_SITE_LIMIT]


def search_ao3_erotica(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       **_: object) -> list[dict]:
    """AO3 adapter for the erotica fan-out — restricts to Explicit
    rating and translates the first vocab tag to AO3's canonical
    freeform tag (see :data:`_AO3_TAG_SLUGS`).

    Why this lives in the erotica module instead of the existing
    :func:`ffn_dl.search.search_ao3`: the general AO3 search returns
    every rating, every fandom, every length. For the erotica
    discovery surface we want the same Explicit-only narrow that
    Literotica's tag-browse gives us by construction. The thin
    wrapper here pins ``rating='explicit'`` and routes vocab tags
    through the freeform field so e.g. ``feet`` → ``Foot Fetish``
    rather than landing in a free-text query that AO3's relevance
    sort can't make tight enough.

    Tags AO3 doesn't have a canonical name for return ``[]`` so the
    fan-out doesn't paper over noise with off-rating fandom hits.
    """
    from ..search import search_ao3

    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    if vocab_tags:
        translated = [_translate_tag("ao3", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        # AO3's freeform_names is a free-text field; comma-separated
        # values are AND-combined by the AO3 search engine.
        freeform_arg = ", ".join(translated)
    else:
        freeform_arg = ""

    if not query and not freeform_arg:
        # Without either a query or a tag, the AO3 search returns
        # every Explicit-rated work on the site — a 6M-work firehose
        # that's never what an erotica discovery search wants.
        return []

    kwargs = {"rating": "explicit", "sort": "kudos"}
    if freeform_arg:
        kwargs["freeform"] = freeform_arg
    try:
        results = search_ao3(query or "", page=page, **kwargs)
    except Exception as exc:
        logger.debug("AO3 erotica search failed: %s", exc)
        return []
    for r in results[:PER_SITE_LIMIT]:
        r["site"] = "ao3"
    return results[:PER_SITE_LIMIT]


def search_wattpad_erotica(query: str, *, page: int = 1,
                           tags: Optional[list] = None,
                           **_: object) -> list[dict]:
    """Wattpad adapter for the erotica fan-out — uses Wattpad's
    ``/stories/<tag>`` HTML tag page rather than the v4 API search
    that :func:`ffn_dl.search.search_wattpad` hits, because the tag
    page returns a directly-targeted JSON-LD ``ItemList`` block whose
    relevance is far tighter than an API free-text query for the same
    tag string.

    Translates vocab tags via :data:`_WATTPAD_TAG_SLUGS` (Wattpad
    uses ``foot-fetish`` / ``noncon`` / ``trans`` / ``dom-sub`` —
    slug shapes that don't match our vocab). Tags not in the table
    return ``[]`` rather than landing on Wattpad's stub 404 page.

    Wattpad's tag pages embed up to ~20 ``ListItem`` JSON-LD entries
    per page; the adapter parses those and caps at
    :data:`PER_SITE_LIMIT`. No pagination — multi-page browses would
    require a different (auth-gated) Wattpad endpoint.
    """
    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    slug = None
    if vocab_tags:
        translated = [_translate_tag("wattpad", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        slug = translated[0]

    if not slug and not query:
        # No query and no resolvable tag — nothing actionable.
        return []
    if not slug:
        # Free-text fallback uses Wattpad's slug-shape on the query
        # (lowercase, hyphens). Wattpad serves a search-results page
        # for arbitrary text inputs, but the tag-page format is what
        # this adapter parses; for a bare query, fold it into the
        # slug and let Wattpad decide whether to redirect or 404.
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        if not slug:
            return []

    url = f"https://www.wattpad.com/stories/{slug}"
    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    # JSON-LD ``ListItem`` blocks embed name + description + url for
    # each story in the tag listing. Parsing the JSON segment is more
    # durable than scraping rotating CSS classes. Tolerate optional
    # whitespace between fields — Wattpad's live response is
    # contiguous, but pretty-printed copies (and our test fixtures)
    # break across lines.
    list_item_re = re.compile(
        r'"@type":\s*"ListItem"\s*,'
        r'\s*"name":\s*"([^"]+)"\s*,'
        r'\s*"description":\s*"([^"]*)"\s*,'
        r'\s*"url":\s*"(https://www\.wattpad\.com/story/(\d+)-[^"]+)"',
        re.DOTALL,
    )
    for m in list_item_re.finditer(html):
        title = m.group(1).strip()
        summary = m.group(2).strip().replace("\\n", " ").replace("\\\"", '"')
        story_url = m.group(3)
        sid = m.group(4)
        if sid in seen or not title:
            continue
        if not _matches_query(query, title, summary):
            continue
        seen.add(sid)
        out.append({
            "title": title,
            "author": "",
            "url": story_url,
            "summary": summary[:500],
            "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "wattpad",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


def search_bdsmlibrary(query: str, *, page: int = 1,
                       tags: Optional[list] = None,
                       **_: object) -> list[dict]:
    """BDSM Library (bdsmlibrary.com) adapter.

    The site speaks plain HTTP only — HTTPS serves an expired
    certificate. Two endpoints feed this adapter:

    * Tag/code searches POST through the advanced search form at
      ``/stories/search.php`` with ``search=advanced``,
      ``codeforstory[<id>]=yes`` for the desired tag, and the
      site's standard ``orderby`` / ``arrange`` pagination knobs.
      The numeric IDs live in :data:`_BDSMLIB_TAG_CODES` —
      femdom resolves to code 13 (F/m, "one female dominating one
      male"), feet to code 41, etc.
    * Plain free-text or tag-less browses GET
      ``/stories/list.php?pos=N&sortby1=moddate&arrange1=DESC`` for
      the recent-stories view.

    Tag-only queries that don't translate return ``[]`` (the site's
    advanced search ignores the form submission with all codes set
    to Maybe and would return every story, swamping the result set).

    Story permalinks are ``/stories/story.php?storyid=N``.
    Position-based pagination steps by 10 rows per page; this
    adapter caps at :data:`PER_SITE_LIMIT` of those rows.
    """
    base = "http://www.bdsmlibrary.com"
    per_page = 10
    pos = max(0, (max(1, int(page)) - 1) * per_page)

    vocab_tags = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    code_id = None
    if vocab_tags:
        translated = [_translate_tag("bdsmlibrary", t) for t in vocab_tags]
        translated = [t for t in translated if t]
        if not translated:
            return []
        code_id = translated[0]

    if code_id:
        # Build the advanced-search GET URL. The form normally POSTs
        # but the same parameters work via GET, which is friendlier
        # to our retry / cache layer in BaseScraper._fetch.
        from urllib.parse import urlencode
        params = {
            "title": "", "author": "", "synopsis": "",
            "lowsize": "", "highsize": "",
            "orderby": "moddate", "arrange": "DESC",
            "search": "advanced",
            "searchit": "1",
            f"codeforstory[{code_id}]": "yes",
            "pos": pos,
        }
        url = f"{base}/stories/search.php?{urlencode(params)}"
    else:
        url = (
            f"{base}/stories/list.php"
            f"?pos={pos}&sortby1=moddate&arrange1=DESC"
        )

    html = _fetch(url)
    if not html:
        return []
    out: list[dict] = []
    seen = set()
    # Each story row carries ``story.php?storyid=N`` for the title
    # and ``author.php?authorid=N`` immediately after it. Pair them
    # in document order.
    pattern = re.compile(
        r'<a\s+href="/stories/story\.php\?storyid=(\d+)"[^>]*>([^<]+)</a>'
        r'(?:.*?<a\s+href="/stories/author\.php\?authorid=\d+"[^>]*>([^<]+)</a>)?',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(html):
        story_id, title, author = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
        if story_id in seen or not title:
            continue
        if not _matches_query(query, title, author):
            continue
        seen.add(story_id)
        out.append({
            "title": title,
            "author": author,
            "url": f"{base}/stories/story.php?storyid={story_id}",
            "summary": "", "words": "?", "chapters": "?",
            "rating": "M", "fandom": "", "status": "",
            "site": "bdsmlibrary",
        })
        if len(out) >= PER_SITE_LIMIT:
            break
    return out


# ── Fan-out ──────────────────────────────────────────────────────

_SITE_FNS: dict[str, Callable[..., list[dict]]] = {
    "literotica": search_literotica_wrapped,
    "ao3": search_ao3_erotica,
    "wattpad": search_wattpad_erotica,
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
    "bdsmlibrary": search_bdsmlibrary,
}


TAG_SITE_COVERAGE: dict[str, list[str]] = {
    # Which sites carry each tag as a first-class category or
    # well-represented kink. Used by the tag picker to annotate each
    # option with its site-count (see :func:`tag_site_count`) so users
    # can see at a glance whether a tag is well-covered or niche.
    # Entries only list sites where the tag has a native URL slug /
    # category / code (verified May 2026 — see :data:`_LITEROTICA_TAG_SLUGS`,
    # :data:`_LUSH_TAG_SLUGS`, :data:`_SOL_TAG_SLUGS`,
    # :data:`_MCS_TAG_CODES`, :data:`_AO3_TAG_SLUGS`,
    # :data:`_BDSMLIB_TAG_CODES`); sites where the tag would have to
    # ride along inside a free-text query don't count.
    "anal": [
        "literotica", "storiesonline", "lushstories", "sexstories", "ao3",
    ],
    "bdsm": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "wattpad", "bdsmlibrary",
    ],
    "body-worship": ["literotica", "ao3", "wattpad"],
    "bondage": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "bukkake": ["literotica", "sexstories", "ao3"],
    "celebrity": ["literotica", "storiesonline", "sexstories", "ao3", "wattpad"],
    "cfnm": ["literotica", "ao3", "wattpad"],
    "cheating": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer", "ao3",
    ],
    "chastity": ["literotica", "storiesonline", "ao3", "bdsmlibrary"],
    "cuckold": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer", "ao3",
    ],
    "cunnilingus": ["literotica", "lushstories", "storiesonline", "ao3"],
    "dominance-submission": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "exhibitionism": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "face-sitting": ["literotica", "lushstories", "ao3"],
    "femdom": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
        "wattpad", "bdsmlibrary",
    ],
    "feet": [
        "literotica", "lushstories", "storiesonline", "greatfeet", "ao3",
        "wattpad", "bdsmlibrary",
    ],
    "female-led": ["literotica", "ao3", "wattpad"],
    "foot-worship": [
        "literotica", "lushstories", "storiesonline", "greatfeet", "ao3",
        "wattpad",
    ],
    "footjob": [
        "literotica", "lushstories", "storiesonline", "greatfeet", "ao3",
    ],
    "fisting": [
        "literotica", "storiesonline", "sexstories", "ao3", "bdsmlibrary",
    ],
    "futanari": ["literotica", "storiesonline", "tgstorytime", "ao3"],
    "gangbang": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
    ],
    "gay": [
        "literotica", "lushstories", "storiesonline", "nifty",
        "sexstories", "aff", "mcstories", "ao3",
    ],
    "group-sex": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
    ],
    "harem": ["literotica", "storiesonline", "lushstories", "ao3"],
    "humiliation": [
        "literotica", "lushstories", "storiesonline", "mcstories", "ao3",
    ],
    "hypnosis": ["mcstories", "storiesonline", "literotica", "chyoa", "ao3"],
    "incest": [
        "literotica", "storiesonline", "sexstories", "aff", "mcstories",
        "ao3", "bdsmlibrary",
    ],
    "interracial": [
        "literotica", "lushstories", "storiesonline", "sexstories",
        "darkwanderer", "ao3", "bdsmlibrary",
    ],
    "lactation": [
        "literotica", "storiesonline", "sexstories", "mcstories", "ao3",
        "bdsmlibrary",
    ],
    "lesbian": [
        "literotica", "lushstories", "storiesonline", "nifty",
        "sexstories", "aff", "mcstories", "ao3",
    ],
    "masturbation": [
        "literotica", "lushstories", "sexstories", "mcstories", "ao3",
    ],
    "mature": ["literotica", "lushstories", "storiesonline", "ao3"],
    "mind-control": [
        "mcstories", "storiesonline", "literotica", "chyoa", "ao3",
    ],
    "non-consent": [
        "literotica", "storiesonline", "mcstories", "sexstories", "ao3",
        "bdsmlibrary",
    ],
    "oral": ["literotica", "lushstories", "sexstories", "ao3"],
    "orgy": ["literotica", "storiesonline", "sexstories", "ao3"],
    "pegging": [
        "literotica", "lushstories", "storiesonline", "ao3", "wattpad",
    ],
    "polyamory": ["literotica", "storiesonline", "lushstories", "ao3"],
    "pregnancy": ["literotica", "storiesonline", "sexstories", "ao3"],
    "public-sex": ["literotica", "lushstories", "storiesonline", "ao3"],
    "queening": ["literotica", "lushstories", "storiesonline", "ao3"],
    "roleplay": ["literotica", "lushstories", "chyoa", "ao3"],
    "rough": ["literotica", "lushstories", "sexstories", "ao3"],
    "spanking": [
        "literotica", "lushstories", "storiesonline", "ao3", "wattpad",
        "bdsmlibrary",
    ],
    "strap-on": ["literotica", "lushstories", "ao3", "wattpad"],
    "swinging": [
        "literotica", "lushstories", "storiesonline", "darkwanderer", "ao3",
    ],
    "tease-and-denial": ["literotica", "ao3", "wattpad"],
    "teen": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
        "bdsmlibrary",
    ],
    "threesome": [
        "literotica", "lushstories", "storiesonline", "sexstories", "ao3",
        "wattpad",
    ],
    "trampling": ["literotica", "greatfeet", "ao3", "wattpad"],
    "transgender": [
        "literotica", "fictionmania", "tgstorytime", "storiesonline",
        "lushstories", "ao3",
    ],
    "voyeur": [
        "literotica", "lushstories", "storiesonline", "ao3", "bdsmlibrary",
    ],
    "watersports": [
        "literotica", "storiesonline", "sexstories", "mcstories", "ao3",
        "bdsmlibrary",
    ],
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


# Sites that fold ``tags`` into their full-text search payload rather
# than having a native tag URL. ``search_sexstories`` extends its query
# terms with ``tags[:3]`` so any tag — including niche ones outside
# :data:`TAG_SITE_COVERAGE` — narrows the result set in a sensible way.
# Listing the site here lets the dispatcher include it for arbitrary
# tags instead of dropping it because our pre-computed coverage map
# happens to omit the tag.
_TAG_TEXT_FOLD_SITES: set[str] = {"sexstories"}


def _site_handles_any_tag(site: str, tags: list[str]) -> bool:
    """True when ``site`` can do *something useful* with at least one
    tag in ``tags``.

    Source of truth: :data:`TAG_SITE_COVERAGE` (which sites natively
    carry each vocab tag). The earlier slug-passthrough escape hatch
    for Lushstories has been retired now that each adapter routes
    tags through :func:`_translate_tag` — Lush returns ``[]`` for
    untranslatable tags rather than 200-ing a stub category page, so
    a passthrough flag here would only let off-topic dead-page calls
    into the fan-out.

    :data:`_TAG_TEXT_FOLD_SITES` still applies: SexStories folds tags
    into its full-text query and so handles arbitrary tags sensibly
    even outside :data:`TAG_SITE_COVERAGE`.

    The gate continues to skip sites whose scrapers genuinely ignore
    the ``tags`` kwarg (Fictionmania, TGStorytime, DarkWanderer, etc.)
    so a tag-only ``feet`` search doesn't include those archives'
    default browses as noise.
    """
    if not tags:
        return True
    for t in tags:
        tag_lower = t.strip().lower()
        if not tag_lower:
            continue
        if site in TAG_SITE_COVERAGE.get(tag_lower, []):
            return True
        if site in _TAG_TEXT_FOLD_SITES:
            return True
    return False


# Public alias for older callers / tests. Both names point at the new
# OR-semantics helper above; the older name simply describes the
# round-1 contract that no longer matches reality.
_site_supports_all_tags = _site_handles_any_tag


_SITE_LABEL_SLUG_RE = re.compile(r"\(([a-z][a-z0-9_-]*)\)\s*$")


def _extract_slug(label_or_slug: str) -> str:
    """Pull the slug from a ``"Label (slug)"`` formatted dropdown
    choice or pass a bare slug through unchanged. The GUI's Site
    dropdown shows friendly labels with a trailing ``(slug)`` marker
    so the user sees ``Adult-FanFiction.org (aff)`` instead of just
    ``aff``; this helper recovers the slug for the fan-out without
    requiring a separate label-to-slug map at the call site.

    Bare slugs (CLI, tests, scripted callers) round-trip unchanged.
    A full label without a slug marker also round-trips through the
    reverse-lookup of :data:`EROTICA_SITE_LABELS` for safety.
    """
    s = str(label_or_slug).strip()
    if not s:
        return s
    m = _SITE_LABEL_SLUG_RE.search(s)
    if m:
        return m.group(1)
    for slug, label in EROTICA_SITE_LABELS.items():
        if s == label:
            return slug
    return s


def _normalise_sites(sites, sites_choice) -> Optional[list]:
    """GUI passes ``sites_choice`` (a single string from the dropdown);
    CLI / tests pass ``sites`` (a list). Fold both into the list form
    the fan-out expects, or ``None`` for "search every site"."""
    if sites:
        if isinstance(sites, str):
            sites = [sites]
        slugs = [_extract_slug(s) for s in sites if s]
        return [s for s in slugs if s and s != "all"] or None
    if sites_choice:
        slug = _extract_slug(sites_choice)
        if slug and slug not in ("", "all"):
            return [slug]
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

    ``total_sites`` is the set of every site the initial search
    considered eligible — the canonical denominator for "have we
    exhausted everything?". The GUI used to compute this from
    ``len(site_stats)``, but on Load More the new call's
    ``site_stats`` only includes the currently-active sites (the ones
    not yet exhausted via ``skip_sites``), so the comparison would
    falsely trigger end-of-results. Tracking the canonical set on the
    result object keeps the GUI's exhaustion check honest.
    """

    site_stats: dict
    exhausted_sites: set
    total_sites: set

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.site_stats = {}
        self.exhausted_sites = set()
        self.total_sites = set()


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

    # Query-to-tag promotion: when the user typed a word that *is* a
    # known erotica tag and didn't pick any tags from the multi-picker,
    # promote the query to a tag search. This is exact-match only —
    # "feet" gets promoted; "feet fetish" does not — so the rule never
    # surprises a user who genuinely wanted a free-text search. Tag-
    # capable sites then use their native tag URL instead of falling
    # back to title filtering, which is dramatically better for broad
    # discovery searches like ``feet`` or ``femdom``.
    if not tag_list and query:
        normalised_query = query.strip().lower()
        if normalised_query in EROTICA_TAG_VOCABULARY:
            tag_list = [normalised_query]
            logger.info(
                "erotica: promoting bare query %r to tag-search",
                normalised_query,
            )

    if resolved_sites is None:
        active = [s for s in _SITE_FNS if s not in skip_set]
    else:
        active = [
            s for s in resolved_sites if s in _SITE_FNS and s not in skip_set
        ]

    # Tag-capability filter: drop sites that don't meaningfully cover
    # the requested tag(s) from this fan-out. Without this, sites
    # whose adapter ignores the ``tags`` kwarg (Fictionmania /
    # TGStorytime / DarkWanderer / GreatFeet for non-feet tags — they
    # accept ``**_: object``) silently fall back to a default browse
    # and flood the result set with unrelated rows.
    #
    # Semantics: OR across selected tags. A site stays in the fan-out
    # if it handles *any* selected tag — most per-site scrapers only
    # consume ``tags[0]`` anyway, so claiming AND semantics would
    # misrepresent what the scrapers actually deliver. See
    # :func:`_site_handles_any_tag` for the per-tag handling lookup.
    #
    # Only applied when the caller didn't restrict ``sites`` to a
    # specific slug — a user who explicitly picks one site has
    # already opted into whatever that site returns.
    if tag_list and resolved_sites is None:
        active = [
            s for s in active
            if _site_handles_any_tag(s, tag_list)
        ]
    # ``total_sites`` is captured BEFORE any further filtering so the
    # GUI's "all sites exhausted?" check has a stable denominator on
    # Load More — the per-call ``site_stats`` keys shrink as exhausted
    # sites are removed from subsequent fan-outs.
    initial_eligible = set(active)
    if not active:
        empty = ErotiCAResults()
        empty.total_sites = initial_eligible
        return empty

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
        new_merged.total_sites = initial_eligible
        merged = new_merged
    else:
        merged.site_stats = site_stats
        merged.total_sites = initial_eligible

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
