"""Regression tests for the 2.3.3 bug-sweep fixes.

Each test pins a specific behaviour change so a future refactor that
silently undoes one of these guards trips a clear failure with the
same vocabulary the changelog uses, rather than a one-line diff with
no breadcrumbs.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from ffn_dl import attribution, search
from ffn_dl.erotica import search as erotica_search
from ffn_dl.library import index as library_index
from ffn_dl.tts_providers import piper as piper_provider


# ── attribution._llm_call response handling ────────────────────────


def test_llm_response_non_json_returns_empty(monkeypatch, caplog):
    """A truncated stream / proxy-injected HTML page would land here
    as an unparseable string. The pipeline must treat it as "empty
    response" rather than crash with a ``ValueError`` propagating up
    to the chapter-by-chapter loop."""
    parsed = attribution._extract_llm_text("not-json-at-all", "ollama")
    assert parsed == ""


def test_llm_response_array_returns_empty():
    """Some providers' error envelopes return a JSON array (e.g.
    ``["error", ...]``); calling ``.get`` on a list crashes. The
    helper must defend against any non-dict shape."""
    assert attribution._extract_llm_text("[1, 2, 3]", "anthropic") == ""


def test_llm_response_anthropic_wrong_content_type():
    """If Anthropic returns ``content`` as a string (a known shape on
    error responses), iterating with ``for block in content`` would
    yield characters and crash on ``.get``."""
    body = '{"content": "an error message instead of blocks"}'
    assert attribution._extract_llm_text(body, "anthropic") == ""


def test_llm_response_openai_choices_with_null_first():
    """Defensive: a malformed OpenAI response with ``choices[0]``
    being null (some self-hosted gateways do this on rate-limit)
    must not blow up."""
    body = '{"choices": [null]}'
    assert attribution._extract_llm_text(body, "openai") == ""


def test_llm_response_ollama_happy_path():
    """Sanity: the guards mustn't have broken the normal path."""
    body = '{"message": {"content": "hello"}}'
    assert attribution._extract_llm_text(body, "ollama") == "hello"


# ── search.fetch_until_limit pagination cap ────────────────────────


def test_fetch_until_limit_breaks_on_repeated_page():
    """A site that returns the exact same rows on every page (CDN
    caching the wrong query, server bug) must not pin the worker
    thread forever — the helper detects identical signatures across
    consecutive pages and bails."""
    same_page = [{"url": "/s/1", "title": "A"}, {"url": "/s/2", "title": "B"}]
    calls = {"n": 0}

    def fake_search(query, *, page, **_):
        calls["n"] += 1
        return list(same_page)

    results, next_page = search.fetch_until_limit(
        fake_search, "x", limit=1000,
    )
    # First page collects 2 results. Second page returns identical
    # signature, so the loop breaks before the third call.
    assert calls["n"] == 2
    assert len(results) == 2


def test_fetch_until_limit_respects_max_pages():
    """A site that returns one fresh row per page indefinitely would
    otherwise let the helper run out to ``limit`` even when ``limit``
    is unreasonably large. Cap at ``_FETCH_UNTIL_LIMIT_MAX_PAGES``."""
    calls = {"n": 0}

    def fake_search(query, *, page, **_):
        calls["n"] += 1
        # New row every page — never repeats, never empty.
        return [{"url": f"/s/{page}", "title": f"Story {page}"}]

    results, _ = search.fetch_until_limit(
        fake_search, "x", limit=10**6,
    )
    assert calls["n"] <= search._FETCH_UNTIL_LIMIT_MAX_PAGES + 1


# ── erotica.search_fictionmania unicode handling ───────────────────


def test_fictionmania_query_preserves_unicode_via_nfkd(monkeypatch):
    """The earlier regex stripped accents entirely, turning "café"
    into "caf" and "résumé" into "rsum". The fix folds via NFKD so
    accented letters degrade to their ASCII base before the strip."""
    captured = {}

    def fake_fetch(url, *args, **kwargs):
        captured["url"] = url
        return ""

    monkeypatch.setattr(erotica_search, "_fetch", fake_fetch)
    erotica_search.search_fictionmania("café résumé")
    assert "cafe" in captured["url"].lower()
    assert "resume" in captured["url"].lower()


# ── piper archive-member validation ────────────────────────────────


def test_piper_archive_safety_passes_normal_members(tmp_path):
    """Sanity: a well-formed archive must not be rejected."""
    piper_provider._assert_safe_archive_members(
        ["piper", "espeak-ng-data/foo", "lib/libpiper.so"],
        tmp_path,
    )


def test_piper_archive_safety_rejects_traversal(tmp_path):
    """A tampered archive with a ``../`` payload must raise rather
    than write outside the install directory."""
    with pytest.raises(RuntimeError, match="outside"):
        piper_provider._assert_safe_archive_members(
            ["../../etc/evil"],
            tmp_path,
        )


def test_piper_archive_safety_rejects_absolute_path(tmp_path):
    """Tar archives can carry absolute member names. ``Path(base) /
    "/x"`` silently returns ``Path("/x")`` — the guard rejects the
    member up-front rather than letting the silent strip turn the
    attack into a "looks safe" path."""
    with pytest.raises(RuntimeError, match="absolute path"):
        piper_provider._assert_safe_archive_members(
            ["/etc/passwd"],
            tmp_path,
        )


def test_piper_archive_safety_real_zip_path_traversal(tmp_path):
    """End-to-end with a real zipfile: a synthetic archive that
    contains ``../escape.txt`` must trip the guard before any data
    is written."""
    archive = tmp_path / "evil.zip"
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", b"would land in tmp_path, not install_dir")
    with zipfile.ZipFile(archive) as zf:
        names = [info.filename for info in zf.infolist()]
        with pytest.raises(RuntimeError):
            piper_provider._assert_safe_archive_members(names, install_dir)


# ── library/index.py untrackable dedup ─────────────────────────────


def test_untrackable_does_not_duplicate_on_rescan(tmp_path):
    """Re-running a scan over the same library shouldn't pile a
    duplicate untrackable entry every pass. The fix updates the
    existing entry in place instead of appending."""
    from ffn_dl.library.candidate import StoryCandidate, Confidence
    from ffn_dl.updater import FileMetadata

    root = tmp_path
    bad_file = root / "garbage.html"
    bad_file.write_text("<html>not really fanfic</html>")

    md = FileMetadata(format="html", title="garbage", author="?")
    cand = StoryCandidate(
        path=bad_file, metadata=md, confidence=Confidence.LOW,
        notes=["no source URL"],
    )

    idx = library_index.LibraryIndex(tmp_path / "idx.json", library_index._empty())
    idx.record(root, cand)
    idx.record(root, cand)
    idx.record(root, cand)
    state = idx.library_state(root)
    assert len(state["untrackable"]) == 1


# ── library/reorganizer escape-the-root guard ──────────────────────


def test_reorganizer_skips_relpath_traversal(tmp_path, caplog):
    """A poisoned index entry with relpath="../system" must not
    produce a move op — the resolver would otherwise have us
    moving system files into the library."""
    from ffn_dl.library.reorganizer import plan
    from ffn_dl.library.index import LibraryIndex, _empty

    root = tmp_path / "library"
    root.mkdir()
    idx_path = tmp_path / "idx.json"

    # Hand-craft an index with a malicious relpath. We bypass record()
    # so we can write the value the validator at scan-time would
    # never produce.
    raw = _empty()
    raw["libraries"][str(root.resolve())] = {
        "last_scan": None,
        "stories": {
            "https://www.fanfiction.net/s/9999": {
                "relpath": "../etc/passwd",
                "title": "x",
                "author": "y",
                "format": "html",
                "adapter": "ffn",
                "confidence": "high",
            },
        },
        "untrackable": [],
    }
    idx_path.write_text(__import__("json").dumps(raw))

    moves = plan(root=root, index_path=idx_path)
    assert moves == []
