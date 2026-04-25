"""Tests for the merge-in-place update flow.

The update path used to download every chapter twice (once for the
delta, again for the re-export) — minutes of wasted network per story
when the local chapter cache was empty. The fix reads chapters
1..existing back out of the file on disk and concatenates them with
the freshly-downloaded new chapters, cutting the update to a single
network round-trip. These tests pin that behaviour so the shortcut
doesn't silently regress.

Also exercises the legacy-format pre-check and filename preservation
at the ``_download_one`` level: a file the merge step can't read must
trigger a single full download (no metadata-fetch double-tap) and the
exported file must land at ``update_path`` rather than orphaning the
old one under a templated filename.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ffn_dl.cli import _download_one, _merge_with_existing
from ffn_dl.exporters import DEFAULT_TEMPLATE, export_epub, export_html, export_txt
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


# ── _download_one: legacy-format pre-check + filename preservation ───
#
# The merge-in-place fallback used to fire AFTER the first download.
# That meant a foreign-format file (FicLab, Calibre, older home-brew)
# took two metadata fetches and a wasted skip=existing first pass
# before settling into a clean re-download. The pre-check below
# detects unreadable files up front so the first ``scraper.download``
# already runs with skip=0; tests pin that single-call shape.
#
# The filename-preservation rename runs after the export so the
# legacy file gets *replaced*, not orphaned alongside a new template-
# named twin. Without this, the old file stays on disk and the next
# update cycle hits the same legacy-format fallback forever.


class _StubScraper:
    """Scraper double for ``_download_one`` integration. Returns a
    pre-built Story for ``download``, no real HTTP."""

    site_name = "stub"
    concurrency = 1

    def __init__(self, story: Story):
        self._story = story
        self.download_calls: list[dict] = []

    def parse_story_id(self, url):
        return "stub-1"

    def download(self, url, *, skip_chapters=0, chapters=None, progress_callback=None):
        self.download_calls.append({
            "url": url,
            "skip_chapters": skip_chapters,
            "chapters": chapters,
        })
        # Mirror real scrapers: skip=N drops chapters 1..N from the
        # returned Story so the caller's new_count math is right.
        out = _story(self._story.url or "https://x", chapters=[])
        out.title = self._story.title
        out.author = self._story.author
        out.metadata = dict(self._story.metadata)
        out.chapters = [
            c for c in self._story.chapters if c.number > skip_chapters
        ]
        return out

    def clean_cache(self, story_id):  # pragma: no cover — never called in these tests
        pass


def _download_args(format_: str = "html") -> argparse.Namespace:
    """Minimal Namespace for ``_download_one`` to read off."""
    return argparse.Namespace(
        format=format_,
        name=DEFAULT_TEMPLATE,
        chapters=None,
        max_retries=5,
        no_cache=True,
        delay_min=None,
        delay_max=None,
        chunk_size=None,
        use_wayback=False,
        cf_solve=False,
        refetch_all=False,
        hr_as_stars=False,
        strip_notes=False,
        send_to_kindle=None,
        clean_cache=False,
        speech_rate="0",
        attribution="builtin",
        attribution_model_size="",
    )


def _patch_scraper(monkeypatch, stub):
    """Pin ``_build_scraper`` to return our stub regardless of URL."""
    from ffn_dl import cli

    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: stub)


def test_download_one_legacy_format_makes_one_full_download(tmp_path, monkeypatch):
    """A non-ffn-dl HTML file (no ``<div class="chapter">`` blocks)
    used to trigger the merge fallback's *second* metadata fetch
    after a wasted skip=N first pass. The pre-check now catches it
    up front, so the single download fires with skip=0."""
    legacy_path = tmp_path / "Legacy Title.html"
    # Old-style export: <h2> chapter headers, no chapter divs.
    legacy_path.write_text(
        "<html><body><h2>Chapter 1: Start</h2><p>old prose</p>"
        "<h2>Chapter 2: Middle</h2><p>more</p></body></html>"
    )
    upstream_full = _baseline_chapters(3)
    stub = _StubScraper(_story("https://x", chapters=list(upstream_full)))
    _patch_scraper(monkeypatch, stub)

    status_lines: list[str] = []
    args = _download_args()
    ok = _download_one(
        "https://x", args, tmp_path,
        update_path=legacy_path,
        existing_chapters=2,  # caller's hint from index — we should ignore it
        status_callback=status_lines.append,
    )
    assert ok is True
    assert len(stub.download_calls) == 1, (
        "legacy-format pre-check must skip the wasteful first pass"
    )
    assert stub.download_calls[0]["skip_chapters"] == 0
    assert any("legacy-format" in line.lower() for line in status_lines), (
        "user should see why we did a clean re-export"
    )


def test_download_one_legacy_format_preserves_filename(tmp_path, monkeypatch):
    """The legacy file's filename must be reused for the new export.
    Without the rename, the export writes ``{title} - {author}.html``
    next to the legacy file and the next update cycle hits the same
    legacy-format fallback against the un-replaced original."""
    legacy_path = tmp_path / "User Hand-Named.html"
    legacy_path.write_text("<html><body><h2>Old</h2></body></html>")
    upstream_full = _baseline_chapters(2)
    stub = _StubScraper(_story("https://x", chapters=list(upstream_full)))
    _patch_scraper(monkeypatch, stub)

    args = _download_args()
    ok = _download_one(
        "https://x", args, tmp_path,
        update_path=legacy_path,
        existing_chapters=0,
        status_callback=lambda _msg: None,
    )
    assert ok is True
    assert legacy_path.exists(), "the original filename must still hold the file"
    # No twin file landed under the templated name.
    siblings = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert siblings == ["User Hand-Named.html"]


def test_download_one_ffn_dl_format_uses_skip_existing(tmp_path, monkeypatch):
    """For a real ffn-dl-format file, the pre-check parses out the
    existing chapters so the scraper can ask for only the new ones —
    no behaviour change for the well-formed case."""
    existing = _story("https://x", chapters=_baseline_chapters(3))
    update_path = export_html(existing, str(tmp_path))

    # Upstream has chapter 4 too; the stub will trim 1..3 when the
    # caller passes skip=3.
    full = _baseline_chapters(4)
    stub = _StubScraper(_story("https://x", chapters=list(full)))
    _patch_scraper(monkeypatch, stub)

    args = _download_args()
    ok = _download_one(
        "https://x", args, tmp_path,
        update_path=update_path,
        existing_chapters=3,
        status_callback=lambda _msg: None,
    )
    assert ok is True
    assert len(stub.download_calls) == 1
    assert stub.download_calls[0]["skip_chapters"] == 3, (
        "well-formed files keep using the merge-in-place skip"
    )


def test_download_one_update_keeps_original_filename_even_when_template_differs(
    tmp_path, monkeypatch,
):
    """Update-mode: title in upstream metadata can drift from what
    the user named the file. The export still lands at update_path
    so the user's filename choice survives."""
    update_path = tmp_path / "My Custom Name.html"
    # Build a valid ffn-dl HTML so the merge path runs.
    seed = _story("https://x", chapters=_baseline_chapters(2))
    seed.title = "Original Title"
    written = export_html(seed, str(tmp_path))
    written.replace(update_path)

    upstream = _story("https://x", chapters=_baseline_chapters(3))
    upstream.title = "Renamed Upstream"
    stub = _StubScraper(upstream)
    _patch_scraper(monkeypatch, stub)

    args = _download_args()
    ok = _download_one(
        "https://x", args, tmp_path,
        update_path=update_path,
        existing_chapters=2,
        status_callback=lambda _msg: None,
    )
    assert ok is True
    assert update_path.exists()
    siblings = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert siblings == ["My Custom Name.html"]
