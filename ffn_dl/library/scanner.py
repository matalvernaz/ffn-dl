"""Scan orchestrator: walk → read → identify → index. No file moves."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..updater import extract_metadata
from .candidate import Confidence
from .identifier import identify
from .index import LibraryIndex


_EXTS = (".epub", ".html", ".txt")


@dataclass
class ScanResult:
    root: Path
    total_files: int = 0
    identified_via_url: int = 0
    ambiguous: int = 0
    errors: int = 0
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
    iterator = root.rglob("*") if recursive else root.iterdir()

    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in _EXTS:
            continue
        result.total_files += 1
        try:
            md = extract_metadata(path)
            candidate = identify(path, md)
            index.record(root, candidate)
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
