"""Tests for the index-driven refresh engine and auto-sort helpers."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from ffn_dl.cli import _apply_library_autosort, _library_subdir_for
from ffn_dl.library.refresh import build_refresh_queue, default_refresh_args
from ffn_dl.library.scanner import scan
from ffn_dl.models import Chapter, Story

from .library_fixtures import (
    bare_txt_no_url,
    ffndl_epub,
)


# ── build_refresh_queue ─────────────────────────────────────────


def _index(tmp_path: Path) -> Path:
    return tmp_path / "idx.json"


def test_build_refresh_queue_empty_library(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    # No scan has been run → index has no entries for this root
    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert queue == []
    assert skipped == []


def test_default_refresh_args_has_every_attribute_download_reads():
    """Regression for "Update failed: 'Namespace' object has no attribute
    'name'". The GUI's Check for Updates button builds this Namespace
    and passes it straight to ``_download_one``, so any attribute
    ``_download_one`` reads unconditionally has to exist here."""
    args = default_refresh_args()
    required_attrs = (
        # Scraper / HTTP tuning (read by _build_scraper)
        "max_retries", "no_cache", "delay_min", "delay_max",
        "chunk_size", "use_wayback",
        # Run orchestration (read by _run_update_queue + _download_one)
        "dry_run", "probe_workers", "format", "output", "chapters",
        # Export path knobs that _download_one dereferences without a
        # getattr fallback — these are what crashed Matt's run.
        "name", "hr_as_stars", "strip_notes",
        # Audio-branch and post-export flags _download_one reads.
        "speech_rate", "attribution", "attribution_model_size",
        "send_to_kindle", "clean_cache",
    )
    missing = [a for a in required_attrs if not hasattr(args, a)]
    assert not missing, f"default_refresh_args missing: {missing}"


def test_build_refresh_queue_from_indexed_library(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="A", url="https://www.fanfiction.net/s/1/1/")
    ffndl_epub(lib, title="B", url="https://archiveofourown.org/works/2")
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert len(queue) == 2
    assert skipped == []
    urls = {entry["url"] for entry in queue}
    # Keys are the canonical URL form — FFN's /1/ suffix is stripped by
    # sites.canonical_url so files carrying different URL shapes of the
    # same story collapse to one entry.
    assert urls == {
        "https://www.fanfiction.net/s/1",
        "https://archiveofourown.org/works/2",
    }
    # Each entry has the shape _run_update_queue needs
    for entry in queue:
        assert "path" in entry and entry["path"].exists()
        assert "rel" in entry and entry["rel"]
        assert "local" in entry and entry["local"] > 0


def test_build_refresh_queue_skips_missing_files(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(lib, title="Vanished")
    scan(lib, index_path=_index(tmp_path))
    path.unlink()

    messages: list[str] = []
    queue, skipped = build_refresh_queue(
        lib, index_path=_index(tmp_path), progress=messages.append,
    )
    assert queue == []
    assert len(skipped) == 1
    assert any("missing on disk" in m for m in messages)


def test_build_refresh_queue_accepts_index_count_for_foreign_format(
    tmp_path: Path,
):
    # A foreign-format file whose chapter files don't match ffn-dl's
    # `chapter_*` convention makes count_chapters return 0. The
    # refresh engine should fall back to the index's recorded count
    # so the story still gets probed.
    from ebooklib import epub

    lib = tmp_path / "lib"
    lib.mkdir()

    book = epub.EpubBook()
    book.set_identifier("foreign-id")
    book.set_title("Foreign Naming")
    book.add_author("Author")
    book.add_metadata("DC", "source", "https://www.royalroad.com/fiction/999")
    book.add_metadata("DC", "subject", "Harry Potter")
    # Chapter file name that count_chapters won't recognise
    ch = epub.EpubHtml(title="Ch 1", file_name="OEBPS/Text/ch001.xhtml")
    ch.content = b"<p>body</p>"
    book.add_item(ch)
    book.toc = [ch]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", ch]
    story_path = lib / "foreign.epub"
    epub.write_epub(str(story_path), book)

    scan(lib, index_path=_index(tmp_path))

    # Patch the index entry's chapter_count to a known positive value
    # so we can assert the fallback picked it up.
    import json
    data = json.loads((_index(tmp_path)).read_text())
    lib_key = next(iter(data["libraries"]))
    for url, entry in data["libraries"][lib_key]["stories"].items():
        entry["chapter_count"] = 3
    (_index(tmp_path)).write_text(json.dumps(data))

    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert len(queue) == 1
    assert queue[0]["local"] == 3


def test_build_refresh_queue_ignores_untrackable_files(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    bare_txt_no_url(lib)  # LOW confidence → lands in untrackable, not stories
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(lib, index_path=_index(tmp_path))
    assert queue == []
    assert skipped == []  # Not even visited — it's not a story


# ── default_refresh_args ─────────────────────────────────────────


def test_default_refresh_args_has_scraper_fields():
    args = default_refresh_args()
    # Every field _build_scraper reads off args needs to be present,
    # otherwise the GUI path will AttributeError at runtime.
    for field in (
        "max_retries",
        "no_cache",
        "delay_min",
        "delay_max",
        "chunk_size",
        "use_wayback",
        "dry_run",
        "skip_complete",
        "probe_workers",
        "format",
        "output",
        "chapters",
        "hr_as_stars",
        "strip_notes",
    ):
        assert hasattr(args, field), field


def test_default_refresh_args_honors_overrides():
    args = default_refresh_args(dry_run=True, skip_complete=True, workers=9)
    assert args.dry_run is True
    assert args.skip_complete is True
    assert args.probe_workers == 9


def test_default_refresh_args_carries_refetch_all():
    """The library GUI's Fresh Copies button sets refetch_all=True.
    ``_download_one`` reads ``args.refetch_all`` to decide whether to
    skip the merge-in-place shortcut, so it has to survive the
    Namespace round-trip without a getattr fallback silently eating it."""
    args = default_refresh_args(refetch_all=True)
    assert args.refetch_all is True

    args_default = default_refresh_args()
    assert args_default.refetch_all is False


# ── Auto-sort ────────────────────────────────────────────────────


def _story(
    fandom: str | None = "Harry Potter",
    *,
    url: str = "https://www.fanfiction.net/s/1/1/",
) -> Story:
    s = Story(
        id=1,
        title="Demo",
        author="A",
        summary="",
        url=url,
    )
    if fandom is not None:
        s.metadata["category"] = fandom
    s.chapters = [Chapter(number=1, title="Ch 1", html="<p>x</p>")]
    return s


def _autosort_args(**overrides) -> Namespace:
    args = Namespace(
        output=None,
        format="epub",
        _library_autosort=True,
        _library_template="{fandom}/{title} - {author}.{ext}",
        _library_misc="Misc",
        _library_original="Original Works",
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def test_library_subdir_none_when_autosort_disabled():
    args = _autosort_args(_library_autosort=False)
    assert _library_subdir_for(_story(), args) is None


def test_library_subdir_uses_fandom():
    subdir = _library_subdir_for(_story("Harry Potter"), _autosort_args())
    assert subdir == Path("Harry Potter")


def test_library_subdir_routes_to_misc_without_fandom():
    subdir = _library_subdir_for(_story(fandom=None), _autosort_args())
    assert subdir == Path("Misc")


def test_library_subdir_splits_comma_separated_fandoms_to_misc():
    # AO3-style "Fandom A, Fandom B" is multi-fandom → Misc
    subdir = _library_subdir_for(
        _story("Harry Potter, The Hobbit"), _autosort_args(),
    )
    assert subdir == Path("Misc")


def test_library_subdir_strips_ffn_breadcrumb_prefix():
    """FFN hands us "Books > Harry Potter" — the user's folder
    should be just "Harry Potter", not "Books _ Harry Potter" with
    the `>` underscored out by the filename sanitiser."""
    subdir = _library_subdir_for(
        _story("Books > Harry Potter"), _autosort_args(),
    )
    assert subdir == Path("Harry Potter")


def test_library_subdir_splits_ao3_crossover_to_misc():
    """AO3 joins crossover fandoms with ` / `. Two distinct fandoms
    means the story belongs in the misc bucket, not a folder named
    "Harry Potter _ Naruto" (the slash-scrubbed single-fandom form
    the old behaviour produced)."""
    subdir = _library_subdir_for(
        _story("Harry Potter / Naruto"), _autosort_args(),
    )
    assert subdir == Path("Misc")


def test_library_subdir_preserves_clean_fandom_name():
    """If a site hands us a plain fandom string with no FFN
    breadcrumb, no AO3 crossover join, and no comma list, it must
    land in a folder with exactly that name. Protects FicWad,
    MediaMiner, and every single-fandom case from bureaucratic
    regressions."""
    subdir = _library_subdir_for(
        _story("The Dresden Files"), _autosort_args(),
    )
    assert subdir == Path("The Dresden Files")


def test_library_subdir_handles_nested_ffn_breadcrumb():
    """A deeper breadcrumb ("Books > Harry Potter > The Marauders")
    should still yield just the leaf — taking the last `>`-segment.
    Unlikely shape in practice but the rsplit handles it for free
    and the test documents the invariant."""
    subdir = _library_subdir_for(
        _story("Books > Harry Potter > The Marauders"), _autosort_args(),
    )
    assert subdir == Path("The Marauders")


def test_library_subdir_combines_breadcrumb_and_ao3_join():
    """A hypothetical site that wrapped an AO3-style crossover in
    an FFN-style breadcrumb should strip the breadcrumb first, then
    recognise the crossover → Misc. The order of the two splits
    matters and this pin guards it."""
    subdir = _library_subdir_for(
        _story("Books > Harry Potter / Naruto"), _autosort_args(),
    )
    assert subdir == Path("Misc")


def test_library_subdir_royal_road_goes_to_original_works():
    """Royal Road is entirely original fiction — "no fandom" there
    is a feature, not missing metadata. The auto-sorter routes RR
    downloads to the Original Works folder rather than Misc so the
    user's library surfaces original novels as their own subtree."""
    rr_story = _story(
        fandom=None,
        url="https://www.royalroad.com/fiction/26727",
    )
    subdir = _library_subdir_for(rr_story, _autosort_args())
    assert subdir == Path("Original Works")


def test_library_subdir_original_folder_honours_override():
    """Matt can rename the bucket via the pref; the default of
    'Original Works' is what the CLI uses when the pref is blank."""
    rr_story = _story(
        fandom=None,
        url="https://www.royalroad.com/fiction/26727",
    )
    args = _autosort_args(_library_original="Web Novels")
    subdir = _library_subdir_for(rr_story, args)
    assert subdir == Path("Web Novels")


def test_library_subdir_royal_road_respects_explicit_category():
    """If a user manually categorises an RR download (pref or
    future metadata injection), honour that instead of forcing the
    original-works bucket. Keeps the one-off override path open."""
    rr_story = _story(
        fandom="Some Franchise",
        url="https://www.royalroad.com/fiction/26727",
    )
    subdir = _library_subdir_for(rr_story, _autosort_args())
    assert subdir == Path("Some Franchise")


def test_library_subdir_non_rr_fanfic_without_fandom_still_misc():
    """Non-original sites with missing metadata stay in misc — the
    original-works routing is strictly for sites whose catalogue is
    entirely original fiction. A FFN story that somehow lost its
    category belongs in misc for manual review, not Original Works."""
    ffn_story = _story(
        fandom=None,
        url="https://www.fanfiction.net/s/1/1/",
    )
    subdir = _library_subdir_for(ffn_story, _autosort_args())
    assert subdir == Path("Misc")


def test_apply_library_autosort_noop_when_output_explicit():
    args = Namespace(output="/some/path")
    _apply_library_autosort(args)
    assert args.output == "/some/path"
    assert not getattr(args, "_library_autosort", False)


def test_apply_library_autosort_noop_when_no_library_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    from ffn_dl import prefs as _prefs

    # Simulate an unset preference by forcing Prefs.get to return "".
    # The DEFAULTS dict doesn't include KEY_LIBRARY_PATH, so a fresh
    # install already returns "" — but we assert it explicitly.
    class _FakePrefs:
        def get(self, key, default=None):
            return ""

    monkeypatch.setattr(_prefs, "Prefs", _FakePrefs)
    args = Namespace(output=None)
    _apply_library_autosort(args)
    assert args.output is None
    assert not getattr(args, "_library_autosort", False)


def test_apply_library_autosort_sets_routing_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from ffn_dl import prefs as _prefs

    lib = tmp_path / "my-library"

    class _FakePrefs:
        def get(self, key, default=None):
            if key == _prefs.KEY_LIBRARY_PATH:
                return str(lib)
            if key == _prefs.KEY_LIBRARY_PATH_TEMPLATE:
                return "{fandom}/{title}.{ext}"
            if key == _prefs.KEY_LIBRARY_MISC_FOLDER:
                return "Other"
            return default

    monkeypatch.setattr(_prefs, "Prefs", _FakePrefs)
    args = Namespace(output=None)
    _apply_library_autosort(args)

    assert args.output == str(lib)
    assert args._library_autosort is True
    assert args._library_template == "{fandom}/{title}.{ext}"
    assert args._library_misc == "Other"


# ── TTL skip ────────────────────────────────────────────────────


def test_build_refresh_queue_ttl_skips_recently_probed(tmp_path: Path):
    """A story whose last_probed stamp is inside the TTL window should
    fall into ``skipped`` rather than the probe queue. The skip message
    explicitly names the time-since-probe so the user can see why."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="Recent", url="https://www.fanfiction.net/s/10/1/")
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    # Stamp the one story we just indexed as "probed 5 minutes ago".
    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, ["https://www.fanfiction.net/s/10"])
    # mark_probed uses _now_iso() — that's "just now", well within a
    # 1-hour TTL window, so the skip path should fire below.

    messages: list[str] = []
    queue, skipped = build_refresh_queue(
        lib,
        index_path=idx_path,
        recheck_interval_s=60 * 60,
        progress=messages.append,
    )
    assert queue == []
    assert len(skipped) == 1
    assert any("ago" in m and "force-recheck" in m for m in messages)


def test_build_refresh_queue_ttl_zero_probes_everything(tmp_path: Path):
    """TTL=0 (the CLI default) preserves the pre-TTL behaviour — every
    indexed story lands in the probe queue regardless of last_probed."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="Freshly probed", url="https://www.fanfiction.net/s/11/1/")
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, ["https://www.fanfiction.net/s/11"])

    queue, skipped = build_refresh_queue(
        lib, index_path=idx_path, recheck_interval_s=0,
    )
    assert len(queue) == 1
    assert skipped == []


def test_build_refresh_queue_ttl_missing_last_probed_is_probed(tmp_path: Path):
    """An indexed story without a last_probed stamp (never probed under
    this build, or only scanned) must fall through to the probe queue
    even with TTL set — we don't want a one-time scan to mask new work
    from the first update-library run."""
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="Unstamped", url="https://www.fanfiction.net/s/12/1/")
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)
    # Deliberately skip mark_probed so last_probed stays absent.

    queue, skipped = build_refresh_queue(
        lib, index_path=idx_path, recheck_interval_s=60 * 60,
    )
    assert len(queue) == 1
    assert skipped == []


def test_build_refresh_queue_uses_mtime_size_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When file mtime+size match the index, build_refresh_queue must
    skip the ebooklib parse and trust the cached chapter_count. This
    is the Phase 1 hot path for big libraries of untouched files."""
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="Cached", url="https://www.fanfiction.net/s/20/1/")
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    # Patch count_chapters to fail loudly — if the cache path doesn't
    # kick in, the test falls through to here and we know immediately.
    import ffn_dl.library.refresh as refresh_mod

    def _exploder(*args, **kwargs):
        raise AssertionError(
            "count_chapters was called on an unchanged file — "
            "mtime/size cache did not kick in"
        )

    monkeypatch.setattr(refresh_mod, "count_chapters", _exploder)

    queue, skipped = build_refresh_queue(lib, index_path=idx_path)
    assert len(queue) == 1
    assert skipped == []
    assert queue[0]["local"] > 0


def test_build_refresh_queue_cache_invalidates_on_file_change(
    tmp_path: Path,
):
    """Mutating the file (mtime+size drift) forces a re-read. Proves
    the cache hasn't locked in a stale count after an external edit."""
    import os

    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Changes", url="https://www.fanfiction.net/s/21/1/",
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    # Bump mtime by 10 seconds and append bytes to shift size — either
    # one alone should bust the cache; doing both keeps the test from
    # being coupled to a single invalidation axis.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    with path.open("ab") as f:
        f.write(b"\x00" * 16)

    calls: list[Path] = []
    import ffn_dl.library.refresh as refresh_mod
    real = refresh_mod.count_chapters

    def _recording(p):
        calls.append(Path(p))
        return real(p)

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(refresh_mod, "count_chapters", _recording)
        build_refresh_queue(lib, index_path=idx_path)
    finally:
        mp.undo()

    assert calls, "count_chapters should have been called after the file changed"


def test_build_refresh_queue_old_index_without_mtime_falls_through(
    tmp_path: Path,
):
    """Entries saved by an older build have no file_mtime/file_size.
    The cache path must fall through to count_chapters for those
    without crashing — users upgrading shouldn't have to re-scan."""
    import json

    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib, title="Legacy", url="https://www.fanfiction.net/s/22/1/",
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    # Simulate an old-build index by stripping the cache fields.
    raw = json.loads(idx_path.read_text(encoding="utf-8"))
    for lib_state in raw["libraries"].values():
        for entry in lib_state["stories"].values():
            entry.pop("file_mtime", None)
            entry.pop("file_size", None)
    idx_path.write_text(json.dumps(raw), encoding="utf-8")

    queue, skipped = build_refresh_queue(lib, index_path=idx_path)
    assert len(queue) == 1
    assert skipped == []


def test_mark_probed_survives_rescan(tmp_path: Path):
    """LibraryIndex.record() preserves last_probed when a rescan rewrites
    the entry. Without this, the post-update rescan after
    --update-library would wipe the stamp we just set, defeating the
    TTL on the very next run."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(lib, title="Sticky", url="https://www.fanfiction.net/s/13/1/")
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(
        lib, ["https://www.fanfiction.net/s/13"],
        timestamp="2026-04-19T12:00:00Z",
    )

    # Simulate the rescan that --update-library runs at the end.
    scan(lib, index_path=idx_path)

    reloaded = LibraryIndex.load(idx_path)
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert entry.get("last_probed") == "2026-04-19T12:00:00Z"


def test_mark_probed_dict_form_stamps_remote_chapter_count(tmp_path: Path):
    """The dict form of mark_probed records remote counts on every
    entry so a later refresh can see remote > local and resume
    without re-probing."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Pending",
        url="https://www.fanfiction.net/s/20/1/",
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/20": 42})

    reloaded = LibraryIndex.load(idx_path)
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert entry.get("remote_chapter_count") == 42
    assert entry.get("last_probed"), "must also stamp last_probed"


def test_mark_probed_none_count_clears_pending(tmp_path: Path):
    """A probe that answered but with no count (StoryNotFoundError)
    must clear any prior remote_chapter_count — otherwise a deleted
    story would stay flagged as "needs update" forever."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Ghost",
        url="https://www.fanfiction.net/s/21/1/",
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/21": 10})
    # Confirm the count was stored
    reloaded = LibraryIndex.load(idx_path)
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert entry.get("remote_chapter_count") == 10

    # Now the story is gone upstream — mark with None.
    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/21": None})
    reloaded = LibraryIndex.load(idx_path)
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert "remote_chapter_count" not in entry


def test_build_refresh_queue_resumes_pending_without_reprobing(tmp_path: Path):
    """Entries with remote_chapter_count > local land in the queue with
    ``remote`` pre-filled — the probe phase sees it and skips the
    upstream call. This is the resume-mid-batch path: an interrupted
    run can finish its downloads on the next invocation without
    re-probing the whole library."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    # File has 3 chapters on disk; remote has 5 (pending update from a
    # previous probe that never got downloaded).
    ffndl_epub(
        lib, title="Pending Update",
        url="https://www.fanfiction.net/s/30/1/",
        chapters=3,
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/30": 5})

    messages: list[str] = []
    queue, skipped = build_refresh_queue(
        lib, index_path=idx_path, progress=messages.append,
    )
    assert len(queue) == 1
    assert queue[0]["local"] == 3
    assert queue[0]["remote"] == 5, "remote must be pre-filled to skip probe"
    assert any("resume" in m for m in messages)


def test_build_refresh_queue_pending_bypasses_ttl(tmp_path: Path):
    """A pending-update entry queues even when the TTL would ordinarily
    skip it — the whole point of the resume path is that the probe
    already happened once, so we don't need to wait for TTL expiry
    to finish the download it surfaced."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Pending Within TTL",
        url="https://www.fanfiction.net/s/31/1/",
        chapters=2,
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    # Probe stamp is "just now" and remote > local — TTL would skip
    # under old rules, but the pending check must fire first.
    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/31": 4})

    queue, skipped = build_refresh_queue(
        lib, index_path=idx_path, recheck_interval_s=60 * 60,
    )
    assert len(queue) == 1
    assert skipped == []
    assert queue[0]["remote"] == 4


def test_build_refresh_queue_pending_resolved_falls_back_to_normal(tmp_path: Path):
    """If remote_chapter_count == local, there's no pending work — the
    entry goes through the normal TTL + probe flow, not the resume
    shortcut."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Resolved",
        url="https://www.fanfiction.net/s/32/1/",
        chapters=3,
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    # remote matches local — no pending work
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/32": 3})

    queue, skipped = build_refresh_queue(
        lib, index_path=idx_path, recheck_interval_s=0,
    )
    # TTL=0 means we'd probe this normally (no pre-filled remote)
    assert len(queue) == 1
    assert "remote" not in queue[0]


def test_index_record_preserves_remote_chapter_count_across_rescan(
    tmp_path: Path,
):
    """rescan_library() must preserve remote_chapter_count on existing
    entries — otherwise the resume-on-next-run path gets defeated by
    the rescan that --update-library runs at the end of every
    library-update pass."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib, title="Keep Remote",
        url="https://www.fanfiction.net/s/33/1/",
    )
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/33": 99})

    # Rescan (as happens after --update-library) must not wipe the count
    scan(lib, index_path=idx_path)

    reloaded = LibraryIndex.load(idx_path)
    [(_url, entry)] = list(reloaded.stories_in(lib))
    assert entry.get("remote_chapter_count") == 99


# ── Stale-complete TTL ──────────────────────────────────────────


def _age_file(path: Path, days: float) -> None:
    """Backdate both access and modification times by ``days`` days so
    the stale-complete gate sees the file as old. Uses the file's own
    current mtime as the anchor so the test is insensitive to wall
    clock variation between the export call and the os.utime call."""
    import os
    current = path.stat().st_mtime
    target = current - days * 86400
    os.utime(path, (target, target))


def test_stale_complete_skips_old_complete_story(tmp_path: Path):
    """A story marked Complete with an mtime older than the threshold
    should be skipped. The skip message names the file age and points
    at --force-recheck so the user knows how to override."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib,
        title="Ancient Complete",
        url="https://www.fanfiction.net/s/40/1/",
        status="Complete",
    )
    _age_file(path, days=400)
    scan(lib, index_path=_index(tmp_path))

    messages: list[str] = []
    queue, skipped = build_refresh_queue(
        lib,
        index_path=_index(tmp_path),
        skip_stale_complete_days=365,
        progress=messages.append,
    )
    assert queue == []
    assert len(skipped) == 1
    assert any("untouched" in m and "force-recheck" in m for m in messages)


def test_stale_complete_keeps_fresh_complete_story(tmp_path: Path):
    """A story that's Complete but was re-exported recently (mtime
    younger than the threshold) must stay in the probe queue — the
    whole point of this gate over --skip-complete is that
    recently-completed fics still get a chance to show an epilogue."""
    lib = tmp_path / "lib"
    lib.mkdir()
    ffndl_epub(
        lib,
        title="Fresh Complete",
        url="https://www.fanfiction.net/s/41/1/",
        status="Complete",
    )
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(
        lib,
        index_path=_index(tmp_path),
        skip_stale_complete_days=365,
    )
    assert len(queue) == 1
    assert skipped == []


def test_stale_complete_keeps_old_in_progress_story(tmp_path: Path):
    """An In-Progress story that happens to be old must still be
    probed — the gate is explicitly restricted to Complete status so
    abandoned WIPs don't get silently dropped from updates."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib,
        title="Old WIP",
        url="https://www.fanfiction.net/s/42/1/",
        status="In-Progress",
    )
    _age_file(path, days=400)
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(
        lib,
        index_path=_index(tmp_path),
        skip_stale_complete_days=365,
    )
    assert len(queue) == 1
    assert skipped == []


def test_stale_complete_bypassed_for_pending_resume(tmp_path: Path):
    """If a previous probe recorded remote > local, the resume shortcut
    must fire even when the file is old and Complete — the download is
    already owed and shouldn't wait another N days to land."""
    from ffn_dl.library.index import LibraryIndex

    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib,
        title="Old Complete With Pending",
        url="https://www.fanfiction.net/s/43/1/",
        status="Complete",
        chapters=3,
    )
    _age_file(path, days=500)
    idx_path = _index(tmp_path)
    scan(lib, index_path=idx_path)

    idx = LibraryIndex.load(idx_path)
    idx.mark_probed(lib, {"https://www.fanfiction.net/s/43": 5})

    queue, skipped = build_refresh_queue(
        lib,
        index_path=idx_path,
        skip_stale_complete_days=365,
    )
    assert len(queue) == 1
    assert skipped == []
    assert queue[0]["remote"] == 5


def test_stale_complete_disabled_by_default(tmp_path: Path):
    """Omitting the flag preserves the pre-gate behaviour — old
    Complete stories still get probed. Protects scripted callers that
    don't opt into the new gate."""
    lib = tmp_path / "lib"
    lib.mkdir()
    path = ffndl_epub(
        lib,
        title="Old Complete (no gate)",
        url="https://www.fanfiction.net/s/44/1/",
        status="Complete",
    )
    _age_file(path, days=400)
    scan(lib, index_path=_index(tmp_path))

    queue, skipped = build_refresh_queue(
        lib,
        index_path=_index(tmp_path),
    )
    assert len(queue) == 1
    assert skipped == []
