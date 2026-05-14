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


# ── 2.4.8 multi-AI review fixes ───────────────────────────────────


def test_search_darkwanderer_query_preserves_unicode_via_nfkd(monkeypatch):
    """DarkWanderer's keyword field accepts only ASCII. NFKD-fold an
    accented query so "café" becomes "cafe" rather than getting
    silently stripped to "caf+". Mirrors the long-standing fix in
    search_fictionmania."""
    captured: dict = {}

    def fake_fetch(url):
        captured["url"] = url
        return ""  # empty body → empty result list

    monkeypatch.setattr(erotica_search, "_fetch", fake_fetch)
    erotica_search.search_darkwanderer("café déjà vu")
    url = captured["url"]
    assert "cafe" in url
    assert "deja" in url
    # Original characters with diacritics gone; query not collapsed
    # to an empty/single "+".
    assert "caf+" not in url.replace("cafe", "")
    assert "keywords=cafe" in url or "keywords=cafe+deja+vu" in url


def test_parse_an_response_treats_string_false_as_negative():
    """``{"1": "false"}`` must NOT flag paragraph 1 as an A/N. The
    earlier code used a bare ``bool(value)`` which silently treated
    the string ``"false"`` as truthy (non-empty), so a model that
    politely answered "this paragraph is NOT an author note" had
    its content destructively stripped."""
    paragraphs = ["body", "real A/N text", "more body"]
    parsed = {"1": "false", "2": "true", "3": "no"}
    flagged = attribution._parse_an_response(parsed, paragraphs)
    # Only paragraph 2 (1-based "2") should be flagged.
    assert flagged == {1}


def test_parse_an_response_keeps_real_booleans_working():
    """The new string-aware truthiness must not regress the
    documented JSON-bool path."""
    paragraphs = ["a", "b", "c"]
    flagged = attribution._parse_an_response(
        {"1": True, "2": False, "3": True}, paragraphs,
    )
    assert flagged == {0, 2}


def test_combine_rate_clamps_to_provider_safe_range():
    """User -100% combined with sad emotion -20% would emit -120%,
    which edge-tts silently rejects. Result must clamp into the
    provider-safe range [-95, +100]."""
    from ffn_dl.tts import _combine_rate

    # Negative overshoot
    assert _combine_rate(-100, "-20%") == "-95%"
    # Positive overshoot
    assert _combine_rate(100, "+15%") == "+100%"
    # Within range — passthrough
    assert _combine_rate(10, "+5%") == "+15%"
    # Zero result returns None (no rate override)
    assert _combine_rate(10, "-10%") is None


def test_voice_mapper_pool_keyed_indices_avoid_collisions(tmp_path):
    """Two characters sharing the same pool must get distinct voices.

    Regression: an earlier shape used per-character indices that all
    started at 0, so every character with the same locale/gender
    filter collapsed onto pool[0]. Fix is to round-robin by pool
    identity instead of per-name.
    """
    from ffn_dl.tts import VoiceMapper

    mapper = VoiceMapper(map_path=tmp_path / "voicemap.json")
    shared_pool = ["edge:en-GB-RyanNeural", "edge:en-GB-ThomasNeural"]
    mapper.set_voice_pool({
        "Harry": list(shared_pool),
        "Ron": list(shared_pool),
        "Sirius": list(shared_pool),
    })
    voices = {
        "Harry": mapper.assign("Harry"),
        "Ron": mapper.assign("Ron"),
    }
    # First two assignments must be distinct — they share the pool
    # but the pool cursor advances across characters.
    assert voices["Harry"] != voices["Ron"]


def test_check_ffmpeg_also_verifies_ffprobe(monkeypatch):
    """Audiobook mux step calls ffprobe; an earlier shape only checked
    ffmpeg here so ffprobe-missing systems failed at the very end of
    a multi-hour render."""
    from ffn_dl import tts

    calls: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd[0])
        if cmd[0].endswith("ffprobe") or cmd[0] == "ffprobe":
            raise FileNotFoundError("ffprobe missing")
        # ffmpeg path returns successfully
        class _OK:
            returncode = 0
        return _OK()

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as excinfo:
        tts._check_ffmpeg()
    assert "ffprobe" in str(excinfo.value)
    assert "ffprobe" in calls or any("ffprobe" in c for c in calls)


def test_llm_strip_an_paragraphs_falls_back_when_llm_unavailable(monkeypatch):
    """If the LLM endpoint is unreachable, audiobook generation must
    keep going with regex-only A/N stripping rather than aborting
    the whole render."""
    from ffn_dl import tts

    text = "Para one.\n\nPara two.\n\nPara three."

    def fake_classify(*_args, **_kwargs):
        raise attribution.LLMUnavailable("endpoint unreachable")

    monkeypatch.setattr(
        attribution, "classify_authors_notes_via_llm", fake_classify,
    )
    out = tts._llm_strip_an_paragraphs(
        text, llm_config={"provider": "ollama", "model": "x"},
    )
    # Returns the input unchanged — regex pass already ran upstream.
    assert out == text


def test_download_queue_cancel_site_drops_pending(monkeypatch):
    """cancel_site must cancel queued (not-yet-running) jobs for the
    named site without touching other sites."""
    import threading

    from ffn_dl.download_queue import DownloadQueues

    # Reset registry so other tests' queues don't leak into this one.
    DownloadQueues._queues.clear()

    block = threading.Event()
    started = threading.Event()

    def slow_job():
        started.set()
        block.wait(timeout=5)
        return "done"

    def quick_job():
        return "ran"

    # First job claims the worker so subsequent enqueues sit pending.
    DownloadQueues.enqueue("ffn", slow_job)
    started.wait(timeout=2)
    f2 = DownloadQueues.enqueue("ffn", quick_job)
    f3 = DownloadQueues.enqueue("ffn", quick_job)
    other = DownloadQueues.enqueue("ao3", quick_job)

    cancelled = DownloadQueues.cancel_site("ffn")
    # Two queued ffn jobs (f2, f3) cancelled; the running one keeps going.
    assert cancelled == 2
    assert f2.cancelled()
    assert f3.cancelled()
    # ao3 untouched — its queue still drains.
    assert other.result(timeout=2) == "ran"

    block.set()  # release the slow ffn job


def test_llm_quotes_batch_loops_within_window():
    """In a dialogue-dense window (>40 quotes), every quote must be
    attributed — not just the first 40. Regression: an earlier shape
    capped batch building at _LLM_QUOTES_PER_REQUEST per window and
    advanced past the rest."""
    from types import SimpleNamespace

    from ffn_dl import attribution as attr

    # Construct a body with 60 quoted segments concatenated in one
    # window-sized block so all 60 midpoints fall inside the first
    # chunk window. ``Segment`` here mimics the dataclass attribution
    # writes back onto.
    quotes = [f'"q{i:02d}"' for i in range(60)]
    full_text = " ".join(quotes)

    segments = [
        SimpleNamespace(
            text=q, speaker=None, emotion=None,
        ) for q in quotes
    ]

    call_count = {"n": 0}
    seen_quote_numbers: set[int] = set()

    def fake_llm_call(*, system_prompt, user_prompt, **_):
        call_count["n"] += 1
        # Each prompt contains "1. ...\n2. ...\n..." — extract count.
        import re as _re
        nums = _re.findall(r"^(\d+)\. ", user_prompt, _re.M)
        for n in nums:
            seen_quote_numbers.add(int(n))
        # Return a JSON map with every quote attributed to "Narrator".
        body = ", ".join(
            f'"{n}": {{"speaker": "Narrator", "emotion": "neutral"}}'
            for n in nums
        )
        return "{" + body + "}"

    # Patch the inner LLM call only.
    import ffn_dl.attribution as nm

    real_call = nm._llm_call
    real_canon = nm._llm_canonicalise_name
    real_emotion = nm._llm_normalise_emotion
    nm._llm_call = fake_llm_call
    nm._llm_canonicalise_name = lambda raw, *_a, **_kw: raw
    nm._llm_normalise_emotion = lambda x: x
    try:
        nm._refine_with_llm(
            segments, full_text,
            provider="ollama", model="test", endpoint="http://localhost",
        )
    finally:
        nm._llm_call = real_call
        nm._llm_canonicalise_name = real_canon
        nm._llm_normalise_emotion = real_emotion

    # All 60 quotes should have been labelled — the within-window
    # batching loop runs ceil(60/40) = 2 batches, not 1.
    assert call_count["n"] >= 2
    assigned = sum(1 for s in segments if s.speaker == "Narrator")
    assert assigned == 60


# ── cli._read_batch_file BOM handling ─────────────────────────────


def test_read_batch_file_strips_utf8_bom(tmp_path):
    """A batch file saved by Windows Notepad as ``UTF-8`` starts with
    the ``\\ufeff`` BOM. With plain ``encoding=\"utf-8\"`` that BOM
    landed at the head of the first URL and the fetch failed with an
    opaque "invalid URL" message that hid the invisible character.
    ``utf-8-sig`` consumes the BOM cleanly."""
    from ffn_dl.cli import _read_batch_file

    batch = tmp_path / "urls.txt"
    batch.write_bytes(
        b"\xef\xbb\xbfhttps://example.com/s/1\n"
        b"https://example.com/s/2\n"
    )
    urls = _read_batch_file(str(batch))
    assert urls == [
        "https://example.com/s/1",
        "https://example.com/s/2",
    ]
