"""Plan and apply file moves to align a library with its path template.

Two-step: plan() returns a list of MoveOps the caller can show to the
user; apply() executes a (possibly filtered) subset. Keeping them
separate is what makes the CLI's dry-run mode and the GUI's per-row
checkbox review work off the same engine.

plan() reads only the library index, so run --scan-library first. Any
file not in the index is invisible to the reorganizer — it won't be
moved, but it won't be cleaned up either.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..updater import FileMetadata
from .index import LibraryIndex
from .template import DEFAULT_MISC_FOLDER, DEFAULT_TEMPLATE, render


@dataclass
class MoveOp:
    """One proposed file move. source/target are absolute paths."""

    source: Path
    target: Path
    source_url: str
    reason: str = "apply template"

    @property
    def is_rename(self) -> bool:
        """True if only the filename differs — same parent directory.
        Used by some UIs to label renames differently from relocations."""
        return self.source.parent == self.target.parent


@dataclass
class ApplyResult:
    applied: int = 0
    skipped: int = 0
    errors: int = 0
    messages: list[str] = field(default_factory=list)


def plan(
    root: Path,
    *,
    index_path: Path | None = None,
    template: str = DEFAULT_TEMPLATE,
    misc_folder: str = DEFAULT_MISC_FOLDER,
) -> list[MoveOp]:
    """Compute the moves that would bring this library into alignment
    with the template. Files already at their template-resolved target
    are omitted, so the result is directly the work the user sees."""
    root = Path(root).expanduser().resolve()
    idx = LibraryIndex.load(index_path)

    moves: list[MoveOp] = []
    for url, entry in idx.stories_in(root):
        md = _entry_to_metadata(url, entry)
        source = (root / entry["relpath"]).resolve(strict=False)
        target = (root / render(md, template=template, misc_folder=misc_folder)).resolve(
            strict=False
        )
        if source == target:
            continue
        moves.append(MoveOp(source=source, target=target, source_url=url))
    return moves


def apply(
    root: Path,
    moves: list[MoveOp],
    *,
    index_path: Path | None = None,
    selected_indices: Iterable[int] | None = None,
) -> ApplyResult:
    """Execute `moves`, or the subset selected by index (GUI checkbox
    behavior). Updates the library index to match the new layout and
    removes now-empty source directories.
    """
    root = Path(root).expanduser().resolve()
    idx = LibraryIndex.load(index_path)
    result = ApplyResult()

    if selected_indices is None:
        ops = list(moves)
    else:
        picked = set(int(i) for i in selected_indices)
        ops = [m for i, m in enumerate(moves) if i in picked]

    # Track source parents for an empty-dir cleanup pass after.
    touched_dirs: set[Path] = set()

    for op in ops:
        if not op.source.exists():
            result.errors += 1
            result.messages.append(f"source missing: {op.source}")
            continue
        if op.target.exists():
            # Safe default: skip rather than overwrite. The collision
            # might be the user's existing file that our scan didn't
            # catalog, or another indexed story that shares a target.
            # Either way, we don't silently obliterate data.
            result.skipped += 1
            result.messages.append(f"target exists, skipped: {op.target}")
            continue

        try:
            op.target.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move handles cross-filesystem, where Path.rename
            # would raise OSError(EXDEV).
            shutil.move(str(op.source), str(op.target))
        except OSError as exc:
            result.errors += 1
            result.messages.append(f"failed: {op.source.name}: {exc}")
            continue

        entry = idx.lookup_by_url(root, op.source_url)
        if entry is not None:
            entry["relpath"] = str(op.target.relative_to(root))

        touched_dirs.add(op.source.parent)
        result.applied += 1

    # Clean up source directories that became empty. Only remove
    # directories under the library root to avoid ever touching an
    # ancestor the user owns for other reasons.
    for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
        _cleanup_empty(d, root)

    if result.applied > 0:
        idx.save()

    return result


def _entry_to_metadata(url: str, entry: dict) -> FileMetadata:
    return FileMetadata(
        source_url=url,
        title=entry.get("title"),
        author=entry.get("author"),
        fandoms=list(entry.get("fandoms") or []),
        rating=entry.get("rating"),
        status=entry.get("status"),
        format=entry.get("format") or "",
        chapter_count=int(entry.get("chapter_count") or 0),
    )


def _cleanup_empty(directory: Path, root: Path) -> None:
    """Remove `directory` and any now-empty parents up to (but not
    including) `root`. Silently skips anything not empty or outside
    the library root."""
    try:
        root_resolved = root.resolve()
    except OSError:
        return

    current = directory
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == root_resolved:
            return
        try:
            resolved.relative_to(root_resolved)
        except ValueError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
