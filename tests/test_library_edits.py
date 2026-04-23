"""Chapter-content silent-edit detection: bootstrap + scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl.content_hash import hash_chapter
from ffn_dl.library import (
    bootstrap_hashes,
    scan_edits,
    stored_hashes,
)
from ffn_dl.library.index import LibraryIndex
from ffn_dl.models import Chapter, Story


def _fresh_index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(
        tmp_path / "library-index.json",
        {"version": 1, "libraries": {}},
    )


def _seed_entry(
    index: LibraryIndex,
    root: Path,
    url: str,
    relpath: str,
    *,
    chapter_count: int = 1,
    chapter_hashes: list[str] | None = None,
) -> None:
    entry = {
        "relpath": relpath,
        "title": "T", "author": "A",
        "fandoms": [], "adapter": "ffn",
        "format": "html", "confidence": "high",
        "chapter_count": chapter_count,
        "last_checked": "2026-04-01T00:00:00Z",
    }
    if chapter_hashes is not None:
        entry["chapter_hashes"] = list(chapter_hashes)
    index.library_state(root)["stories"][url] = entry


def _export_html(path: Path, chapters: list[tuple[int, str, str]]) -> None:
    """Write a minimal ffn-dl-shaped HTML export that ``read_chapters``
    can parse back. Chapters are ``(number, title, body_html)``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        '<!DOCTYPE html><html><body>',
    ]
    for num, title, body in chapters:
        parts.append(
            f'<div class="chapter" id="chapter-{num}">'
            f'<h2>{title}</h2>'
            f'{body}'
            '</div><hr>'
        )
    parts.append('</body></html>')
    path.write_text("".join(parts), encoding="utf-8")


# ── Bootstrap ─────────────────────────────────────────────────────

class TestBootstrap:
    def test_empty_library_reports_zeros(self, tmp_path):
        idx = _fresh_index(tmp_path)
        report = bootstrap_hashes(tmp_path, idx)
        assert report.populated == []
        assert report.already_hashed == []

    def test_populates_hashes_from_local_html(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        story_file = root / "story.html"
        _export_html(story_file, [
            (1, "Chapter 1", "<p>One</p>"),
            (2, "Chapter 2", "<p>Two</p>"),
        ])
        _seed_entry(idx, root, "https://x/1", "story.html", chapter_count=2)

        report = bootstrap_hashes(root, idx)
        assert report.populated == ["https://x/1"]

        entry = idx.lookup_by_url(root, "https://x/1")
        hashes = stored_hashes(entry)
        assert hashes is not None
        assert len(hashes) == 2

    def test_skips_entries_with_existing_hashes_by_default(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        story_file = root / "story.html"
        _export_html(story_file, [(1, "C1", "<p>hi</p>")])
        _seed_entry(
            idx, root, "https://x/1", "story.html",
            chapter_hashes=["already-have-this"],
        )

        report = bootstrap_hashes(root, idx)
        assert report.populated == []
        assert report.already_hashed == ["https://x/1"]
        # The stored hash list stays untouched.
        entry = idx.lookup_by_url(root, "https://x/1")
        assert stored_hashes(entry) == ["already-have-this"]

    def test_force_rehash_overwrites_existing(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        story_file = root / "story.html"
        _export_html(story_file, [(1, "C1", "<p>hello</p>")])
        _seed_entry(
            idx, root, "https://x/1", "story.html",
            chapter_hashes=["stale-hash"],
        )

        report = bootstrap_hashes(root, idx, force=True)
        assert report.populated == ["https://x/1"]

        entry = idx.lookup_by_url(root, "https://x/1")
        hashes = stored_hashes(entry)
        assert hashes is not None
        assert hashes != ["stale-hash"]

    def test_skips_missing_file(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()
        _seed_entry(idx, root, "https://x/1", "gone.html")

        report = bootstrap_hashes(root, idx)
        assert report.skipped_missing_file == ["https://x/1"]
        assert report.populated == []

    def test_skips_unreadable_txt(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()
        txt = root / "story.txt"
        txt.write_text("plain text body", encoding="utf-8")
        _seed_entry(idx, root, "https://x/1", "story.txt")

        report = bootstrap_hashes(root, idx)
        assert len(report.skipped_unreadable) == 1
        assert report.skipped_unreadable[0][0] == "https://x/1"

    def test_does_not_save_index(self, tmp_path):
        """Callers batch the save so a ctrl-C mid-run doesn't leave
        the index file half-updated."""
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        _export_html(root / "x.html", [(1, "c", "<p>x</p>")])
        _seed_entry(idx, root, "https://x/1", "x.html")

        bootstrap_hashes(root, idx)
        # Index was mutated in memory but not written to disk yet.
        assert not idx.path.exists()


# ── Scan ──────────────────────────────────────────────────────────

class _FakeScraper:
    """Test double that returns pre-canned Story objects instead of
    touching the network."""

    def __init__(self, stories_by_url):
        self.stories_by_url = stories_by_url
        self.calls = []

    def download(self, url, **kwargs):
        self.calls.append(url)
        story = self.stories_by_url.get(url)
        if story is None:
            from ffn_dl.scraper import StoryNotFoundError
            raise StoryNotFoundError(f"no such story: {url}")
        return story


def _make_story(url, chapters: list[tuple[int, str]]) -> Story:
    """Build a Story with the given ``(number, html)`` chapters."""
    s = Story(id=1, title="T", author="A", summary="", url=url)
    for num, html in chapters:
        s.chapters.append(Chapter(number=num, title=f"C{num}", html=html))
    return s


class TestScanEdits:
    def test_unchanged_content_reports_no_drift(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()

        stored = [hash_chapter("<p>a</p>"), hash_chapter("<p>b</p>")]
        _seed_entry(
            idx, root, "https://www.fanfiction.net/s/1",
            "x.html", chapter_count=2, chapter_hashes=stored,
        )

        scraper = _FakeScraper({
            "https://www.fanfiction.net/s/1": _make_story(
                "https://www.fanfiction.net/s/1",
                [(1, "<p>a</p>"), (2, "<p>b</p>")],
            ),
        })

        report = scan_edits(
            root, idx,
            scraper_cache={"ffn": scraper},
        )
        assert report.silent_edits == []
        assert report.count_changes == []
        assert report.unchanged == 1
        assert report.is_clean()

    def test_silent_edit_detected(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()

        stored = [hash_chapter("<p>original</p>"), hash_chapter("<p>b</p>")]
        _seed_entry(
            idx, root, "https://www.fanfiction.net/s/1",
            "x.html", chapter_count=2, chapter_hashes=stored,
        )

        scraper = _FakeScraper({
            "https://www.fanfiction.net/s/1": _make_story(
                "https://www.fanfiction.net/s/1",
                [(1, "<p>REVISED</p>"), (2, "<p>b</p>")],
            ),
        })

        report = scan_edits(root, idx, scraper_cache={"ffn": scraper})
        assert len(report.silent_edits) == 1
        assert report.silent_edits[0].changed_chapters == [1]
        assert report.unchanged == 0
        assert not report.is_clean()

    def test_count_change_reported_separately(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()

        stored = [hash_chapter("<p>a</p>")]
        _seed_entry(
            idx, root, "https://www.fanfiction.net/s/1",
            "x.html", chapter_count=1, chapter_hashes=stored,
        )
        scraper = _FakeScraper({
            "https://www.fanfiction.net/s/1": _make_story(
                "https://www.fanfiction.net/s/1",
                [(1, "<p>a</p>"), (2, "<p>new</p>")],
            ),
        })

        report = scan_edits(root, idx, scraper_cache={"ffn": scraper})
        assert report.silent_edits == []
        assert len(report.count_changes) == 1
        change = report.count_changes[0]
        assert change.local_count == 1
        assert change.remote_count == 2

    def test_story_not_found_treated_as_count_change(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()

        _seed_entry(
            idx, root, "https://www.fanfiction.net/s/1",
            "x.html", chapter_count=3,
            chapter_hashes=["a", "b", "c"],
        )
        scraper = _FakeScraper({})  # no stories — all lookups miss

        report = scan_edits(root, idx, scraper_cache={"ffn": scraper})
        assert len(report.count_changes) == 1
        assert report.count_changes[0].remote_count == 0

    def test_skips_entries_without_baseline(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()

        _seed_entry(
            idx, root, "https://www.fanfiction.net/s/1",
            "x.html", chapter_count=1,
            # no chapter_hashes set
        )
        scraper = _FakeScraper({})

        report = scan_edits(root, idx, scraper_cache={"ffn": scraper})
        assert report.skipped_no_baseline == ["https://www.fanfiction.net/s/1"]
        # No network call was made for a story without a baseline.
        assert scraper.calls == []

    def test_progress_callback_invoked(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()

        for i in range(3):
            url = f"https://www.fanfiction.net/s/{i}"
            _seed_entry(
                idx, root, url, f"x{i}.html",
                chapter_count=1,
                chapter_hashes=[hash_chapter(f"<p>{i}</p>")],
            )
        stories = {
            f"https://www.fanfiction.net/s/{i}":
            _make_story(
                f"https://www.fanfiction.net/s/{i}",
                [(1, f"<p>{i}</p>")],
            )
            for i in range(3)
        }

        events = []
        scan_edits(
            root, idx,
            progress=lambda n, t, u: events.append((n, t, u)),
            scraper_cache={"ffn": _FakeScraper(stories)},
        )
        assert [e[0] for e in events] == [1, 2, 3]
        assert all(e[1] == 3 for e in events)

    def test_progress_exception_does_not_abort_scan(self, tmp_path):
        idx = _fresh_index(tmp_path)
        root = tmp_path / "lib"
        root.mkdir()
        _seed_entry(
            idx, root, "https://www.fanfiction.net/s/1",
            "x.html",
            chapter_hashes=[hash_chapter("<p>x</p>")],
        )
        scraper = _FakeScraper({
            "https://www.fanfiction.net/s/1": _make_story(
                "https://www.fanfiction.net/s/1", [(1, "<p>x</p>")],
            ),
        })

        def bad_progress(*a, **k):
            raise RuntimeError("boom")

        report = scan_edits(
            root, idx,
            progress=bad_progress,
            scraper_cache={"ffn": scraper},
        )
        # Scan completed despite the progress callback raising.
        assert report.scanned == 1
        assert report.unchanged == 1
