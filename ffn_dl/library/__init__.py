"""Library manager: scan a directory of story files, identify them,
track them in a persistent index, and sort downloads by category.

Works with ffn-dl's own output as well as files produced by other
downloaders (FanFicFare, FicHub, bare HTML scrapes) when enough
metadata survived.
"""

from . import backup
from .candidate import Confidence, StoryCandidate
from .doctor import HealReport, IntegrityReport, check_integrity, heal
from .edits import (
    BootstrapReport,
    CountChange,
    ScanReport,
    SilentEdit,
    bootstrap_hashes,
    scan_edits,
)
from .find import LibraryMatch, search_index
from .fulltext import (
    BootstrapReport as FullTextBootstrapReport,
    FullTextHit,
    FullTextIndex,
    chapter_text,
    default_db_path as default_search_db_path,
    populate_from_library as populate_fulltext_from_library,
)
from .hashes import (
    ChapterHashUnavailable,
    compute_local_hashes,
    store_hashes,
    stored_hashes,
)
from .stats import LibraryStats, compute_stats

__all__ = [
    "Confidence",
    "StoryCandidate",
    "IntegrityReport",
    "HealReport",
    "check_integrity",
    "heal",
    "LibraryStats",
    "compute_stats",
    "LibraryMatch",
    "search_index",
    "FullTextHit",
    "FullTextIndex",
    "FullTextBootstrapReport",
    "chapter_text",
    "default_search_db_path",
    "populate_fulltext_from_library",
    "backup",
    "ChapterHashUnavailable",
    "compute_local_hashes",
    "store_hashes",
    "stored_hashes",
    "BootstrapReport",
    "ScanReport",
    "SilentEdit",
    "CountChange",
    "bootstrap_hashes",
    "scan_edits",
]
