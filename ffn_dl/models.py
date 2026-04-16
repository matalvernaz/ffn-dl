"""Data models for stories and chapters."""

from dataclasses import dataclass, field


@dataclass
class Chapter:
    number: int
    title: str
    html: str


@dataclass
class Story:
    id: int
    title: str
    author: str
    summary: str
    url: str
    chapters: list[Chapter] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
