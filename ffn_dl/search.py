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
SEARCH_PATH = "/search/"
MAX_RESULTS = 25


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


def _build_search_url(query, filters):
    params = {"keywords": query, "ready": 1, "type": "story"}

    mapping = [
        ("censorid", "rating", FFN_RATING),
        ("languageid", "language", FFN_LANGUAGE),
        ("statusid", "status", FFN_STATUS),
        ("genreid", "genre", FFN_GENRE),
        ("words", "min_words", FFN_WORDS),
        ("formatid", "crossover", FFN_CROSSOVER),
        ("match", "match", FFN_MATCH),
    ]
    for param, key, table in mapping:
        value = filters.get(key)
        resolved = _resolve_filter(value, table, key)
        if resolved is not None:
            params[param] = resolved

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

    for div in result_divs[:MAX_RESULTS]:
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


def search_ffn(query, **filters):
    """Search fanfiction.net and return a list of result dicts.

    Keyword filters (all optional — pass a label from the corresponding
    FFN_* table, or the raw numeric ID):
        rating: all / K / K+ / T / M / K-T
        language: english / spanish / french / ... (see FFN_LANGUAGE)
        status: all / in-progress / complete
        genre: romance / humor / adventure / angst / ... (see FFN_GENRE)
        min_words: <1k / <5k / 5k+ / 30k+ / 50k+ / 150k+ / 300k+
        crossover: any / only / exclude
        match: any / title / summary  (where the keywords must appear)

    Each result dict has keys: title, author, url, summary, words,
    chapters, rating, fandom, status.
    """
    url = _build_search_url(query, filters)
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


def _build_ao3_search_url(query, filters):
    params = {}
    if query:
        params["work_search[query]"] = query

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

    sort = resolve(filters.get("sort"), AO3_SORT, "sort")
    if sort is not None:
        params["work_search[sort_column]"] = sort

    if filters.get("single_chapter"):
        params["work_search[single_chapter]"] = 1

    # Free-text AO3 fields pass straight through
    for key, param in [
        ("language", "work_search[language_id]"),
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

    for li in works_ol.find_all("li", recursive=False)[:MAX_RESULTS]:
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
            }
        )

    return results


def search_ao3(query, **filters):
    """Search Archive of Our Own and return a list of result dicts.

    Keyword filters (all optional):
        rating: all / not rated / general / teen / mature / explicit
        complete: any / complete / in-progress
        crossover: any / only / exclude
        sort: best match / date updated / kudos / hits / ... (see AO3_SORT)
        single_chapter: truthy → one-shots only
        language: ISO code (e.g. "en", "fr")
        fandom: fandom name(s) (AO3 accepts loose matching)
        word_count: range expression e.g. "<5000", ">10000", "1000-5000"
        character, relationship, freeform, title, creator: AO3 free-text fields
    """
    url = _build_ao3_search_url(query, filters)
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


def _build_rr_search_url(query, filters):
    params = {}
    if query:
        params["title"] = query
    status = (filters.get("status") or "").strip().lower()
    if status and status in RR_STATUS and RR_STATUS[status]:
        params["status"] = RR_STATUS[status]
    type_ = (filters.get("type") or "").strip().lower()
    if type_ and type_ in RR_TYPE and RR_TYPE[type_]:
        params["type"] = RR_TYPE[type_]
    order = (filters.get("order_by") or "").strip().lower()
    if order and order in RR_ORDER_BY and RR_ORDER_BY[order] != "relevance":
        params["orderBy"] = RR_ORDER_BY[order]
    if filters.get("tags"):
        # Comma-separated: "magic,dungeons" → tagsAdd=magic&tagsAdd=dungeons
        tag_list = [t.strip() for t in str(filters["tags"]).split(",") if t.strip()]
        # urlencode supports duplicate keys via doseq
        return (
            RR_BASE + "/fictions/search?" +
            urlencode(
                list(params.items())
                + [("tagsAdd", t) for t in tag_list]
            )
        )
    return RR_BASE + "/fictions/search?" + urlencode(params)


def _parse_rr_results(html):
    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.find_all("div", class_="fiction-list-item")[:MAX_RESULTS]:
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

        # Status labels (ONGOING/COMPLETED/etc.) and type (Original/Fanfiction)
        status = "In-Progress"
        rating = "?"
        labels = [
            lbl.get_text(strip=True).upper()
            for lbl in item.find_all("span", class_="label")
        ]
        for lbl in labels:
            if lbl == "COMPLETED":
                status = "Complete"
            elif lbl in ("HIATUS", "STUB", "DROPPED", "INACTIVE"):
                status = lbl.title()

        # Genre tags
        tag_links = item.find_all("a", class_="fiction-tag")
        genre_or_fandom = ", ".join(a.get_text(strip=True) for a in tag_links[:5])

        # Stats — pages, chapters, followers
        stats_text = item.get_text(" ", strip=True)
        pages_m = re.search(r"(\d[\d,]*)\s+Pages", stats_text)
        chapters_m = re.search(r"(\d[\d,]*)\s+Chapters", stats_text)
        words = f"{pages_m.group(1)}p" if pages_m else "?"
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
        })

    return results


def search_royalroad(query, **filters):
    """Search royalroad.com. Returns result dicts matching search_ffn shape.

    Keyword filters:
        status:   any / ongoing / hiatus / completed / dropped / stub
        type:     any / original / fanfiction
        order_by: relevance / popularity / last update / pages / rating / title
        tags:     comma-separated tag list (e.g. "progression,magic")
    """
    url = _build_rr_search_url(query, filters)
    session = curl_requests.Session(impersonate="chrome")
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Royal Road search failed (HTTP {resp.status_code})."
        )
    return _parse_rr_results(resp.text)
