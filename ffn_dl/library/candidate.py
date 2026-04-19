"""StoryCandidate — one file's worth of identification state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..updater import FileMetadata


class Confidence(Enum):
    """How sure we are that this candidate maps to a real, updatable story.

    HIGH — source URL was embedded and matched an adapter we support.
    MEDIUM — no URL, but title+author produced a strong single search hit.
    LOW — nothing conclusive; file is indexed but won't be auto-updated.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class StoryCandidate:
    """A file encountered during a library scan, plus what we learned.

    Wraps a FileMetadata (what was read off disk) with identification
    context (how confident we are, which adapter handles the source).
    """

    path: Path
    metadata: FileMetadata
    confidence: Confidence = Confidence.LOW
    adapter_name: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def format(self) -> str:
        return self.metadata.format

    @property
    def is_trackable(self) -> bool:
        """True when future --update-all runs can re-check this story.
        LOW-confidence candidates are indexed but skipped on update."""
        return self.confidence in (Confidence.HIGH, Confidence.MEDIUM)
