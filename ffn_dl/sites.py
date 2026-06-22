"""Central registry of supported sites.

Keeps site-detection logic in one place so the CLI, clipboard watcher,
and GUI share a single source of truth for URL patterns instead of
each maintaining their own copies.
"""

import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from .ao3 import AO3Scraper
from .erotica import (
    AFFScraper,
    BDSMLibraryScraper,
    ChyoaScraper,
    DarkWandererScraper,
    FictionmaniaScraper,
    GreatFeetScraper,
    LiteroticaScraper,
    LushStoriesScraper,
    MCStoriesScraper,
    NiftyScraper,
    SexStoriesScraper,
    StoriesOnlineScraper,
    TGStorytimeScraper,
)
from .ficwad import FicWadScraper
from .mediaminer import MediaMinerScraper
from .royalroad import RoyalRoadScraper
from .scraper import BaseScraper, FFNScraper
from .wattpad import WattpadScraper
from .webnovel import WebnovelScraper


# Story-URL patterns are declared with an explicit ``https?://`` scheme for
# readability, then loosened (see ``_loosen`` below) so a bare host works
# too — users paste ``fanfiction.net/s/123`` (no scheme, no www) as often
# as the full URL.
_STORY_URL_PATTERNS_STRICT: list[tuple[type[BaseScraper], re.Pattern[str]]] = [
    (FicWadScraper, re.compile(r"https?://(?:www\.)?ficwad\.com/story/\d+", re.I)),
    (
        AO3Scraper,
        re.compile(
            r"https?://(?:www\.)?(?:archiveofourown\.org|ao3\.org)/works/\d+",
            re.I,
        ),
    ),
    (
        RoyalRoadScraper,
        re.compile(r"https?://(?:www\.)?royalroad\.com/fiction/\d+", re.I),
    ),
    (
        MediaMinerScraper,
        re.compile(
            r"https?://(?:www\.)?mediaminer\.org/fanfic/"
            r"(?:view_st\.php/\d+|s/[^?#\s]+?/\d+)",
            re.I,
        ),
    ),
    (
        LiteroticaScraper,
        re.compile(r"https?://(?:www\.)?literotica\.com/s/[a-z0-9-]+", re.I),
    ),
    (
        WattpadScraper,
        re.compile(
            r"https?://(?:www\.|m\.)?wattpad\.com/(?:story/)?\d+", re.I
        ),
    ),
    # ── Erotica subpackage ───────────────────────────────────────
    (
        AFFScraper,
        re.compile(
            r"https?://[a-z0-9-]+\.adult-fanfiction\.org/story\.php\?no=\d+",
            re.I,
        ),
    ),
    (
        StoriesOnlineScraper,
        re.compile(r"https?://(?:www\.)?storiesonline\.net/s/\d+", re.I),
    ),
    (
        NiftyScraper,
        re.compile(r"https?://(?:www\.)?nifty\.org/nifty/[a-z0-9/_-]+", re.I),
    ),
    (
        SexStoriesScraper,
        re.compile(r"https?://(?:www\.)?sexstories\.com/story/\d+", re.I),
    ),
    (
        MCStoriesScraper,
        re.compile(
            r"https?://(?:www\.)?mcstories\.com/[A-Za-z][A-Za-z0-9_-]+/?",
            re.I,
        ),
    ),
    (
        LushStoriesScraper,
        re.compile(
            r"https?://(?:www\.)?lushstories\.com/stories/[a-z0-9-]+/[a-z0-9-]+",
            re.I,
        ),
    ),
    (
        FictionmaniaScraper,
        re.compile(
            r"https?://(?:www\.)?fictionmania\.tv/stories/read(?:html|text)story\.html\?storyID=\d+",
            re.I,
        ),
    ),
    (
        TGStorytimeScraper,
        re.compile(
            r"https?://(?:www\.)?tgstorytime\.com/viewstory\.php\?sid=\d+",
            re.I,
        ),
    ),
    (
        ChyoaScraper,
        re.compile(
            r"https?://(?:www\.)?chyoa\.com/(?:story|chapter)/[^/?#\s]+\.\d+",
            re.I,
        ),
    ),
    (
        DarkWandererScraper,
        re.compile(
            r"https?://(?:www\.)?darkwanderer\.net/threads/[^/.]+\.\d+",
            re.I,
        ),
    ),
    (
        GreatFeetScraper,
        re.compile(
            r"https?://(?:www\.)?greatfeet\.com/stories/ts\d+\.htm", re.I,
        ),
    ),
    (
        BDSMLibraryScraper,
        re.compile(
            r"https?://(?:www\.)?bdsmlibrary\.com/stories/"
            r"(?:story|chapter)\.php\?storyid=\d+",
            re.I,
        ),
    ),
    (
        WebnovelScraper,
        re.compile(
            r"https?://(?:www\.|m\.)?webnovel\.com/book/(?:[^/?#]*_)?\d+",
            re.I,
        ),
    ),
    (
        FFNScraper,
        re.compile(r"https?://(?:www\.)?fanfiction\.net/s/\d+", re.I),
    ),
]


# Left-boundary guard + optional scheme. The guard (negative lookbehind on
# word chars, ``@``, ``.``, ``-``) stops a host fragment inside a larger
# token — ``notfanfiction.net`` — from matching ``fanfiction.net``, which
# the mandatory scheme used to prevent before it became optional.
_URL_PREFIX = r"(?<![\w@.\-])(?:https?://)?"


def _loosen(pattern: re.Pattern[str]) -> re.Pattern[str]:
    """Return ``pattern`` with its leading ``https?://`` made optional and
    the left-boundary guard prepended, so bare-host URLs match too."""
    src = pattern.pattern
    if src.startswith("https?://"):
        src = src[len("https?://"):]
    return re.compile(_URL_PREFIX + src, pattern.flags)


_STORY_URL_PATTERNS: list[tuple[type[BaseScraper], re.Pattern[str]]] = [
    (scraper_cls, _loosen(pattern))
    for scraper_cls, pattern in _STORY_URL_PATTERNS_STRICT
]

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def _ensure_scheme(url: str) -> str:
    """Prepend ``https://`` to a bare-host URL (``fanfiction.net/s/1``) so
    :func:`urlsplit` can populate the hostname. URLs that already carry a
    scheme, and bare ids / free text with no dotted host, pass through
    unchanged — preserving the FFN-fallback for bare numeric ids.
    """
    s = (url or "").strip()
    if not s or _SCHEME_RE.match(s):
        return s
    head = s.split("/", 1)[0]
    return f"https://{s}" if "." in head else s


# Hostname fragments for sites that don't require the full /s/N etc.
# path — used when the caller already knows they have a story URL and
# just needs to pick the scraper class (e.g. after the user pastes a
# bare URL or the CLI has the full argument in hand).
_HOSTNAME_TO_SCRAPER: list[tuple[str, type[BaseScraper]]] = [
    ("ficwad.com", FicWadScraper),
    ("archiveofourown.org", AO3Scraper),
    ("ao3.org", AO3Scraper),
    ("royalroad.com", RoyalRoadScraper),
    ("mediaminer.org", MediaMinerScraper),
    ("literotica.com", LiteroticaScraper),
    ("wattpad.com", WattpadScraper),
    ("webnovel.com", WebnovelScraper),
    ("adult-fanfiction.org", AFFScraper),
    ("storiesonline.net", StoriesOnlineScraper),
    ("nifty.org", NiftyScraper),
    ("sexstories.com", SexStoriesScraper),
    ("mcstories.com", MCStoriesScraper),
    ("lushstories.com", LushStoriesScraper),
    ("fictionmania.tv", FictionmaniaScraper),
    ("tgstorytime.com", TGStorytimeScraper),
    ("chyoa.com", ChyoaScraper),
    ("darkwanderer.net", DarkWandererScraper),
    ("greatfeet.com", GreatFeetScraper),
    ("bdsmlibrary.com", BDSMLibraryScraper),
]

# Scrapers whose is_author_url / is_series_url static methods should be
# consulted when classifying a URL.
ALL_SCRAPERS: list[type[BaseScraper]] = [
    FFNScraper,
    FicWadScraper,
    AO3Scraper,
    RoyalRoadScraper,
    MediaMinerScraper,
    LiteroticaScraper,
    WattpadScraper,
    WebnovelScraper,
    AFFScraper,
    StoriesOnlineScraper,
    NiftyScraper,
    SexStoriesScraper,
    MCStoriesScraper,
    LushStoriesScraper,
    FictionmaniaScraper,
    TGStorytimeScraper,
    ChyoaScraper,
    DarkWandererScraper,
    GreatFeetScraper,
    BDSMLibraryScraper,
]

# Erotica-specific scraper classes, exported for the unified Erotic
# Story Search window. Keeping the tuple here (rather than inside
# ``erotica/__init__.py``) lets callers ask "is this scraper erotica?"
# without pulling in the whole erotica subpackage symbol table.
EROTICA_SCRAPERS: tuple[type[BaseScraper], ...] = (
    LiteroticaScraper,
    AFFScraper,
    StoriesOnlineScraper,
    NiftyScraper,
    SexStoriesScraper,
    MCStoriesScraper,
    LushStoriesScraper,
    FictionmaniaScraper,
    TGStorytimeScraper,
    ChyoaScraper,
    DarkWandererScraper,
    GreatFeetScraper,
    BDSMLibraryScraper,
)


def detect_scraper(url: str) -> type[BaseScraper]:
    """Return the scraper class that handles ``url``.

    Matches against the parsed hostname rather than substring-searching
    the entire URL — otherwise a URL whose path or query happens to
    contain a known site's name (``https://example.com/?ref=ao3.org``)
    misroutes to that scraper, then fails awkwardly inside its parser
    instead of falling through cleanly. We compare ``host == hostname``
    or ``host.endswith("." + hostname)`` so subdomains like
    ``hp.adult-fanfiction.org`` match their root scraper.

    Falls back to FFNScraper for bare numeric IDs and unrecognised
    hostnames — FFN has historically been the default "just give me a
    number" behaviour.
    """
    text = _ensure_scheme(str(url))
    host = ""
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").lower()
    except (ValueError, AttributeError):
        host = ""
    if host:
        for hostname, scraper_cls in _HOSTNAME_TO_SCRAPER:
            if host == hostname or host.endswith("." + hostname):
                return scraper_cls
    return FFNScraper


def is_author_url(url: str) -> bool:
    """Return True if ``url`` is an author page on any supported site."""
    return any(cls.is_author_url(url) for cls in ALL_SCRAPERS)


def is_series_url(url: str) -> bool:
    """Return True if ``url`` is a series page (AO3 or Literotica)."""
    return AO3Scraper.is_series_url(url) or LiteroticaScraper.is_series_url(url)


def extract_story_url(text: str) -> Optional[str]:
    """Return the first supported story URL found in ``text``, or None.

    Used by the clipboard watcher — users paste whole paragraphs or
    URLs-with-query-strings, and we want the canonical story URL we
    know how to download.
    """
    for _, pattern in _STORY_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return _ensure_scheme(match.group(0))
    return None


# ---------------------------------------------------------------------------
# URL canonicalisation
#
# Two files embedding different URL forms of the same story must collapse
# to one index entry — otherwise the library scanner records two stale
# copies and duplicate detection misses them. Observed variation in real
# libraries (from one 817-file sample):
#
#   https://www.fanfiction.net/s/12345        ← canonical
#   http://www.fanfiction.net/s/12345         ← http
#   https://www.fanfiction.net/s/12345/       ← trailing slash
#   https://www.fanfiction.net/s/12345/1/     ← chapter suffix
#   https://www.fanfiction.net/s/12345/1/Title-Slug  ← chapter + slug
#   http://archiveofourown.org/works/12345    ← http AO3
#   https://archiveofourown.org/works/12345   ← canonical AO3
#
# ``canonical_url`` maps all of these to a single per-site canonical
# form, so the library index and the watchlist store can rely on a
# byte-identical key for the "same" story.
# ---------------------------------------------------------------------------

# Per-site rewrite rules. Each tuple is
#   (hostname_fragment, canonical_hostname, id_path_regex, canonical_path_template)
# ``id_path_regex`` matches the path portion of the URL and captures the
# story identifier; the canonical form is ``canonical_path_template``
# interpolated with that capture.
_CANONICAL_RULES: list[tuple[str, str, re.Pattern[str], str]] = [
    (
        "fanfiction.net", "www.fanfiction.net",
        re.compile(r"^/s/(\d+)"), "/s/{}",
    ),
    (
        "archiveofourown.org", "archiveofourown.org",
        re.compile(r"^/works/(\d+)"), "/works/{}",
    ),
    (
        "ao3.org", "archiveofourown.org",
        re.compile(r"^/works/(\d+)"), "/works/{}",
    ),
    (
        "royalroad.com", "www.royalroad.com",
        re.compile(r"^/fiction/(\d+)"), "/fiction/{}",
    ),
    (
        "ficwad.com", "ficwad.com",
        re.compile(r"^/story/(\d+)"), "/story/{}",
    ),
    (
        "mediaminer.org", "www.mediaminer.org",
        # Two MediaMiner URL shapes exist: /fanfic/view_st.php/<id> and
        # /fanfic/s/<slug>/<id>. Both trail with the numeric id, which
        # we canonicalise to view_st.php.
        re.compile(r"^/fanfic/(?:view_st\.php|s/[^/]+)/(\d+)"),
        "/fanfic/view_st.php/{}",
    ),
    (
        "literotica.com", "www.literotica.com",
        # Literotica story ids are slugs, not integers.
        re.compile(r"^/s/([a-z0-9\-]+)"), "/s/{}",
    ),
    (
        "wattpad.com", "www.wattpad.com",
        # Wattpad accepts /story/<id> and /<id>-<slug>; collapse both to
        # the /story/<id> form that the scraper uses as its canonical.
        re.compile(r"^/(?:story/)?(\d+)"), "/story/{}",
    ),
    (
        "webnovel.com", "www.webnovel.com",
        # /book/<id> or /book/<slug>_<id>; collapse both to bare /book/<id>.
        re.compile(r"^/book/(?:[^/?#]*_)?(\d+)"), "/book/{}",
    ),
    (
        "storiesonline.net", "storiesonline.net",
        re.compile(r"^/s/(\d+)"), "/s/{}",
    ),
    (
        "nifty.org", "www.nifty.org",
        re.compile(r"^/(nifty/[a-z0-9/_-]+?)/?$"), "/{}/",
    ),
    (
        "sexstories.com", "www.sexstories.com",
        re.compile(r"^/story/(\d+)"), "/story/{}",
    ),
    (
        "mcstories.com", "mcstories.com",
        re.compile(r"^/([A-Za-z][A-Za-z0-9_-]+)/?(?:index\.html)?$"),
        "/{}/",
    ),
    (
        "lushstories.com", "www.lushstories.com",
        re.compile(
            r"^/stories/([a-z0-9-]+/[a-z0-9][a-z0-9-]+)/?$", re.I,
        ),
        "/stories/{}",
    ),
    (
        "chyoa.com", "chyoa.com",
        # Chyoa URL shape: /story/<slug>.<id> or /chapter/<slug>.<id>.
        # We canonicalise both to the chapter form because that's what
        # the scraper operates on (the story URL redirects to the root
        # chapter of the same tree). ``re.I`` because chyoa serves the
        # same chapter at /chapter/Foo.99 and /CHAPTER/Foo.99 — without
        # case-insensitive matching the second form falls through to
        # the unknown-host fallback and fails to dedupe against the first.
        re.compile(r"^/(?:story|chapter)/([^/?#\s]+\.\d+)/?$", re.I),
        "/chapter/{}",
    ),
    (
        "darkwanderer.net", "darkwanderer.net",
        re.compile(r"^/threads/([^/.]+\.\d+)"),
        "/threads/{}/",
    ),
    (
        "greatfeet.com", "www.greatfeet.com",
        re.compile(r"^/stories/ts(\d+)\.htm$"), "/stories/ts{}.htm",
    ),
]

# Sites whose story id lives in the query string rather than the path.
# ``canonical_url`` special-cases these so we don't drop the query
# during its normal "strip query and fragment" cleanup.
_AFF_NO_RE = re.compile(r"(?:^|[?&])no=(\d+)")
_FM_STORY_RE = re.compile(r"(?:^|[?&])storyID=(\d+)", re.I)
_BDSMLIB_STORYID_RE = re.compile(r"(?:^|[?&])storyid=(\d+)", re.I)
_TGS_SID_RE = re.compile(r"(?:^|[?&])sid=(\d+)", re.I)


def canonical_url(url: str) -> str:
    """Return the canonical form of a supported-site story URL.

    All known variations for a given story (http/https, with or without
    ``www.``, trailing slash, ``/1/`` chapter suffix, title slug) collapse
    to a single deterministic string. Unsupported URLs are returned
    lowercased and scheme-normalised but otherwise unchanged so callers
    always get something stable to use as a dict key.

    Empty strings are passed through — the caller (library index) treats
    "no URL" and "empty URL" the same way.
    """
    if not url:
        return ""
    raw = _ensure_scheme(url.strip())
    parts = urlsplit(raw)
    netloc = parts.netloc.lower()
    path = parts.path

    # AFF is subdomain-per-fandom with id in ``?no=<N>`` — preserve both
    # the subdomain (different subs carry different stories at the same
    # id) and the query parameter while dropping everything else.
    if netloc.endswith("adult-fanfiction.org"):
        m = _AFF_NO_RE.search(parts.query or "")
        if m:
            return urlunsplit(
                ("https", netloc, "/story.php", f"no={m.group(1)}", "")
            )

    # Fictionmania's id lives in ``?storyID=<N>`` on readhtmlstory.html.
    # Canonical form pins it to the HTML-reader URL so the text-reader
    # fallback still collapses to the same key.
    if "fictionmania.tv" in netloc:
        m = _FM_STORY_RE.search(parts.query or "")
        if m:
            return urlunsplit((
                "https", "fictionmania.tv",
                "/stories/readhtmlstory.html",
                f"storyID={m.group(1)}", "",
            ))

    # TGStorytime ids live in ``?sid=<N>``. Canonical form strips the
    # age-consent / chapter / textsize params that otherwise churn
    # between URL variants for the same work.
    if "tgstorytime.com" in netloc:
        m = _TGS_SID_RE.search(parts.query or "")
        if m:
            return urlunsplit((
                "https", "www.tgstorytime.com",
                "/viewstory.php",
                f"sid={m.group(1)}", "",
            ))

    # BDSM Library ids live in ``?storyid=<N>`` on story.php and
    # chapter.php; canonicalise both to the story page so the chapter
    # variant of a URL still dedupes against its story root. The site
    # only speaks plain HTTP (HTTPS cert is expired) — preserve that.
    if "bdsmlibrary.com" in netloc:
        m = _BDSMLIB_STORYID_RE.search(parts.query or "")
        if m:
            return urlunsplit((
                "http", "www.bdsmlibrary.com",
                "/stories/story.php",
                f"storyid={m.group(1)}", "",
            ))

    for host_fragment, canonical_host, path_re, path_template in _CANONICAL_RULES:
        if host_fragment not in netloc:
            continue
        match = path_re.match(path)
        if not match:
            continue
        return urlunsplit(
            ("https", canonical_host, path_template.format(match.group(1)), "", "")
        )

    # Unknown host: at least normalise scheme to https, drop ``www.``
    # (if present), drop query/fragment, and strip a trailing slash so
    # minor URL variants still dedupe.
    fallback_host = netloc
    if fallback_host.startswith("www."):
        fallback_host = fallback_host[len("www."):]
    return urlunsplit(("https", fallback_host, path.rstrip("/"), "", ""))
