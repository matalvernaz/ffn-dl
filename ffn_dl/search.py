"""Search fanfiction.net, Archive of Our Own, and Royal Road."""

import logging
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup, NavigableString
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

FFN_BASE = "https://www.fanfiction.net"
AO3_BASE = "https://archiveofourown.org"
RR_BASE = "https://www.royalroad.com"
LIT_BASE = "https://www.literotica.com"
LIT_TAGS_BASE = "https://tags.literotica.com"
SEARCH_PATH = "/search/"
DEFAULT_LIMIT = 25


# ── Filter option tables ──────────────────────────────────────────
# Keys are the human-readable labels shown in CLI --choices / GUI combos.
# Values are the numeric IDs FFN's search form submits.
# `None` means "no filter" — omit the param entirely.

FFN_RATING = {
    "all": None,
    "K": 1,
    "K+": 2,
    "T": 3,
    "M": 4,
    "K-T": 103,
}

FFN_STATUS = {
    "all": None,
    "in-progress": 1,
    "complete": 2,
}

FFN_GENRE = {
    "any": None,
    "general": 1,
    "romance": 2,
    "humor": 3,
    "drama": 4,
    "poetry": 5,
    "adventure": 6,
    "mystery": 7,
    "horror": 8,
    "parody": 9,
    "angst": 10,
    "supernatural": 11,
    "suspense": 12,
    "sci-fi": 13,
    "fantasy": 14,
    "tragedy": 16,
    "crime": 18,
    "family": 19,
    "hurt/comfort": 20,
    "friendship": 21,
}

FFN_WORDS = {
    "any": None,
    "<1k": 1,
    "<5k": 2,
    "5k+": 3,
    "10k+": 4,
    "30k+": 5,
    "50k+": 6,
    "150k+": 7,
    "300k+": 8,
}

FFN_LANGUAGE = {
    "any": None,
    "english": 1,
    "spanish": 2,
    "french": 3,
    "german": 4,
    "chinese": 5,
    "dutch": 7,
    "portuguese": 8,
    "russian": 10,
    "italian": 11,
    "polish": 13,
    "hungarian": 14,
    "swedish": 17,
    "norwegian": 18,
    "danish": 19,
    "finnish": 20,
    "turkish": 30,
    "czech": 31,
    "indonesian": 32,
    "vietnamese": 37,
}

FFN_CROSSOVER = {
    "any": None,
    "only": 1,
    "exclude": 2,
}

FFN_MATCH = {
    "any": None,
    "title": "title",
    "summary": "summary",
}

FFN_SORT = {
    "best match": None,
    "updated": 1,
    "published": 2,
    "reviews": 3,
    "favorites": 4,
    "follows": 5,
}


def _resolve_filter(value, choices, name):
    """Map a user value (label or raw ID) to a FFN param value.

    Label matching is case-insensitive so callers can pass natural-case
    labels like "K+" or "English" without having to remember the table
    casing.
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    lower = s.lower()
    for key, resolved in choices.items():
        if key.lower() == lower:
            return resolved
    valid = ", ".join(k for k in choices if k not in ("any", "all"))
    raise ValueError(f"Unknown {name}: {value!r}. Valid: {valid}")


def _build_search_url(query, filters, page=1):
    params = {"keywords": query, "ready": 1, "type": "story"}

    mapping = [
        ("censorid", "rating", FFN_RATING),
        ("languageid", "language", FFN_LANGUAGE),
        ("statusid", "status", FFN_STATUS),
        ("genreid", "genre", FFN_GENRE),
        # FFN's search form has a second genre dropdown (AND filter);
        # same value table as the first.
        ("genreid2", "genre2", FFN_GENRE),
        ("words", "min_words", FFN_WORDS),
        ("formatid", "crossover", FFN_CROSSOVER),
        ("match", "match", FFN_MATCH),
        ("sortid", "sort", FFN_SORT),
    ]
    for param, key, table in mapping:
        value = filters.get(key)
        resolved = _resolve_filter(value, table, key)
        if resolved is not None:
            params[param] = resolved

    if page and page > 1:
        params["ppage"] = int(page)

    return FFN_BASE + SEARCH_PATH + "?" + urlencode(params)


def _fetch_search_page(url):
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Search request failed (HTTP {resp.status_code}). "
            "FFN may be blocking requests — try again later."
        )
    lower = resp.text[:2000].lower()
    if "just a moment" in lower and "cloudflare" in lower:
        raise RuntimeError(
            "Cloudflare challenge detected. Try again in a few minutes."
        )
    return resp.text


def _extract_title(stitle_tag):
    """Extract the story title from the stitle link, preserving spaces
    between bold-wrapped keywords and surrounding text."""
    parts = []
    for child in stitle_tag.children:
        # Skip the cover image thumbnail
        if hasattr(child, "name") and child.name == "img":
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
        else:
            parts.append(child.get_text())
    return " ".join("".join(parts).split())


def _parse_results(html):
    """Parse the FFN search results HTML and return a list of result dicts."""
    soup = BeautifulSoup(html, "lxml")
    result_divs = soup.find_all("div", class_="z-list")
    results = []

    for div in result_divs:
        stitle = div.find("a", class_="stitle")
        if not stitle:
            continue

        href = stitle.get("href", "")
        url = FFN_BASE + href if href else ""
        title = _extract_title(stitle)

        author_tag = div.find("a", href=lambda h: h and "/u/" in h)
        author = author_tag.get_text(strip=True) if author_tag else "Unknown"

        # Summary is the text content of z-indent before the metadata div
        zindent = div.find("div", class_="z-indent")
        summary = ""
        if zindent:
            summary_parts = []
            for child in zindent.children:
                if hasattr(child, "attrs") and "z-padtop2" in child.get(
                    "class", []
                ):
                    break
                text = (
                    child.get_text(" ", strip=True)
                    if hasattr(child, "get_text")
                    else str(child).strip()
                )
                if text:
                    summary_parts.append(text)
            summary = " ".join(summary_parts)

        # Metadata from the gray div
        meta_div = div.find("div", class_="z-padtop2")
        meta_text = meta_div.get_text(" ", strip=True) if meta_div else ""

        words_m = re.search(r"Words:\s*([\d,]+)", meta_text)
        chapters_m = re.search(r"Chapters:\s*(\d+)", meta_text)
        rating_m = re.search(r"Rated:\s*(\S+)", meta_text)
        status_m = re.search(r"\bComplete\b", meta_text)

        # Fandom is the first segment before " - Rated:"
        fandom = ""
        fandom_m = re.match(r"^(.+?)\s*-\s*Rated:", meta_text)
        if fandom_m:
            fandom = fandom_m.group(1).strip()

        results.append(
            {
                "title": title,
                "author": author,
                "url": url,
                "summary": summary,
                "words": words_m.group(1) if words_m else "?",
                "chapters": chapters_m.group(1) if chapters_m else "1",
                "rating": rating_m.group(1) if rating_m else "?",
                "fandom": fandom,
                "status": "Complete" if status_m else "In-Progress",
            }
        )

    return results


def search_ffn(query, *, page=1, **filters):
    """Search fanfiction.net and return a list of result dicts.

    Keyword filters (all optional — pass a label from the corresponding
    FFN_* table, or the raw numeric ID):
        rating: all / K / K+ / T / M / K-T
        language: english / spanish / french / ... (see FFN_LANGUAGE)
        status: all / in-progress / complete
        genre: romance / humor / adventure / angst / ... (see FFN_GENRE)
        genre2: same values as `genre`; adds a second AND-filtered genre.
        min_words: <1k / <5k / 5k+ / 30k+ / 50k+ / 150k+ / 300k+
        crossover: any / only / exclude
        match: any / title / summary  (where the keywords must appear)

    Each result dict has keys: title, author, url, summary, words,
    chapters, rating, fandom, status.

    `page` (keyword-only) selects a specific results page for "load more"
    workflows — defaults to 1.
    """
    url = _build_search_url(query, filters, page=page)
    html = _fetch_search_page(url)
    return _parse_results(html)


# ── AO3 search ────────────────────────────────────────────────────

AO3_RATING = {
    "all": None,
    "not rated": 9,
    "general": 10,
    "teen": 11,
    "mature": 12,
    "explicit": 13,
}

AO3_COMPLETE = {
    "any": None,
    "complete": "T",
    "in-progress": "F",
}

AO3_CROSSOVER = {
    "any": None,
    "only": "T",
    "exclude": "F",
}

AO3_SORT = {
    "best match": "_score",
    "author": "authors_to_sort_on",
    "title": "title_to_sort_on",
    "date posted": "created_at",
    "date updated": "revised_at",
    "word count": "word_count",
    "hits": "hits",
    "kudos": "kudos_count",
    "comments": "comments_count",
    "bookmarks": "bookmarks_count",
}

# AO3 category IDs (work_search[category_ids][]). These are global
# tag IDs assigned by AO3 to each of the six top-level relationship
# categories; values sourced from AO3's search form source.
AO3_CATEGORY = {
    "any": None,
    "gen": 21,
    "f/m": 22,
    "m/m": 23,
    "f/f": 116,
    "multi": 2246,
    "other": 24,
}

# AO3 accepts either the short language code (ISO-ish — "en", "zh") or
# the numeric language_id in `work_search[language_id]`. Short codes are
# stable across AO3's DB rebuilds, so we use those. The pretty-label
# lookup here just saves users from memorizing codes; anything unknown
# is passed through so raw codes still work for languages not listed.
AO3_LANGUAGES = {
    "any": None,
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "polish": "pl",
    "dutch": "nl",
    "swedish": "sv",
    "norwegian": "no",
    "danish": "da",
    "finnish": "fi",
    "turkish": "tr",
    "arabic": "ar",
    "hebrew": "he",
    "indonesian": "id",
    "vietnamese": "vi",
    "czech": "cs",
    "hungarian": "hu",
    "greek": "el",
    "ukrainian": "uk",
    "thai": "th",
    "latin": "la",
}


def _build_ao3_search_url(query, filters, page=1):
    params = {}
    if query:
        params["work_search[query]"] = query
    if page and page > 1:
        params["page"] = int(page)

    def resolve(value, choices, name):
        if value is None or value == "":
            return None
        s = str(value).strip()
        if s.isdigit():
            return int(s) if name != "complete" and name != "crossover" else s
        lower = s.lower()
        for key, resolved in choices.items():
            if key.lower() == lower:
                return resolved
        valid = ", ".join(k for k in choices if k != "any" and k != "all")
        raise ValueError(f"Unknown {name}: {value!r}. Valid: {valid}")

    rating = resolve(filters.get("rating"), AO3_RATING, "rating")
    if rating is not None:
        params["work_search[rating_ids]"] = rating

    complete = resolve(filters.get("complete"), AO3_COMPLETE, "complete")
    if complete is not None:
        params["work_search[complete]"] = complete

    crossover = resolve(filters.get("crossover"), AO3_CROSSOVER, "crossover")
    if crossover is not None:
        params["work_search[crossover]"] = crossover

    category = resolve(filters.get("category"), AO3_CATEGORY, "category")
    if category is not None:
        # AO3 expects category_ids as an array param — urlencode handles
        # the [] suffix when we feed it a list under the same key.
        params["work_search[category_ids][]"] = category

    sort = resolve(filters.get("sort"), AO3_SORT, "sort")
    if sort is not None:
        params["work_search[sort_column]"] = sort

    if filters.get("single_chapter"):
        params["work_search[single_chapter]"] = 1

    # Language: accept a pretty label from AO3_LANGUAGES, or a raw ISO
    # code / numeric ID passed through verbatim.
    lang_raw = filters.get("language")
    if lang_raw:
        lang_str = str(lang_raw).strip()
        matched = None
        for label, code in AO3_LANGUAGES.items():
            if label.lower() == lang_str.lower():
                matched = code
                break
        params["work_search[language_id]"] = matched if matched else lang_str

    # Free-text AO3 fields pass straight through
    for key, param in [
        ("fandom", "work_search[fandom_names]"),
        ("word_count", "work_search[word_count]"),
        ("character", "work_search[character_names]"),
        ("relationship", "work_search[relationship_names]"),
        ("freeform", "work_search[freeform_names]"),
        ("title", "work_search[title]"),
        ("creator", "work_search[creators]"),
    ]:
        value = filters.get(key)
        if value:
            params[param] = value

    return AO3_BASE + "/works/search?" + urlencode(params)


def _parse_ao3_results(html):
    soup = BeautifulSoup(html, "lxml")
    results = []
    works_ol = soup.find("ol", class_="work")
    if not works_ol:
        return results

    for li in works_ol.find_all("li", recursive=False):
        heading = li.find("h4", class_="heading")
        if not heading:
            continue

        title_link = heading.find("a", href=re.compile(r"^/works/\d+"))
        if not title_link:
            continue
        title = title_link.get_text(strip=True)
        work_id_m = re.search(r"/works/(\d+)", title_link["href"])
        if not work_id_m:
            continue
        url = f"{AO3_BASE}/works/{work_id_m.group(1)}"

        # Authors are the other <a> tags in the heading (all but the title link)
        authors = [
            a.get_text(strip=True)
            for a in heading.find_all("a")
            if a is not title_link and "/users/" in a.get("href", "")
        ]
        author = ", ".join(authors) if authors else "Anonymous"

        fandoms_h5 = li.find("h5", class_="fandoms")
        fandom = ""
        if fandoms_h5:
            fandoms = [a.get_text(strip=True) for a in fandoms_h5.find_all("a")]
            fandom = ", ".join(fandoms)

        summary_bq = li.find("blockquote", class_="summary")
        summary = summary_bq.get_text(" ", strip=True) if summary_bq else ""

        stats_dl = li.find("dl", class_="stats")
        words = "?"
        chapters = "1"
        status = "In-Progress"
        if stats_dl:
            w = stats_dl.find("dd", class_="words")
            if w:
                words = w.get_text(strip=True)
            c = stats_dl.find("dd", class_="chapters")
            if c:
                ratio = c.get_text(strip=True)
                parts = ratio.split("/")
                if parts:
                    chapters = parts[0]
                if len(parts) == 2 and parts[0] == parts[1]:
                    status = "Complete"

        rating = "?"
        rating_li = li.find("span", class_="rating")
        if rating_li:
            rating = rating_li.get("title") or rating_li.get_text(strip=True)

        # Series membership — AO3 blurbs show "Part N of <a>Series</a>"
        series_entries = []
        for s_li in li.select("ul.series li"):
            s_link = s_li.find("a", href=re.compile(r"^/series/\d+"))
            if not s_link:
                continue
            s_id_m = re.search(r"/series/(\d+)", s_link["href"])
            if not s_id_m:
                continue
            part_m = re.match(
                r"Part\s+(\d+)\s+of", s_li.get_text(" ", strip=True), re.I,
            )
            series_entries.append({
                "id": s_id_m.group(1),
                "name": s_link.get_text(strip=True),
                "url": f"{AO3_BASE}/series/{s_id_m.group(1)}",
                "part": int(part_m.group(1)) if part_m else None,
            })

        results.append(
            {
                "title": title,
                "author": author,
                "url": url,
                "summary": summary,
                "words": words,
                "chapters": chapters,
                "rating": rating,
                "fandom": fandom,
                "status": status,
                "series": series_entries,
            }
        )

    return results


def search_ao3(query, *, page=1, **filters):
    """Search Archive of Our Own and return a list of result dicts.

    Keyword filters (all optional):
        rating: all / not rated / general / teen / mature / explicit
        category: any / gen / f/m / m/m / f/f / multi / other
        complete: any / complete / in-progress
        crossover: any / only / exclude
        sort: best match / date updated / kudos / hits / ... (see AO3_SORT)
        single_chapter: truthy → one-shots only
        language: label ("english", "french") or raw ISO code ("en", "fr")
        fandom: fandom name(s) (AO3 accepts loose matching)
        word_count: range expression e.g. "<5000", ">10000", "1000-5000"
        character, relationship, freeform, title, creator: AO3 free-text fields

    `page` (keyword-only) selects a specific results page.
    """
    url = _build_ao3_search_url(query, filters, page=page)
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"AO3 search failed (HTTP {resp.status_code}). "
            "The site may be temporarily unavailable."
        )
    return _parse_ao3_results(resp.text)


# ── Royal Road search ─────────────────────────────────────────────

RR_STATUS = {
    "any": None,
    "ongoing": "ONGOING",
    "hiatus": "HIATUS",
    "completed": "COMPLETED",
    "complete": "COMPLETED",
    "dropped": "DROPPED",
    "stub": "STUB",
}

RR_TYPE = {
    "any": None,
    "original": "ORIGINAL",
    "fanfiction": "FANFICTION",
}

RR_ORDER_BY = {
    "relevance": "relevance",
    "popularity": "popularity",
    "last update": "last_update",
    "pages": "pages",
    "rating": "rating",
    "title": "title",
}

# RR's curated discovery lists. When one of these is picked, the search
# switches from /fictions/search?title=... to /fictions/<slug>, which
# doesn't accept a free-text query but does accept tagsAdd.
RR_LISTS = {
    "search": None,
    "best rated": "best-rated",
    "trending": "trending",
    "active popular": "active-popular",
    "weekly popular": "weekly-popular",
    "monthly popular": "monthly-popular",
    "latest updates": "latest-updates",
    "new releases": "new-releases",
    "complete": "complete",
    "rising stars": "rising-stars",
}

# Royal Road genres and content tags. On RR these are the same field
# under the hood (tagsAdd=<slug>) — "genres" is just the first 15 RR
# assigns top-billing to. Split here for UX: users recognize "Fantasy"
# as a genre and "LitRPG" as a tag.
# Key = human-readable label; value = RR's slug for the tagsAdd param.
RR_GENRES = {
    "Action": "action",
    "Adventure": "adventure",
    "Comedy": "comedy",
    "Contemporary": "contemporary",
    "Drama": "drama",
    "Fantasy": "fantasy",
    "Historical": "historical",
    "Horror": "horror",
    "Mystery": "mystery",
    "Psychological": "psychological",
    "Romance": "romance",
    "Satire": "satire",
    "Sci-fi": "sci_fi",
    "Short Story": "one_shot",
    "Tragedy": "tragedy",
}

RR_TAGS = {
    "Anti-Hero Lead": "anti-hero_lead",
    "Artificial Intelligence": "artificial_intelligence",
    "Attractive Lead": "attractive_lead",
    "Cyberpunk": "cyberpunk",
    "Dungeon": "dungeon",
    "Dystopia": "dystopia",
    "Female Lead": "female_lead",
    "First Contact": "first_contact",
    "GameLit": "gamelit",
    "Gender Bender": "gender_bender",
    "Genetically Engineered": "genetically_engineered",
    "Grimdark": "grimdark",
    "Hard Sci-fi": "hard_sci-fi",
    "Harem": "harem",
    "Hero": "hero",
    "High Fantasy": "high_fantasy",
    "LitRPG": "litrpg",
    "Low Fantasy": "low_fantasy",
    "Magic": "magic",
    "Male Lead": "male_lead",
    "Martial Arts": "martial_arts",
    "Military": "military",
    "Multiple Lead Characters": "multiple_lead",
    "Mythos": "mythos",
    "Non-Human Lead": "non-human_lead",
    "Portal Fantasy / Isekai": "summoned_hero",
    "Post Apocalyptic": "post_apocalyptic",
    "Progression": "progression",
    "Reader Interactive": "reader_interactive",
    "Reincarnation": "reincarnation",
    "Ruling Class": "ruling_class",
    "School Life": "school_life",
    "Secret Identity": "secret_identity",
    "Slice of Life": "slice_of_life",
    "Soft Sci-fi": "soft_sci-fi",
    "Space Opera": "space_opera",
    "Sports": "sports",
    "Steampunk": "steampunk",
    "Strategy": "strategy",
    "Strong Lead": "strong_lead",
    "Super Heroes": "super_heroes",
    "Supernatural": "supernatural",
    "Technologically Engineered": "technologically_engineered",
    "Time Loop": "loop",
    "Time Travel": "time_travel",
    "Urban Fantasy": "urban_fantasy",
    "Villainous Lead": "villainous_lead",
    "Virtual Reality": "virtual_reality",
    "War and Military": "war_and_military",
    "Wuxia": "wuxia",
    "Xianxia": "xianxia",
}

# Content warnings — RR submits these as warningsAdd params (separate
# bucket from tagsAdd, though the UI looks identical).
RR_WARNINGS = {
    "Profanity": "profanity",
    "Sexual Content": "sexuality",
    "Gore": "gore",
    "Traumatising Content": "traumatising",
    "AI-Assisted Content": "ai_assisted",
    "AI-Generated Content": "ai_generated",
}


def _rr_slug_list(raw, lookup=None):
    """Split a comma/semicolon list into slugs, translating labels via
    `lookup` (label → slug) when possible. Unknown entries pass through
    verbatim so power users can hand-write RR's raw slugs.
    """
    if not raw:
        return []
    out = []
    for piece in re.split(r"[,;]", str(raw)):
        s = piece.strip()
        if not s:
            continue
        if lookup:
            # Case-insensitive label lookup
            matched = None
            for label, slug in lookup.items():
                if label.lower() == s.lower():
                    matched = slug
                    break
            out.append(matched if matched else s)
        else:
            out.append(s)
    return out


def _rr_positive_int(value, name):
    """Coerce a user-supplied min/max filter value to int, or None when
    blank. Rejects negatives and non-numeric input with ValueError.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if n < 0:
        raise ValueError(f"{name} must be >= 0, got {n}")
    return n


def _collect_rr_tag_slugs(filters):
    """Gather tagsAdd slugs from the three filter surfaces — genres,
    tags, and the legacy free-text `tags` field — in a single list.
    De-dupes so a user who selects "Fantasy" in genres and then also
    types "fantasy" in tags doesn't send it twice.
    """
    seen = []
    for source, lookup in (
        ("genres", RR_GENRES),
        ("tags_picked", RR_TAGS),
        ("tags", None),
    ):
        for slug in _rr_slug_list(filters.get(source), lookup):
            if slug and slug not in seen:
                seen.append(slug)
    return seen


def _build_rr_search_url(query, filters, page=1):
    list_key = (filters.get("list") or "").strip().lower()
    list_slug = RR_LISTS.get(list_key)
    tag_slugs = _collect_rr_tag_slugs(filters)
    warning_slugs = _rr_slug_list(filters.get("warnings"), RR_WARNINGS)

    if list_slug:
        # List endpoints ignore `title=`. Keep tagsAdd / warningsAdd
        # working so users can browse e.g. "Rising Stars tagged
        # progression with gore warnings filtered in".
        params = {}
        if page and page > 1:
            params["page"] = int(page)
        query_parts = (
            list(params.items())
            + [("tagsAdd", t) for t in tag_slugs]
            + [("warningsAdd", w) for w in warning_slugs]
        )
        suffix = "?" + urlencode(query_parts) if query_parts else ""
        return RR_BASE + f"/fictions/{list_slug}" + suffix

    params = {}
    if query:
        params["title"] = query
    if page and page > 1:
        params["page"] = int(page)
    status = (filters.get("status") or "").strip().lower()
    if status and status in RR_STATUS and RR_STATUS[status]:
        params["status"] = RR_STATUS[status]
    type_ = (filters.get("type") or "").strip().lower()
    if type_ and type_ in RR_TYPE and RR_TYPE[type_]:
        params["type"] = RR_TYPE[type_]
    order = (filters.get("order_by") or "").strip().lower()
    if order and order in RR_ORDER_BY and RR_ORDER_BY[order] != "relevance":
        params["orderBy"] = RR_ORDER_BY[order]

    for key, param in (
        ("min_words", "minWords"),
        ("max_words", "maxWords"),
        ("min_pages", "minPages"),
        ("max_pages", "maxPages"),
    ):
        n = _rr_positive_int(filters.get(key), key)
        if n is not None:
            params[param] = n

    # Rating is 0.0–5.0 on RR; pass through as-is when non-empty.
    rating_raw = filters.get("min_rating")
    if rating_raw is not None and str(rating_raw).strip() != "":
        try:
            rating = float(str(rating_raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"min_rating must be a number, got {rating_raw!r}")
        if rating < 0 or rating > 5:
            raise ValueError(f"min_rating must be between 0 and 5, got {rating}")
        params["minRating"] = rating

    if tag_slugs or warning_slugs:
        return (
            RR_BASE + "/fictions/search?" +
            urlencode(
                list(params.items())
                + [("tagsAdd", t) for t in tag_slugs]
                + [("warningsAdd", w) for w in warning_slugs]
            )
        )
    return RR_BASE + "/fictions/search?" + urlencode(params)


def _parse_rr_results(html):
    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.find_all("div", class_="fiction-list-item"):
        title_link = item.find("h2", class_="fiction-title")
        if title_link:
            title_link = title_link.find("a", href=re.compile(r"/fiction/\d+"))
        if not title_link:
            continue
        href = title_link["href"]
        url = RR_BASE + href if href.startswith("/") else href
        title = title_link.get_text(strip=True)

        # Author — not directly shown in search results, leave blank
        author = ""

        # Status labels (ONGOING/COMPLETED/etc.) and type (Original/Fanfiction).
        # STUB is orthogonal to completion state: the author stubbed the
        # fiction (usually because it's been published elsewhere), but the
        # work may still be ongoing or complete underneath. Track it as a
        # flag and combine with whatever completion label is present. When
        # the card only carries STUB with no completion label (common for
        # stubbed works), flag the result for later enrichment from the
        # fiction page — see search_royalroad().
        status = "In-Progress"
        stubbed = False
        completion_from_card = False
        rating = "?"
        labels = [
            lbl.get_text(strip=True).upper()
            for lbl in item.find_all("span", class_="label")
        ]
        for lbl in labels:
            if lbl == "COMPLETED":
                status = "Complete"
                completion_from_card = True
            elif lbl in ("HIATUS", "DROPPED", "INACTIVE"):
                status = lbl.title()
                completion_from_card = True
            elif lbl == "ONGOING":
                status = "In-Progress"
                completion_from_card = True
            elif lbl == "STUB":
                stubbed = True
        stubbed_unknown = stubbed and not completion_from_card
        if stubbed and completion_from_card:
            status = f"{status} (Stubbed)"
        elif stubbed:
            status = "Stubbed"

        # Genre tags
        tag_links = item.find_all("a", class_="fiction-tag")
        genre_or_fandom = ", ".join(a.get_text(strip=True) for a in tag_links[:5])

        # Stats — pages, chapters, followers. RR search cards DON'T
        # expose a raw word count anywhere — only "N Pages". Convert at
        # RR's house conversion of ~275 words per page so the "Words"
        # column actually resembles a word count instead of looking
        # like a tiny 4-digit story. A leading "~" signals the
        # estimate; the fiction page itself (fetched on download) has
        # the authoritative count.
        stats_text = item.get_text(" ", strip=True)
        pages_m = re.search(r"(\d[\d,]*)\s+Pages", stats_text)
        chapters_m = re.search(r"(\d[\d,]*)\s+Chapters", stats_text)
        if pages_m:
            pages = int(pages_m.group(1).replace(",", ""))
            words = f"~{pages * 275:,}"
        else:
            words = "?"
        chapters = chapters_m.group(1) if chapters_m else "?"

        # Description — in a hidden #description-<id> div; show first N chars
        desc_div = item.find("div", id=re.compile(r"^description-\d+"))
        summary = desc_div.get_text(" ", strip=True) if desc_div else ""
        if not summary:
            desc_wrap = item.find("div", class_="fiction-description")
            if desc_wrap:
                summary = desc_wrap.get_text(" ", strip=True)

        results.append({
            "title": title,
            "author": author,
            "url": url,
            "summary": summary,
            "words": words,
            "chapters": chapters,
            "rating": rating,
            "fandom": genre_or_fandom,
            "status": status,
            "_stubbed_unknown": stubbed_unknown,
        })

    return results


def _fetch_rr_fiction_status(session, fiction_url):
    """Pull the canonical completion label (Complete/In-Progress/Hiatus/…)
    from a Royal Road fiction page. Used to fill in the status for
    stubbed search results, whose cards don't carry completion info.
    Returns None if the page can't be parsed.
    """
    try:
        resp = session.get(fiction_url, timeout=30)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    for lbl in soup.find_all("span", class_="label"):
        text = lbl.get_text(strip=True).upper()
        if text == "COMPLETED":
            return "Complete"
        if text == "ONGOING":
            return "In-Progress"
        if text in ("HIATUS", "DROPPED", "INACTIVE"):
            return text.title()
    return None


def search_royalroad(query, *, page=1, **filters):
    """Search royalroad.com. Returns result dicts matching search_ffn shape.

    Keyword filters:
        status:   any / ongoing / hiatus / completed / dropped / stub
        type:     any / original / fanfiction
        order_by: relevance / popularity / last update / pages / rating / title
        genres:   comma-separated RR genre labels (see RR_GENRES, e.g.
                  "Fantasy,Sci-fi"). Accepts raw slugs too.
        tags_picked: comma-separated RR tag labels (see RR_TAGS, e.g.
                  "LitRPG,Progression"). Accepts raw slugs too.
        tags:     legacy free-text tag list (raw slugs, e.g. "progression,magic")
        warnings: comma-separated warning labels (see RR_WARNINGS) —
                  selects fictions that carry each listed warning.
        min_words / max_words: word-count bounds (integer).
        min_pages / max_pages: page-count bounds (integer).
        min_rating: minimum average rating 0.0-5.0.
        list:     search (default) / best rated / trending / active popular /
                  weekly popular / monthly popular / latest updates /
                  new releases / complete / rising stars

    `page` (keyword-only) selects a specific results page. When `list` is
    set to one of the non-search values, the free-text query is ignored
    and the corresponding RR discovery page is browsed instead; `tags`
    and `warnings` still filter, but `status`/`type`/`order_by` and the
    min/max numeric filters do not apply to list-browse endpoints.
    """
    url = _build_rr_search_url(query, filters, page=page)
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Royal Road search failed (HTTP {resp.status_code})."
        )
    results = _parse_rr_results(resp.text)
    # Enrich stubbed results whose cards don't carry a completion label.
    # The fiction page reliably does, so one cheap GET per such row
    # gives us "Complete (Stubbed)" instead of a bare "Stubbed".
    for r in results:
        if r.pop("_stubbed_unknown", False):
            real = _fetch_rr_fiction_status(session, r["url"])
            if real:
                r["status"] = f"{real} (Stubbed)"
    return results


# ── Literotica search ─────────────────────────────────────────────
# Literotica's keyword search is JS-rendered against an auth-gated API.
# The public tag-browse subdomain (tags.literotica.com/<tag>) is
# server-rendered with schema.org microdata on every story card, so we
# treat "search" as tag-browsing: the user's query becomes a tag slug
# (lowercased, spaces → hyphens). That covers most what-you'd-actually-
# type-into-a-search-box use cases on Literotica.


def _literotica_tag_slug(query: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 -]+", "", query).strip().lower()
    return re.sub(r"\s+", "-", s)


def _parse_literotica_results(html):
    soup = BeautifulSoup(html, "lxml")
    results = []
    for card in soup.find_all(
        "div", attrs={"property": "itemListElement"}
    ):
        url = card.get("resource") or ""
        title = ""
        h_tag = card.find(["h4", "h3"])
        if h_tag:
            title = h_tag.get_text(strip=True)
        if not title:
            # Fallback to <meta property=name>
            mn = card.find("meta", attrs={"property": "name"})
            if mn:
                title = mn.get("content", "")

        author = ""
        author_link = card.find(
            "a", attrs={"property": re.compile(r"author")}
        )
        if author_link:
            mn = author_link.find("meta", attrs={"property": "name"})
            if mn:
                author = mn.get("content", "")
            else:
                author = author_link.get_text(" ", strip=True).replace("by ", "").strip()

        summary = ""
        headline = card.find("p", attrs={"property": "headline"})
        if headline:
            summary = headline.get_text(" ", strip=True)

        # Category (Novels / Loving Wives / etc.) stands in for "fandom"
        fandom = ""
        cat_link = card.find("a", href=re.compile(r"literotica\.com/c/"))
        if cat_link:
            fandom = cat_link.get_text(" ", strip=True)
            # Strip trailing date
            fandom = re.sub(r"\d{2}/\d{2}/\d{4}\s*$", "", fandom).strip()

        rating = "?"
        rating_span = card.find("span", attrs={"property": "ratingValue"})
        if rating_span:
            rating = rating_span.get_text(strip=True)

        results.append({
            "title": title,
            "author": author,
            "url": url,
            "summary": summary,
            "words": "?",
            "chapters": "?",
            "rating": rating,
            "fandom": fandom,
            "status": "",
        })
    return results


def fetch_until_limit(search_fn, query, *, limit, start_page=1, **kwargs):
    """Call `search_fn` across successive pages until `limit` results are
    collected or a page comes back empty. Returns (results, next_page).

    `results` is the full list of dicts from all fetched pages (may run a
    little past `limit` if the last page's natural size overshoots). The
    caller can trim it further if they want a hard cap. `next_page` is
    the page number a subsequent "load more" should request.
    """
    collected = []
    page = max(1, int(start_page))
    while len(collected) < limit:
        page_results = search_fn(query, page=page, **kwargs)
        if not page_results:
            break
        collected.extend(page_results)
        page += 1
    return collected, page


def collapse_ao3_series(results):
    """Fold multiple AO3 works that share a series into a single series
    row, but only when 2+ parts of the same series appear in the results.
    Solo matches stay as regular work rows — promoting them to a "series"
    label hides the work's real title behind the series title.

    Works that belong to more than one series still appear as work rows;
    collapsing them would hide the multi-membership.
    """
    series_counts = {}
    for r in results:
        series = r.get("series") or []
        if len(series) != 1:
            continue
        sid = series[0]["id"]
        series_counts[sid] = series_counts.get(sid, 0) + 1

    collapsed = []
    seen_series = {}
    for r in results:
        series = r.get("series") or []
        if len(series) != 1 or series_counts.get(series[0]["id"], 0) < 2:
            collapsed.append(r)
            continue
        s = series[0]
        if s["id"] in seen_series:
            seen_series[s["id"]]["series_parts"].append(r)
            continue
        row = {
            "title": s["name"],
            "author": r.get("author", ""),
            "url": s["url"],
            "summary": r.get("summary", ""),
            "words": "?",
            "chapters": str(series_counts[s["id"]]),
            "rating": r.get("rating", "?"),
            "fandom": r.get("fandom", ""),
            "status": "Series",
            "is_series": True,
            "series_id": s["id"],
            "series_parts": [r],
        }
        seen_series[s["id"]] = row
        collapsed.append(row)
    return collapsed


# Literotica titles/URLs suffix chapters and parts a few different ways:
#   /s/foo-ch-07          "Foo Ch. 07"
#   /s/foo-pt-02          "Foo Pt. 02"
#   /s/foo-6              "Foo - 6"     (bare-number)
#   /s/foo-p4             "Foo P4"      (compact "P<N>")
# Authors sometimes append a variant tag ("Ch. 07 - Alt Ending") which we
# strip so variants group with their canonical siblings.
_LIT_CHAPTER_URL_RE = re.compile(
    r"/s/(.+?)-(?:ch|chapter|pt|part)-(\d+)(?:-[^/]*)?/?$",
    re.IGNORECASE,
)
_LIT_COMPACT_P_URL_RE = re.compile(
    r"/s/(.+?)-p(\d+)/?$",
    re.IGNORECASE,
)
_LIT_BARE_NUM_URL_RE = re.compile(
    r"/s/(.+?)-(\d+)/?$",
)
_LIT_BARE_SLUG_URL_RE = re.compile(r"/s/([^/?#]+?)/?$", re.IGNORECASE)
_LIT_CHAPTER_TITLE_RE = re.compile(
    r"^(.*?)(?:[\s:]+(?:Ch|Chapter|Pt|Part)\.?\s*\d+"
    r"|\s*[-\u2013]\s*\d+"
    r"|\s+P\d+)(?:\s*[-:].*)?$",
    re.IGNORECASE,
)


def _literotica_series_key(result):
    """Return (base_slug, part_number, base_title) if the result looks like
    a numbered chapter of a larger Literotica work, else None.

    URL patterns with an explicit `ch`/`pt`/`part`/`p<N>` marker are
    trusted on the URL alone. The bare `-N` URL suffix is ambiguous
    (it also matches year-tagged annual stories like
    `/s/new-years-eve-2024`) so it's only accepted when the *title*
    also carries a numeric chapter marker.
    """
    url = result.get("url") or ""
    title = result.get("title") or ""
    title_has_marker = bool(_LIT_CHAPTER_TITLE_RE.match(title))
    strict_patterns = (
        (_LIT_CHAPTER_URL_RE, True),
        (_LIT_COMPACT_P_URL_RE, True),
        (_LIT_BARE_NUM_URL_RE, title_has_marker),
    )
    for pattern, accept in strict_patterns:
        m = pattern.search(url)
        if not m:
            continue
        if not accept:
            return None
        base_slug = m.group(1).lower()
        try:
            part = int(m.group(2))
        except ValueError:
            continue
        tm = _LIT_CHAPTER_TITLE_RE.match(title)
        base_title = tm.group(1).strip() if tm else title
        return base_slug, part, base_title
    return None


def _literotica_bare_slug(result):
    """Return the /s/<slug> slug portion of a Literotica URL, lowercased."""
    url = result.get("url") or ""
    m = _LIT_BARE_SLUG_URL_RE.search(url)
    return m.group(1).lower() if m else None


def collapse_literotica_series(results):
    """Group Literotica results that are numbered chapters of the same
    work (same URL slug stem, same author) into one series row. Only
    collapses when 2+ parts are present — otherwise a lone "Ch. 06"
    would be hidden behind a series label with no siblings to show.

    Literotica's own convention is that Part 1 of a series is posted
    *without* any suffix (just the bare title), and subsequent parts
    get "Pt. 02" / "Ch. 02" / "- 2" appended. So if we see a bare-
    titled work whose URL slug is the base stem of some other suffixed
    work by the same author, we treat it as part 1 of that series.
    """
    groups = {}  # (author, base_slug) → [(index, result, part, base_title)]
    seen_indices = set()
    for i, r in enumerate(results):
        key = _literotica_series_key(r)
        if key is None:
            continue
        base_slug, part, base_title = key
        group_key = (r.get("author") or "", base_slug)
        groups.setdefault(group_key, []).append((i, r, part, base_title))
        seen_indices.add(i)

    # Second pass: adopt bare-titled results as part 1 when their slug
    # matches an existing group's base_slug and the author lines up —
    # but only if that group doesn't *already* have an explicit Part 1
    # member. That guard prevents a standalone work that happens to
    # share a slug prefix with a later, unrelated serial from being
    # folded into it.
    for i, r in enumerate(results):
        if i in seen_indices:
            continue
        bare = _literotica_bare_slug(r)
        if not bare:
            continue
        group_key = (r.get("author") or "", bare)
        if group_key not in groups:
            continue
        existing_parts = {m[2] for m in groups[group_key]}
        if 1 in existing_parts:
            continue
        title = r.get("title") or bare
        groups[group_key].append((i, r, 1, title))

    to_collapse = {}  # anchor index → series row
    hide = set()
    for group_key, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: m[2])
        anchor_i, anchor_r, _, base_title = members[0]
        parts = [m[1] for m in members]
        for i, *_ in members:
            hide.add(i)
        to_collapse[anchor_i] = {
            "title": base_title,
            "author": anchor_r.get("author", ""),
            "url": anchor_r.get("url", ""),
            "summary": anchor_r.get("summary", ""),
            "words": "?",
            "chapters": str(len(parts)),
            "rating": anchor_r.get("rating", "?"),
            "fandom": anchor_r.get("fandom", ""),
            "status": "Series",
            "is_series": True,
            "series_id": f"lit:{group_key[1]}",
            "series_parts": parts,
            # Signal to the download dispatch: iterate series_parts URLs
            # directly rather than trying to resolve a /series/se/<id>.
            "parts_only": True,
        }

    collapsed = []
    for i, r in enumerate(results):
        if i in to_collapse:
            collapsed.append(to_collapse[i])
        elif i in hide:
            continue
        else:
            collapsed.append(r)
    return collapsed


LIT_CATEGORIES = {
    "any": None,
    # Literotica's top-level category slugs also exist as tag slugs on
    # tags.literotica.com, so we reuse the tag-browse URL rather than
    # fetching /c/<slug> (which is paginated differently and doesn't
    # expose schema.org microdata per card).
    "Anal": "anal",
    "BDSM": "bdsm",
    "Celebrities & Fan Fiction": "celeb",
    "Chain Stories": "chain-story",
    "Erotic Couplings": "erotic-couplings",
    "Erotic Horror": "erotic-horror",
    "Exhibitionist & Voyeur": "exhibitionist",
    "Fetish": "fetish",
    "First Time": "first-time",
    "Gay Male": "gay-male",
    "Group Sex": "group-sex",
    "How To": "how-to",
    "Humor & Satire": "humor",
    "Illustrated": "illustrated",
    "Incest/Taboo": "incest",
    "Interracial Love": "interracial",
    "Lesbian Sex": "lesbian",
    "Letters & Transcripts": "letters",
    "Loving Wives": "loving-wives",
    "Mature": "mature",
    "Mind Control": "mind-control",
    "Non-consent/Reluctance": "non-consent",
    "Non-English": "non-english",
    "Novels and Novellas": "novel",
    "Reviews & Essays": "reviews-essays",
    "Romance": "romance",
    "Sci-Fi & Fantasy": "sci-fi-fantasy",
    "Toys & Masturbation": "toys",
    "Transgender & Crossdressers": "transgender",
}


def search_literotica(query, *, page=1, **filters):
    """Search Literotica by tag or category. `query` is converted to a
    tag slug (lowercased, whitespace → hyphens) and looked up on
    tags.literotica.com — the server-rendered alternative to
    Literotica's JS-only keyword search.

    Keyword filters (optional):
        category: picks one of Literotica's top-level categories from
                  LIT_CATEGORIES. When set, the category slug is used
                  instead of the query — browsing e.g. "Loving Wives"
                  standalone instead of searching for a user-typed tag.

    Unknown tags return no results; the response is still a 200 page,
    just without story cards.

    `page` (keyword-only) selects a specific results page.
    """
    cat_raw = filters.get("category") if filters else None
    slug = None
    if cat_raw:
        cat_str = str(cat_raw).strip()
        for label, cat_slug in LIT_CATEGORIES.items():
            if cat_slug and label.lower() == cat_str.lower():
                slug = cat_slug
                break
        if slug is None and cat_str.lower() not in ("", "any"):
            # Pass-through for users who type a raw slug.
            slug = _literotica_tag_slug(cat_str) or None
    if slug is None:
        slug = _literotica_tag_slug(query)
    if not slug:
        return []
    url = f"{LIT_TAGS_BASE}/{slug}"
    try:
        page_n = int(str(page).strip()) if page else 1
    except (TypeError, ValueError):
        page_n = 1
    if page_n > 1:
        url += f"?page={page_n}"
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Literotica search failed (HTTP {resp.status_code})."
        )
    return _parse_literotica_results(resp.text)


# ── Wattpad ──────────────────────────────────────────────────────

WP_API = "https://api.wattpad.com"
WP_PAGE_SIZE = 20


WP_MATURE = {
    "any": None,
    "exclude": "exclude",
    "only": "only",
}


WP_COMPLETED = {
    "any": None,
    "complete": True,
    "in-progress": False,
}


def search_wattpad(query, *, page=1, **filters):
    """Search Wattpad via the public v4 stories API.

    Wattpad's web search is JS-rendered and rate-limits aggressively, so
    we hit ``api.wattpad.com/v4/stories`` instead — the same endpoint
    the mobile app uses, accepting an unauthenticated GET.

    Keyword filters (optional):
        mature:    any / exclude / only — client-side filter on the
                   mature flag; Wattpad's API doesn't expose a filter
                   param so we filter the returned page.
        completed: any / complete / in-progress — same, client-side.

    The API uses offset-based paging; page=1 → offset=0, page=2 → offset=20.
    Returns a list of result dicts compatible with other search_* sites.
    """
    import json as _json
    from curl_cffi import requests as _curl_requests

    q = (query or "").strip()
    if not q:
        # Empty query would return a generic random set; be explicit.
        return []

    try:
        page_n = max(1, int(page))
    except (TypeError, ValueError):
        page_n = 1
    offset = (page_n - 1) * WP_PAGE_SIZE

    fields = (
        "stories("
        "id,title,user,description,cover,completed,mature,numParts,"
        "url,tags,language,paidModel,isPaywalled,length"
        ")"
    )
    qs = {
        "query": q,
        "limit": WP_PAGE_SIZE,
        "offset": offset,
        "fields": fields,
    }
    url = f"{WP_API}/v4/stories?" + urlencode(qs)
    session = _curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Wattpad search failed (HTTP {resp.status_code})."
        )
    try:
        data = _json.loads(resp.text)
    except ValueError as exc:
        raise RuntimeError("Wattpad returned non-JSON.") from exc
    stories = data.get("stories") or []

    mature = (filters or {}).get("mature")
    completed = (filters or {}).get("completed")
    # Allow natural-case labels ("Complete", "In-Progress") and the
    # boolean-ish keys from the CLI.
    def _norm(s):
        return str(s).strip().lower() if s is not None else None
    mature = _norm(mature)
    completed = _norm(completed)

    results = []
    for s in stories:
        is_mature = bool(s.get("mature"))
        is_complete = bool(s.get("completed"))
        if mature == "exclude" and is_mature:
            continue
        if mature == "only" and not is_mature:
            continue
        if completed == "complete" and not is_complete:
            continue
        if completed == "in-progress" and is_complete:
            continue

        user = s.get("user") or {}
        author = user.get("name") or user.get("fullname") or ""
        length = s.get("length") or 0
        # Character count → rough word count (5 chars/word). Wattpad
        # doesn't expose a word field, and we want search cards to
        # surface something more useful than "length".
        words_est = f"{max(1, int(length) // 5):,}" if length else ""
        tags = s.get("tags") or []
        fandom_str = ", ".join(tags[:3]) if tags else ""
        paid = s.get("paidModel") or ""
        rating_bits = []
        if is_mature:
            rating_bits.append("Mature")
        if paid:
            rating_bits.append("Paid")
        rating = " / ".join(rating_bits) or "GA"
        results.append({
            "title": s.get("title") or "Untitled",
            "author": author,
            "url": s.get("url") or f"https://www.wattpad.com/story/{s.get('id')}",
            "summary": (s.get("description") or "").strip(),
            "words": words_est,
            "chapters": str(s.get("numParts") or ""),
            "rating": rating,
            "fandom": fandom_str,
            "status": "Complete" if is_complete else "In-Progress",
        })
    return results
