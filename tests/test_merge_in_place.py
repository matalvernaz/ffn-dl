"""Tests for the merge-in-place update flow.

The update path used to download every chapter twice (once for the
delta, again for the re-export) — minutes of wasted network per story
when the local chapter cache was empty. The fix reads chapters
1..existing back out of the file on disk and concatenates them with
the freshly-downloaded new chapters, cutting the update to a single
network round-trip. These tests pin that behaviour so the shortcut
doesn't silently regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.cli import _merge_with_existing
from ffn_dl.exporters import export_epub, export_html, export_txt
from ffn_dl.models import Chapter, Story


class _FakeScraper:
    """Records calls so tests can assert whether a full re-download fired."""

    def __init__(self, full_story_chapters: list[Chapter]):
        self._full = full_story_chapters
        self.download_calls: list[dict] = []

    def download(self, url, *, skip_chapters=0, chapters=None, progress_callback=None):
        self.download_calls.append(
            {"url": url, "skip_chapters": skip_chapters, "chapters": chapters},
        )
        story = _story("https://x", chapters=[])
        story.chapters = list(self._full)
        return story


def _story(url: str, *, chapters: list[Chapter]) -> Story:
    s = Story(
        id=1, title="Fic", author="Auth", summary="sum", url=url,
    )
    s.metadata["status"] = "In-Progress"
    s.chapters = chapters
    return s


def _baseline_chapters(n: int) -> list[Chapter]:
    return [
        Chapter(number=i, title=f"Ch {i}", html=f"<p>body {i}</p>")
        for i in range(1, n + 1)
    ]


def test_merges_existing_html_with_new_chapters(tmp_path):
    """HTML file on disk + freshly-downloaded new chapters → one merged Story,
    no extra network hit."""
    existing = _story("https://x", chapters=_baseline_chapters(3))
    path = export_html(existing, str(tmp_path))

    new_only = _story("https://x", chapters=[
        Chapter(number=4, title="Ch 4", html="<p>body 4</p>"),
    ])
    scraper = _FakeScraper(full_story_chapters=[])

    merged = _merge_with_existing(
        new_only, scraper, "https://x", None,
        update_path=path, refetch_all=False,
        status=lambda _msg: None,
        progress_callback=None,
    )

    assert [c.number for c in merged.chapters] == [1, 2, 3, 4]
    assert [c.title for c in merged.chapters] == ["Ch 1", "Ch 2", "Ch 3", "Ch 4"]
    assert scraper.download_calls == [], "Merge-in-place must not re-download"


def test_merges_existing_epub_with_new_chapters(tmp_path):
    try:
        path = export_epub(
            _story("https://x", chapters=_baseline_chapters(3)), str(tmp_path),
        )
    except ImportError:
        pytest.skip("ebooklib not installed")

    new_only = _story("https://x", chapters=[
        Chapter(number=4, title="Ch 4", html="<p>body 4</p>"),
    ])
    scraper = _FakeScraper(full_story_chapters=[])

    merged = _merge_with_existing(
        new_only, scraper, "https://x", None,
        update_path=path, refetch_all=False,
        status=lambda _msg: None,
        progress_callback=None,
    )
    assert [c.number for c in merged.chapters] == [1, 2, 3, 4]
    assert scraper.download_calls == []


def test_refetch_all_triggers_full_redownload(tmp_path):
    """When refetch_all=True, merge shortcut is skipped regardless of
    whether the local file would be parseable. This is the escape
    hatch for authors who silently edited old chapters."""
    path = export_html(
        _story("https://x", chapters=_baseline_chapters(3)), str(tmp_path),
    )
    new_only = _story("https://x", chapters=[])
    full_refetch = _baseline_chapters(4)
    scraper = _FakeScraper(full_story_chapters=full_refetch)

    merged = _merge_with_existing(
        new_only, scraper, "https://x", None,
        update_path=path, refetch_all=True,
        status=lambda _msg: None,
        progress_callback=None,
    )
    assert len(scraper.download_calls) == 1
    assert scraper.download_calls[0]["skip_chapters"] == 0
    assert [c.number for c in merged.chapters] == [1, 2, 3, 4]


def test_txt_falls_back_to_full_redownload(tmp_path):
    """TXT exports are lossy (HTML stripped) so the reader refuses.
    The helper must silently fall back to the old re-download path
    rather than erroring out."""
    path = export_txt(
        _story("https://x", chapters=_baseline_chapters(3)), str(tmp_path),
    )
    new_only = _story("https://x", chapters=[])
    scraper = _FakeScraper(full_story_chapters=_baseline_chapters(4))

    merged = _merge_with_existing(
        new_only, scraper, "https://x", None,
        update_path=path, refetch_all=False,
        status=lambda _msg: None,
        progress_callback=None,
    )
    assert len(scraper.download_calls) == 1
    assert [c.number for c in merged.chapters] == [1, 2, 3, 4]


def test_corrupt_html_falls_back_to_full_redownload(tmp_path):
    """A local file that can't be parsed (hand-edited, truncated, or
    from a foreign downloader) must trigger a full re-download rather
    than bailing out — the update still has to succeed."""
    path = tmp_path / "truncated.html"
    path.write_text("<html><body><h1>Nope</h1></body></html>")
    new_only = _story("https://x", chapters=[])
    scraper = _FakeScraper(full_story_chapters=_baseline_chapters(4))

    status_lines: list[str] = []
    merged = _merge_with_existing(
        new_only, scraper, "https://x", None,
        update_path=path, refetch_all=False,
        status=status_lines.append,
        progress_callback=None,
    )
    assert len(scraper.download_calls) == 1
    assert any("re-download" in line.lower() for line in status_lines), (
        "user should see why we fell back to the slower path"
    )
    assert [c.number for c in merged.chapters] == [1, 2, 3, 4]


def test_merge_sorts_chapters_by_number(tmp_path):
    """The reader returns chapters sorted by number, but a defensive
    final sort ensures any out-of-order new chapters from the scraper
    (shouldn't happen, but cheap insurance) end up in the right spot
    for the exporter."""
    existing = _story("https://x", chapters=_baseline_chapters(3))
    path = export_html(existing, str(tmp_path))
    # New chapters deliberately out of order — merge must re-sort.
    new_only = _story("https://x", chapters=[
        Chapter(number=5, title="Ch 5", html="<p>5</p>"),
        Chapter(number=4, title="Ch 4", html="<p>4</p>"),
    ])
    scraper = _FakeScraper(full_story_chapters=[])

    merged = _merge_with_existing(
        new_only, scraper, "https://x", None,
        update_path=path, refetch_all=False,
        status=lambda _msg: None,
        progress_callback=None,
    )
    assert [c.number for c in merged.chapters] == [1, 2, 3, 4, 5]
