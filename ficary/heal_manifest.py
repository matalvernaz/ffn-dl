"""Timestamped record of what a destructive doctor heal changed.

Rounds 8–9 of the audit both found doctor data-loss bugs; the systemic
fix is that every destructive heal (index drops, watchlist drops, cache
prune) writes a manifest naming the pre-heal snapshots and the cache
quarantine directory, and ``--doctor-restore-last`` rolls the most
recent one back in a single command. Manifests live under
``portable_root()/heal-manifests/`` with the same stamp+salt naming and
depth cap as :mod:`ficary.library.backup`.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import portable
from .atomic import atomic_write_text

logger = logging.getLogger(__name__)

_MANIFEST_DIRNAME = "heal-manifests"
_MAX_MANIFESTS = 10
_NAME_RE = re.compile(r"^heal-(\d{8}-\d{6})-[0-9a-f]{8}\.json$")


@dataclass
class HealManifest:
    created_at: str = ""
    label: str = ""
    index_snapshot: Optional[str] = None
    watchlist_snapshot: Optional[str] = None
    cache_quarantine_dir: Optional[str] = None
    dropped_index_entries: int = 0
    dropped_watches: int = 0
    pruned_cache_entries: int = 0
    restored_at: str = ""
    path: str = field(default="", compare=False)

    def has_anything_to_restore(self) -> bool:
        return bool(
            self.index_snapshot
            or self.watchlist_snapshot
            or self.cache_quarantine_dir
        )


def manifest_dir() -> Path:
    return Path(portable.portable_root()) / _MANIFEST_DIRNAME


def write_manifest(manifest: HealManifest) -> Path:
    """Persist ``manifest`` under a fresh stamped name and prune old
    ones past the depth cap. Returns the manifest path."""
    directory = manifest_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    salt = uuid.uuid4().hex[:8]
    path = directory / f"heal-{stamp}-{salt}.json"
    manifest.created_at = manifest.created_at or time.strftime(
        "%Y-%m-%dT%H:%M:%S")
    manifest.path = str(path)
    payload = {k: v for k, v in asdict(manifest).items() if k != "path"}
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")
    _prune_old(directory)
    return path


def load_manifest(path: Path) -> Optional[HealManifest]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable heal manifest %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    manifest = HealManifest(path=str(path))
    for key, value in data.items():
        if hasattr(manifest, key) and key != "path":
            setattr(manifest, key, value)
    return manifest


def list_manifests() -> list[Path]:
    directory = manifest_dir()
    if not directory.is_dir():
        return []
    entries = [p for p in directory.iterdir() if _NAME_RE.match(p.name)]
    entries.sort(key=lambda p: p.name, reverse=True)
    return entries


def latest_manifest() -> Optional[HealManifest]:
    for path in list_manifests():
        manifest = load_manifest(path)
        if manifest is not None:
            return manifest
    return None


def mark_restored(manifest: HealManifest) -> None:
    """Stamp ``restored_at`` into the on-disk manifest so a second
    ``--doctor-restore-last`` is visibly a re-run, not fresh."""
    manifest.restored_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    if manifest.path:
        payload = {k: v for k, v in asdict(manifest).items() if k != "path"}
        try:
            atomic_write_text(Path(manifest.path),
                              json.dumps(payload, indent=2) + "\n")
        except OSError:
            logger.warning("Couldn't stamp restored_at on %s", manifest.path)


def _prune_old(directory: Path) -> None:
    entries = [p for p in directory.iterdir() if _NAME_RE.match(p.name)]
    entries.sort(key=lambda p: p.name, reverse=True)
    for old in entries[_MAX_MANIFESTS:]:
        try:
            old.unlink()
        except OSError:
            pass
