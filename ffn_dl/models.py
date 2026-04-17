"""Data models for stories and chapters."""

import re
from dataclasses import dataclass, field


@dataclass
class Chapter:
    number: int
    title: str
    html: str


@dataclass
class Story:
    id: int
    title: str
    author: str
    summary: str
    url: str
    author_url: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def parse_chapter_spec(spec):
    """Parse a chapter-range expression into a list of (lo, hi) tuples.

    Accepts comma-separated tokens: "5" (single), "1-5" (closed range),
    "-5" (1..5), "20-" (20..end), "1,3,5-10". hi=None means "through
    the last chapter". Whitespace is tolerated. Returns None when the
    spec is empty or None (meaning "no filter — take everything").
    Raises ValueError on malformed input.
    """
    if spec is None:
        return None
    text = str(spec).strip()
    if not text:
        return None

    ranges = []
    for token in text.split(","):
        t = token.strip()
        if not t:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", t)
        if m:
            lo_s, hi_s = m.group(1), m.group(2)
            lo = int(lo_s) if lo_s else 1
            hi = int(hi_s) if hi_s else None
            if lo < 1:
                raise ValueError(f"Chapter range {t!r}: lower bound must be >= 1")
            if hi is not None and hi < lo:
                raise ValueError(f"Chapter range {t!r}: upper < lower")
            ranges.append((lo, hi))
            continue
        if t.isdigit():
            n = int(t)
            if n < 1:
                raise ValueError(f"Chapter {t!r}: must be >= 1")
            ranges.append((n, n))
            continue
        raise ValueError(f"Bad chapter token: {t!r}")

    return ranges or None


def chapter_in_spec(n, ranges):
    """True if chapter number `n` falls inside the parsed spec."""
    if ranges is None:
        return True
    for lo, hi in ranges:
        if n >= lo and (hi is None or n <= hi):
            return True
    return False
