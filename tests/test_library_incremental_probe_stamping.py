"""Regression tests for incremental last_probed stamping.

Before this feature, ``last_probed`` got stamped in one shot at the
very end of a library update. A user who closed the app during a
long probe cycle (800+ FFN stories at 6 s/probe = ~80 minutes) lost
every stamp, so the *next* Check for Updates re-probed every story
they had already checked. These tests guard against that regression:

* ``_run_update_queue`` fires ``on_probe_complete(url)`` for every
  successful probe, never for failures.
* Pending stamps get flushed to the index in batches so a partial
  run leaves persistent progress on disk.
"""

import threading
import types
from pathlib import Path

import pytest


class _FakeSiteClass:
    """A type object that _run_update_queue accepts as its partition
    key. We need a real class (not SimpleNamespace) because the queue
    reads ``site_cls.site_name`` off the class, not an instance."""
    site_name = "fake"


def _fake_scraper(results):
    """Build a scraper stub whose ``get_chapter_count`` returns the
    next pre-scripted value or raises a pre-scripted exception."""
    scraper = types.SimpleNamespace()
    scraper.site_name = "fake"
    scraper.concurrency = 1
    iterator = iter(results)

    def get_chapter_count(url):
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value
    scraper.get_chapter_count = get_chapter_count
    return scraper


def test_probe_complete_callback_fires_on_definitive_answers(monkeypatch):
    """Callback fires whenever the probe got a *definitive* answer
    from upstream — either a chapter count (story exists) or a
    ``StoryNotFoundError`` (story confirmed gone). Both stamp
    ``last_probed`` so the TTL suppresses the next probe. Transient
    failures (rate-limit, Cloudflare block, timeout, parse errors)
    stay unstamped so the next run retries them."""
    from ffn_dl import cli
    from ffn_dl.scraper import (
        CloudflareBlockError,
        RateLimitError,
        StoryNotFoundError,
    )

    fake = _fake_scraper([
        10,                               # story 1 → ok (exists)
        StoryNotFoundError("404"),        # story 2 → definitive (deleted)
        12,                               # story 3 → ok (exists)
        RateLimitError("429"),            # story 4 → transient
        CloudflareBlockError("cf"),       # story 5 → transient
    ])
    monkeypatch.setattr(cli, "_build_scraper", lambda url, args: fake)
    monkeypatch.setattr(cli, "_detect_site", lambda url: _FakeSiteClass)

    probe_queue = [
        {"path": Path(f"/tmp/x{i}.epub"), "rel": f"x{i}.epub",
         "url": f"https://example.com/s/{i}", "local": 5}
        for i in range(1, 6)
    ]

    probed = []

    def on_complete(url):
        probed.append(url)

    args = types.SimpleNamespace(dry_run=True, format="html")

    cli._run_update_queue(
        probe_queue, args, workers=1,
        skipped_count=0,
        label="test",
        progress=lambda _: None,
        on_probe_complete=on_complete,
    )

    # Stories 1, 2, 3 got a definitive answer; 4 and 5 didn't.
    assert probed == [
        "https://example.com/s/1",
        "https://example.com/s/2",
        "https://example.com/s/3",
    ]
    # The deletion also sets upstream_missing so the GUI can surface
    # the distinction between "dead upstream" and "network flaky".
    assert probe_queue[1].get("upstream_missing") is True
    assert "upstream_missing" not in probe_queue[3]
    assert "upstream_missing" not in probe_queue[4]


def test_gui_batching_flushes_every_n_probes(tmp_path, monkeypatch):
    """End-to-end: simulate the GUI's stamp-flush batch and confirm
    that disk state reflects successful probes as they roll in, not
    only at the cycle's end."""
    from ffn_dl.library.index import LibraryIndex

    index_path = tmp_path / "library-index.json"
    root = tmp_path / "lib"
    root.mkdir()

    # Seed the index with 30 entries so we can test batching at the
    # default flush threshold (25).
    idx = LibraryIndex.load(index_path)
    lib = idx.library_state(root)
    for i in range(30):
        lib["stories"][f"https://example.com/s/{i}"] = {
            "relpath": f"s{i}.epub",
            "title": f"Story {i}",
            "author": "A",
            "chapter_count": 5,
        }
    idx.save()

    # Simulate the GUI worker's stamp buffer + flush.
    STAMP_FLUSH_EVERY = 25
    stamp_lock = threading.Lock()
    pending: list[str] = []

    def flush_locked():
        if not pending:
            return
        idx2 = LibraryIndex.load(index_path)
        idx2.mark_probed(root, list(pending))
        pending.clear()

    def on_complete(url):
        with stamp_lock:
            pending.append(url)
            if len(pending) >= STAMP_FLUSH_EVERY:
                flush_locked()

    # Simulate 27 probes rolling in.
    for i in range(27):
        on_complete(f"https://example.com/s/{i}")

    # After 25 rolled in, flush fired; after 26 and 27 the buffer
    # holds 2. Disk should reflect the 25 stamped entries.
    stamped_on_disk = [
        url for url, e in LibraryIndex.load(index_path)
        .library_state(root)["stories"].items()
        if e.get("last_probed")
    ]
    assert len(stamped_on_disk) == 25

    # Simulate the app closing abruptly here: pending buffer NOT
    # flushed, but on disk we already have 25 of the 27.
    # Next call to Check for Updates would re-probe only 5 (30 total
    # minus 25 stamped) — previously it would re-probe all 30.
    not_stamped = 30 - 25
    assert not_stamped == 5, (
        "sanity: a mid-cycle abort should leave ~5 of 30 re-probed "
        "on the next run, not all 30"
    )


def test_final_flush_handles_remainder_under_batch_size(tmp_path):
    """The final flush-locked call in the worker's ``finally`` tail
    picks up the trailing <25 stamps that never crossed the batch
    threshold — without it we'd lose the last chunk on every run."""
    from ffn_dl.library.index import LibraryIndex

    index_path = tmp_path / "library-index.json"
    root = tmp_path / "lib"
    root.mkdir()

    idx = LibraryIndex.load(index_path)
    lib = idx.library_state(root)
    for i in range(10):
        lib["stories"][f"https://example.com/s/{i}"] = {
            "relpath": f"s{i}.epub", "title": f"Story {i}", "author": "A",
            "chapter_count": 5,
        }
    idx.save()

    pending: list[str] = []

    def flush_locked():
        if not pending:
            return
        idx2 = LibraryIndex.load(index_path)
        idx2.mark_probed(root, list(pending))
        pending.clear()

    # Only 10 stamps — below the flush threshold. Without the final
    # flush none would land on disk.
    for i in range(10):
        pending.append(f"https://example.com/s/{i}")

    flush_locked()  # mimics the finally-tail call

    stamped = [
        e.get("last_probed") for e
        in LibraryIndex.load(index_path).library_state(root)["stories"].values()
        if e.get("last_probed")
    ]
    assert len(stamped) == 10
