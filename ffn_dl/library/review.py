"""Promote untrackable library entries once a user supplies a URL.

Phase 1's scanner leaves files with title+author but no embedded URL
in the ``untrackable`` list — indexed but not auto-updatable. The
review flow lets the user provide a URL per file (manually for now,
eventually by picking from a fuzzy-search result set). Confirmed
entries migrate to ``stories`` with MEDIUM confidence so subsequent
--update-library runs pick them up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .candidate import Confidence
from .identifier import adapter_for_url
from .index import LibraryIndex


@dataclass
class PromotionResult:
    """Outcome of attempting to promote one untrackable file."""

    ok: bool
    message: str
    adapter: Optional[str] = None


def promote_untrackable(
    idx: LibraryIndex,
    root: Path,
    relpath: str,
    url: str,
    *,
    save: bool = True,
) -> PromotionResult:
    """Move an untrackable entry to the stories list with the given URL.

    Fails (returns ``ok=False``) if the URL doesn't match any adapter
    or the entry isn't in the untrackable list. Caller may pass
    ``save=False`` to batch many promotions into a single index
    write — useful for the GUI review dialog.
    """
    url = url.strip()
    adapter = adapter_for_url(url)
    if adapter is None:
        return PromotionResult(
            ok=False,
            message=(
                f"URL {url!r} does not match any supported site; "
                "nothing promoted."
            ),
        )

    lib = idx.library_state(root)
    untrackable = lib.get("untrackable") or []
    matching = [e for e in untrackable if e.get("relpath") == relpath]
    if not matching:
        return PromotionResult(
            ok=False,
            message=f"No untrackable entry at {relpath!r}.",
        )
    entry = matching[0]

    lib["untrackable"] = [e for e in untrackable if e is not entry]

    lib.setdefault("stories", {})[url] = {
        "relpath": relpath,
        "title": entry.get("title"),
        "author": entry.get("author"),
        "fandoms": [],
        "rating": None,
        "status": None,
        "adapter": adapter,
        "format": entry.get("format") or "",
        "confidence": Confidence.MEDIUM.value,
        "chapter_count": 0,
        "last_checked": datetime.now(tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }

    if save:
        idx.save()
    return PromotionResult(ok=True, message="Promoted.", adapter=adapter)


def untrackable_for_root(idx: LibraryIndex, root: Path) -> list[dict]:
    """Helper: all untrackable entries for a library root, as plain dicts.

    Thin wrapper over LibraryIndex.untrackable_in — exposed here so
    the review module is the single surface a caller needs to import.
    """
    return idx.untrackable_in(root)
