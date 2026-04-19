"""Scan orchestrator: walk → read → identify → index. No file moves."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..updater import extract_metadata
from .candidate import Confidence
from .identifier import identify
from .index import LibraryIndex


_EXTS = (".epub", ".html", ".txt")


def _walk_files(root: Path, recursive: bool) -> Iterator[Path]:
    """Yield every file under ``root`` without following symlinks.

    Using ``os.walk(followlinks=False)`` instead of ``Path.rglob``
    protects against two failure modes: self-referential symlinks
    that would loop forever, and unintended double-indexing when a
    user's library contains convenience symlinks to files that
    already live elsewhere in the tree.
    """
    if not recursive:
        for entry in root.iterdir():
            if entry.is_file() and not entry.is_symlink():
                yield entry
        return
    for dirpath, _dirnames, filenames in os.walk(str(root), followlinks=False):
        for fname in filenames:
            candidate = Path(dirpath) / fname
            # Still skip symlink files even when walking without
            # following — they could point outside the library or
            # duplicate indexed content.
            if candidate.is_symlink():
                continue
            yield candidate


@dataclass
class ScanResult:
    root: Path
    total_files: int = 0
    identified_via_url: int = 0
    ambiguous: int = 0
    errors: int = 0
    duplicates: int = 0
    error_files: list[tuple[Path, str]] = field(default_factory=list)


def scan(
    root: Path,
    *,
    index_path: Path | None = None,
    recursive: bool = True,
    clear_existing: bool = False,
) -> ScanResult:
    """Scan ``root`` and populate/update the library index.

    ``clear_existing`` replaces this library's entries with the scan
    results instead of merging — use it when the user wants orphans
    (files deleted off disk) dropped from the index.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")

    index = LibraryIndex.load(index_path)
    if clear_existing:
        index.clear_library(root)

    result = ScanResult(root=root)

    for path in _walk_files(root, recursive):
        if path.suffix.lower() not in _EXTS:
            continue
        result.total_files += 1
        try:
            md = extract_metadata(path)
            # Pass ``root`` so identify() can backfill the fandom from
            # the parent folder when the file's HTML metadata didn't
            # include one (most common on FicLab dumps whose tags row
            # mixes genres/characters/status/fandom into one blob).
            candidate = identify(path, md, root=root)
            # record() returns False when this candidate was a
            # duplicate of a story already indexed under the same
            # canonical URL — the second (third, …) copy is recorded
            # in the primary entry's duplicate_relpaths list. Surface
            # the count so --scan-library can tell the user.
            is_new = index.record(root, candidate)
            if not is_new:
                result.duplicates += 1
            if candidate.confidence == Confidence.HIGH:
                result.identified_via_url += 1
            else:
                result.ambiguous += 1
        except Exception as exc:
            result.errors += 1
            result.error_files.append((path, str(exc)))

    index.mark_scan_complete(root)
    index.save()
    return result
