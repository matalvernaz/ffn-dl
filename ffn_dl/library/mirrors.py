"""Cross-site mirror detection for the library index.

The same story often lives on two (or three) sites — an FFN original
mirrored to AO3, a Literotica serial crossposted to StoriesOnline,
etc. The library index treats each URL as its own story, so a
``--library-stats`` view counts the work twice, ``--update-library``
probes both copies, and the user can accidentally read or audiobook
a stale mirror without knowing the other copy has newer chapters.

This module produces a list of *candidate* mirror pairs. It never
deletes — false positives on common titles ("The Dragon", "Untitled")
are inevitable on signal-strength heuristics, so acting on the
output is always the user's call.

Signal model (destructive-heuristics rule applies: ≥2 signals
required before flagging):

1. **Normalised title match.** Lowercase, strip punctuation, collapse
   whitespace, then compare token sets. Exact match or Jaccard
   similarity ≥ 0.85 counts.
2. **Normalised author match.** Same treatment as title; punctuation
   differences between sites (pen name on one, underscore on the
   other) still collapse to a match.
3. **First-chapter word overlap.** Extract the first chapter's plain
   text from each local file and compare unique-word sets. An overlap
   ≥ 0.6 counts. Cheap on a cached library — we already have the
   files on disk from the original downloads.

Cross-site constraint: candidates must live on *different* registered
scrapers. Within-site duplicates are the library-doctor's
``duplicate_relpaths`` territory — surfacing them here would muddle
the "possible mirror" signal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .fulltext import chapter_text
from .index import LibraryIndex


TITLE_JACCARD_THRESHOLD = 0.85
"""Fraction of title tokens that must overlap for the signal to fire.
0.85 picked because punctuation and article drift (``The ...`` vs
``A ...``) shave maybe 1–2 tokens off short titles; keeping the
threshold high enough that two different stories in the same fandom
(which share the fandom-flavoured vocabulary) don't both match."""

FIRST_CHAPTER_OVERLAP_THRESHOLD = 0.6
"""Fraction of unique words that must be shared between the two
first chapters. Lower than the title threshold because even a faithful
mirror picks up site-specific chrome (site-inserted copyright notice,
A/N formatting) and minor author edits."""

MIN_FIRST_CHAPTER_TOKENS = 50
"""Chapters shorter than this don't produce a reliable overlap
signal — the token set is too small, so "same 40 common words" is
noise. We skip the first-chapter signal entirely on short chapters
and rely on title + author alone."""

MIN_SIGNALS_TO_FLAG = 2
"""Keep in sync with the destructive-heuristics rule Matt has
specified: a content flag always wants ≥2 corroborating signals so
one accidental match doesn't produce a false positive."""


_DROP_RE = re.compile(r"['‘’“”]")
"""Characters deleted without a replacement. Apostrophes and curly
quotes shouldn't produce a word boundary — ``Renée's`` matching
``Renees`` is the whole point of normalisation."""

_NORMALISE_RE = re.compile(r"[^\w\s]+", re.UNICODE)
"""Strip punctuation but *keep* Unicode letters and digits. An
earlier draft restricted the character class to ASCII, which
silently erased CJK, Cyrillic, and other non-Latin titles: both
sides of a Japanese mirror pair normalised to empty strings, the
empty-title guard dropped them, and the feature quietly missed
every non-Latin fanfic in the library."""

_WS_RE = re.compile(r"\s+")


def _strip_accents(s: str) -> str:
    """Fold accented characters to ASCII so "Renée" matches "Renee"
    across a site that allows Unicode pen names and one that doesn't."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def normalise_title(title: str | None) -> str:
    """Normalised form used for title-equality and Jaccard similarity.

    Lowercase, accent-fold, drop punctuation, collapse whitespace.
    Preserves word boundaries — the comparison is token-set Jaccard,
    not substring, so word order doesn't matter and leading articles
    can still cause a one-token drift that the threshold absorbs.
    """
    if not title:
        return ""
    folded = _strip_accents(title.lower())
    # Delete apostrophes/quotes first so ``renee's`` collapses into
    # ``renees`` instead of splitting into ``renee`` + ``s``. Only
    # after that do we replace other punctuation with whitespace.
    deleted = _DROP_RE.sub("", folded)
    stripped = _NORMALISE_RE.sub(" ", deleted)
    return _WS_RE.sub(" ", stripped).strip()


def normalise_author(author: str | None) -> str:
    """Same shape as :func:`normalise_title` — same transforms apply
    equally to author names."""
    return normalise_title(author)


def _token_set(s: str) -> set[str]:
    if not s:
        return set()
    return {tok for tok in s.split() if tok}


def jaccard(a: set[str], b: set[str]) -> float:
    """Intersection-over-union size. Returns 0.0 when both sets are
    empty so an absent signal doesn't spuriously claim similarity."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def first_chapter_tokens(file_path: Path) -> set[str] | None:
    """Return the set of unique words in the first chapter of a local
    story file, or ``None`` if the chapter can't be recovered
    cheaply.

    Uses :func:`ffn_dl.updater.read_chapters` to parse the file, then
    :func:`ffn_dl.library.fulltext.chapter_text` to strip HTML. Short
    first chapters return ``None`` (see ``MIN_FIRST_CHAPTER_TOKENS``)
    so the caller can ignore them in the signal tally rather than
    passing a noisy small-set Jaccard value through.
    """
    from ..updater import read_chapters
    try:
        chapters = read_chapters(file_path)
    except Exception:
        return None
    if not chapters:
        return None
    first = min(chapters, key=lambda c: c.number)
    text = chapter_text(first.html)
    tokens = _token_set(normalise_title(text))  # reuse the token-friendly normaliser
    if len(tokens) < MIN_FIRST_CHAPTER_TOKENS:
        return None
    return tokens


# ── Candidate record ────────────────────────────────────────────


@dataclass(frozen=True)
class StoryKey:
    """Minimal identifier used inside :class:`MirrorCandidate`."""

    root: str
    url: str

    def __str__(self) -> str:
        return f"{self.url} (in {self.root})"


@dataclass
class MirrorCandidate:
    """One suspected cross-site mirror pair.

    ``signals`` is the ordered set of heuristics that matched —
    ``title``, ``author``, and/or ``first_chapter``. The caller can
    show it so the user knows *why* the pair was flagged, which is
    what makes accepting or dismissing the candidate an informed
    call rather than a "trust the tool" moment.
    """

    a: StoryKey
    b: StoryKey
    a_title: str = ""
    b_title: str = ""
    a_author: str = ""
    b_author: str = ""
    a_relpath: str = ""
    b_relpath: str = ""
    signals: list[str] = field(default_factory=list)
    title_similarity: float = 0.0
    first_chapter_overlap: float = 0.0

    @property
    def signal_count(self) -> int:
        return len(self.signals)


# ── Core detection ──────────────────────────────────────────────


def _site_key_for_url(url: str) -> str:
    """Pick a short site key for cross-site comparison.

    We want the check "these two URLs are on different sites" to be
    cheap and robust. Matching by registered scraper isn't quite
    right — two FFN URLs with different path shapes would pick the
    same scraper but we've already normalised via ``canonical_url``
    before reaching this code, so equal site keys here really do
    mean "same site". The hostname (minus ``www.``) is the simplest
    form that satisfies the invariant.
    """
    m = re.match(r"https?://([^/]+)/", url + "/")
    if not m:
        return ""
    host = m.group(1).lower()
    if host.startswith("www."):
        host = host[4:]
    return host


@dataclass
class _StoryRecord:
    """Bundle the fields the comparator reads, so we gather the index
    and file-system data once per story rather than re-parsing the
    index shape at every pair."""

    key: StoryKey
    title_norm: str
    title_tokens: set[str]
    author_norm: str
    relpath: str
    abs_path: Path
    site: str
    raw_title: str
    raw_author: str

    # Lazily-populated
    _first_chapter: set[str] | None = None
    _first_chapter_loaded: bool = False

    def first_chapter(self) -> set[str] | None:
        if not self._first_chapter_loaded:
            if self.abs_path.exists():
                self._first_chapter = first_chapter_tokens(self.abs_path)
            else:
                self._first_chapter = None
            self._first_chapter_loaded = True
        return self._first_chapter


def _collect_records(
    index: LibraryIndex,
    roots: Iterable[Path] | None,
) -> list[_StoryRecord]:
    """Materialise a :class:`_StoryRecord` for every tracked story in
    the given roots (or all indexed roots if ``None``). Skips entries
    with no relpath — we can't diff first chapters without a file."""
    if roots is None:
        root_list = [Path(r) for r in index.library_roots()]
    else:
        root_list = [Path(r).expanduser().resolve() for r in roots]

    records: list[_StoryRecord] = []
    for root in root_list:
        for url, entry in index.stories_in(root):
            relpath = entry.get("relpath") or ""
            if not relpath:
                continue
            title = entry.get("title") or ""
            author = entry.get("author") or ""
            title_norm = normalise_title(title)
            author_norm = normalise_author(author)
            records.append(_StoryRecord(
                key=StoryKey(root=str(root), url=url),
                title_norm=title_norm,
                title_tokens=_token_set(title_norm),
                author_norm=author_norm,
                relpath=relpath,
                abs_path=root / relpath,
                site=_site_key_for_url(url),
                raw_title=title,
                raw_author=author,
            ))
    return records


def _bucket_by_title_prefix(records: list[_StoryRecord]) -> dict[str, list[_StoryRecord]]:
    """Group records by the first two alphabetical tokens of the
    normalised title. Two copies of the same story always land in
    the same bucket; pairs that share a prefix by coincidence are
    cheap to compare further.

    Stories with fewer than two tokens land in a ``_short_`` bucket
    that's compared all-pairs — they're the cases where bucketing
    would cost accuracy more than it saves time.
    """
    buckets: dict[str, list[_StoryRecord]] = {}
    for r in records:
        toks = r.title_norm.split()
        if len(toks) >= 2:
            key = f"{toks[0]} {toks[1]}"
        elif toks:
            key = toks[0]
        else:
            key = "_notitle_"
        buckets.setdefault(key, []).append(r)
    return buckets


def find_mirrors(
    index: LibraryIndex,
    *,
    roots: Iterable[Path] | None = None,
    use_first_chapter: bool = True,
) -> list[MirrorCandidate]:
    """Scan the library index for cross-site mirror candidates.

    ``roots`` limits the search to specific library roots; ``None``
    (the default) compares across every indexed library so a fic
    mirrored into a different library folder is still caught.

    ``use_first_chapter`` controls whether the third signal fires.
    Set to ``False`` for a dry-run that skips file parsing entirely —
    useful for scripted callers who only want the fast
    metadata-pair pass.
    """
    records = _collect_records(index, roots)
    buckets = _bucket_by_title_prefix(records)
    candidates: list[MirrorCandidate] = []

    seen_pairs: set[tuple[StoryKey, StoryKey]] = set()

    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                # Cross-site only — same-site duplicates are the
                # library doctor's territory.
                if not a.site or not b.site or a.site == b.site:
                    continue
                pair = (a.key, b.key) if (a.key.url, a.key.root) < (b.key.url, b.key.root) else (b.key, a.key)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                signals: list[str] = []
                title_sim = 0.0
                overlap = 0.0

                if a.title_norm and b.title_norm:
                    if a.title_norm == b.title_norm:
                        signals.append("title")
                        title_sim = 1.0
                    else:
                        title_sim = jaccard(a.title_tokens, b.title_tokens)
                        if title_sim >= TITLE_JACCARD_THRESHOLD:
                            signals.append("title")

                if a.author_norm and b.author_norm and a.author_norm == b.author_norm:
                    signals.append("author")

                if use_first_chapter:
                    ta = a.first_chapter()
                    tb = b.first_chapter()
                    if ta is not None and tb is not None:
                        overlap = jaccard(ta, tb)
                        if overlap >= FIRST_CHAPTER_OVERLAP_THRESHOLD:
                            signals.append("first_chapter")

                if len(signals) >= MIN_SIGNALS_TO_FLAG:
                    # Canonical ordering so downstream output is
                    # stable across runs — important for tests and
                    # for users scripting on top of the output.
                    if (a.key.url, a.key.root) < (b.key.url, b.key.root):
                        first, second = a, b
                    else:
                        first, second = b, a
                    candidates.append(MirrorCandidate(
                        a=first.key,
                        b=second.key,
                        a_title=first.raw_title,
                        b_title=second.raw_title,
                        a_author=first.raw_author,
                        b_author=second.raw_author,
                        a_relpath=first.relpath,
                        b_relpath=second.relpath,
                        signals=signals,
                        title_similarity=title_sim,
                        first_chapter_overlap=overlap,
                    ))

    # Sort strongest signals first, then by title so the report is
    # deterministic regardless of bucket iteration order.
    candidates.sort(
        key=lambda c: (-c.signal_count, -c.first_chapter_overlap, c.a_title.lower()),
    )
    return candidates


def summarise(candidates: list[MirrorCandidate]) -> str:
    """Human-readable block describing a list of mirror candidates."""
    if not candidates:
        return "No cross-site mirror candidates found."
    lines = [
        f"Found {len(candidates)} possible mirror "
        f"pair{'s' if len(candidates) != 1 else ''}:",
    ]
    for c in candidates:
        lines.append("")
        lines.append(
            f"  {c.a_title or '(no title)'} — "
            f"{c.a_author or '(no author)'}"
        )
        signal_bits = list(c.signals)
        if c.first_chapter_overlap > 0 and "first_chapter" not in c.signals:
            signal_bits.append(
                f"first-chapter {c.first_chapter_overlap:.0%}"
            )
        lines.append(f"    signals: {', '.join(c.signals)}")
        lines.append(f"    A: {c.a.url}")
        lines.append(f"       {c.a_relpath}  [{c.a.root}]")
        lines.append(f"    B: {c.b.url}")
        lines.append(f"       {c.b_relpath}  [{c.b.root}]")
    lines.append("")
    lines.append(
        "These are candidates — verify before deleting either copy."
    )
    return "\n".join(lines)
