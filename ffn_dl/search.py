"""Search fanfiction.net from the CLI."""

import logging
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup, NavigableString
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

FFN_BASE = "https://www.fanfiction.net"
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
        min_words: <1k / <5k / 5k+ / 10k+ / 30k+ / 50k+ / 150k+ / 300k+
        crossover: any / only / exclude
        match: any / title / summary  (where the keywords must appear)

    Each result dict has keys: title, author, url, summary, words,
    chapters, rating, fandom, status.
    """
    url = _build_search_url(query, filters)
    html = _fetch_search_page(url)
    return _parse_results(html)
