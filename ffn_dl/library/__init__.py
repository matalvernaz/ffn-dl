"""Library manager: scan a directory of story files, identify them,
track them in a persistent index, and sort downloads by category.

Works with ffn-dl's own output as well as files produced by other
downloaders (FanFicFare, FicHub, bare HTML scrapes) when enough
metadata survived.
"""

from .candidate import Confidence, StoryCandidate

__all__ = ["Confidence", "StoryCandidate"]
