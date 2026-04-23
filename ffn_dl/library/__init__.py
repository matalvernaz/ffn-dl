"""Library manager: scan a directory of story files, identify them,
track them in a persistent index, and sort downloads by category.

Works with ffn-dl's own output as well as files produced by other
downloaders (FanFicFare, FicHub, bare HTML scrapes) when enough
metadata survived.
"""

from .candidate import Confidence, StoryCandidate
from .doctor import HealReport, IntegrityReport, check_integrity, heal
from .find import LibraryMatch, search_index
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
]
