"""Atomic file writes.

A crash (OS kill, power loss, user Ctrl-C mid-export) is much more
likely to happen during a long-running ffn-dl run than is obvious — a
library-wide update hits hundreds of file writes, and losing one to a
half-written file corrupts the entry silently. The symptoms then show
up later as "why is this EPUB unreadable?" or "why did the next scan
mark this story as untrackable?"

This module centralises the write-to-tmp, fsync, rename-over-target
idiom so every file the program persists lands whole-or-not-at-all.
Before this existed, only ``LibraryIndex.save`` and ``watchlist.save``
bothered; exporters and the scraper cache wrote directly to the
destination, and an interrupted export left a partial file sitting in
the library.

Design notes:

* The rename is atomic on POSIX and (since Python 3.3) on Windows when
  ``os.replace`` is used. We never use ``os.rename`` here because it
  fails if the destination exists on Windows.

* ``fsync`` on the temp file forces pending page-cache data to disk
  before the rename is committed, so a power loss between the rename
  and the actual data hitting the platter still leaves either the old
  file or the new one — never garbage.

* We do *not* fsync the enclosing directory. On ext4 + most journalled
  filesystems the rename itself is journalled and durable; the extra
  sync would double the wall time of a library-wide export run for a
  guarantee users don't need (the index is our source of truth; a
  missing file just triggers a re-download). If a durability-critical
  caller ever needs it, pass ``fsync_dir=True``.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Union

PathLike = Union[str, os.PathLike[str]]


def atomic_write_text(
    path: PathLike,
    content: str,
    *,
    encoding: str = "utf-8",
    fsync_dir: bool = False,
) -> None:
    """Write ``content`` to ``path`` atomically.

    The target path either ends up with the full new content or is
    left untouched; there's no half-written window a concurrent
    reader (or a crash-restart re-scan) could observe.
    """
    _atomic_write(
        path,
        content.encode(encoding),
        fsync_dir=fsync_dir,
    )


def atomic_write_bytes(
    path: PathLike,
    content: bytes,
    *,
    fsync_dir: bool = False,
) -> None:
    """Binary equivalent of :func:`atomic_write_text`.

    Useful for anything that's already been serialised into bytes —
    cover images, the EPUB zip (when we've built it in memory first),
    and other binary artefacts.
    """
    _atomic_write(path, content, fsync_dir=fsync_dir)


@contextmanager
def atomic_path(
    target: PathLike,
    *,
    suffix: str = ".tmp",
    fsync_dir: bool = False,
) -> Generator[Path, None, None]:
    """Yield a temporary Path in the same directory as ``target``.

    On successful exit, ``target`` is atomically replaced by the temp
    file's contents. On any exception, the temp file is removed and
    the original ``target`` is left untouched.

    Use this when a third-party library insists on writing to a
    filesystem path itself (``ebooklib.write_epub``, for example):
    hand it the yielded temp path instead of the real target and let
    the context manager swap it in atomically on success.
    """
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp returns a file descriptor we immediately close — the
    # third-party writer opens the path itself. The mkstemp prefix
    # keeps the temp file hidden in directory listings.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=suffix,
        dir=str(target_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        yield tmp_path
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    else:
        os.replace(tmp_path, target_path)
        if fsync_dir:
            _fsync_dir(target_path.parent)


def _atomic_write(
    path: PathLike,
    payload: bytes,
    *,
    fsync_dir: bool,
) -> None:
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            # fsync the file before rename so the rename commit can't
            # outrun the data to disk on a crash.
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync isn't supported on every filesystem (some
                # network mounts, tmpfs in unusual configurations).
                # The rename is still atomic — we just lose the
                # durability guarantee on those mounts.
                pass
        os.replace(tmp_name, target_path)
    except BaseException:
        # The fdopen block already closed the fd on success; on
        # failure it may or may not have, so we try unlink and ignore
        # a "file gone" error.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    if fsync_dir:
        _fsync_dir(target_path.parent)


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory. Silent on platforms (Windows)
    that don't allow opening a directory for fsync."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
