"""Library index search — plain substring matching across metadata.

Users with 700+ stories indexed don't want to grep the JSON to find
"that one Harry Potter AU I read two years ago". The match surface
covers title, author, fandom list, and URL — the fields the user
actually remembers. Case-insensitive substring (plus a few small
niceties below) beats fuzzy matching for the typical query length
of 1-3 words: fuzzy adds false positives that make scanning harder.

Returned matches are :class:`LibraryMatch` dataclasses so a CLI can
render them and a GUI can populate a list control without either
having to care about the underlying index shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .index import LibraryIndex


@dataclass
class LibraryMatch:
    """One hit from :func:`search_index`."""

    root: Path
    url: str
    entry: dict

    # Convenience accessors so callers don't all have to memorise
    # the index entry shape. Returning "" (rather than None) on
    # missing keys means downstream formatting stays uniform — a
    # GUI renders an empty column instead of crashing on a None.

    @property
    def title(self) -> str:
        return self.entry.get("title") or ""

    @property
    def author(self) -> str:
        return self.entry.get("author") or ""

    @property
    def fandoms(self) -> list[str]:
        return list(self.entry.get("fandoms") or [])

    @property
    def relpath(self) -> str:
        return self.entry.get("relpath") or ""

    @property
    def absolute_path(self) -> Path:
        return self.root / self.relpath if self.relpath else self.root


def search_index(
    index: LibraryIndex,
    query: str,
    *,
    roots: Iterable[Path] | None = None,
    limit: int | None = None,
) -> list[LibraryMatch]:
    """Return stories matching ``query`` across ``roots``.

    When ``roots`` is ``None``, every library root in the index is
    searched. ``limit`` caps the result count; pass ``None`` for "no
    cap". Matching is case-insensitive substring against the title,
    author, every fandom, and the canonical URL — concatenated so a
    multi-word query like ``harry potter au`` can match a title
    that's literally "Harry Potter AU" even though the tokens are
    split across fields when indexed.

    Ordering: index insertion order within each root, then roots in
    the order they appear in ``index.library_roots()``. This is
    stable across runs, which matters for anyone scripting on top
    of the output.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []

    if roots is None:
        roots_list = [Path(r) for r in index.library_roots()]
    else:
        roots_list = [Path(r) for r in roots]

    matches: list[LibraryMatch] = []
    for root in roots_list:
        for url, entry in index.stories_in(root):
            haystack = " ".join([
                entry.get("title") or "",
                entry.get("author") or "",
                " ".join(entry.get("fandoms") or []),
                url,
            ]).lower()
            if needle in haystack:
                matches.append(LibraryMatch(root=root, url=url, entry=entry))
                if limit is not None and len(matches) >= limit:
                    return matches
    return matches
