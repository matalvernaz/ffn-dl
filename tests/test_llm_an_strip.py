"""Tests for the LLM author's-note backstop on the export pipeline.

These exercise :func:`ffn_dl.exporters.strip_an_via_llm` end-to-end
with the underlying ``attribution._llm_call`` stubbed out, so we
never make a real network round-trip during testing. The stub lets
us:

* Pin the cache hit / cache miss branching.
* Verify that the runaway threshold triggers the verification round
  instead of just dropping the LLM result.
* Check the user-facing rule the user asked for: when verification
  retracts every flag, the chapter content stays intact rather than
  being declared "worthless" by the first pass alone.
* Confirm graceful degradation when the LLM transport fails — the
  regex-only output is what callers get back, never an exception.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffn_dl import attribution, exporters


# ── Test helpers ──────────────────────────────────────────────────


def _isolate_cache_dir(monkeypatch, tmp_path):
    """Point the LLM cache helper at ``tmp_path`` so each test gets a
    fresh slate. The production helper falls through ``portable``
    detection then uses ``~/.cache/ffn-dl/llm_an``; substituting the
    base directory keeps tests hermetic without monkeypatching the
    portable module."""
    cache_root = tmp_path / "llm_an"

    def fake_cache_path(site_name, story_id):
        cache_root.mkdir(parents=True, exist_ok=True)
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_-]", "_", str(story_id))
        return cache_root / f"{site_name}_{safe}.json"

    monkeypatch.setattr(exporters, "_llm_an_cache_path", fake_cache_path)
    return cache_root


def _stub_llm(monkeypatch, replies):
    """Replace ``attribution._llm_call`` with a stub that returns the
    next reply from ``replies`` (a list of strings) on each call.
    The stub records calls so tests can assert how many round-trips
    happened and which system prompts were used.
    """
    calls: list[dict] = []

    def fake_call(*, provider, model, api_key, endpoint,
                  system_prompt, user_prompt):
        calls.append({
            "provider": provider,
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        })
        if not replies:
            return ""
        return replies.pop(0)

    monkeypatch.setattr(attribution, "_llm_call", fake_call)
    return calls


def _llm_config():
    return {
        "provider": "ollama",
        "model": "llama3.1:8b",
        "api_key": "",
        "endpoint": "",
    }


# ── Cache wiring ──────────────────────────────────────────────────


class TestLlmAnCacheKey:
    def test_key_changes_when_paragraph_text_changes(self):
        a = exporters._llm_an_cache_key(["one", "two"], "model-a")
        b = exporters._llm_an_cache_key(["one", "two changed"], "model-a")
        assert a != b

    def test_key_changes_when_model_changes(self):
        a = exporters._llm_an_cache_key(["one", "two"], "model-a")
        b = exporters._llm_an_cache_key(["one", "two"], "model-b")
        assert a != b, "different models must not share cache entries"

    def test_key_stable_for_same_inputs(self):
        a = exporters._llm_an_cache_key(["x", "y"], "m")
        b = exporters._llm_an_cache_key(["x", "y"], "m")
        assert a == b


# ── Single-pass behaviour ─────────────────────────────────────────


class TestStripAnViaLlmSinglePass:
    """When the first-pass flag rate is below the runaway threshold,
    we trust the result and drop the flagged paragraphs without
    making a second round-trip."""

    def test_drops_flagged_paragraphs(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        # Five paragraphs: one is an A/N (index 4 → "5" in 1-based),
        # the rest are story prose. 1/5 = 20% — below the 40% gate.
        html = (
            "<p>Story paragraph one.</p>"
            "<p>Story paragraph two.</p>"
            "<p>Story paragraph three.</p>"
            "<p>Story paragraph four.</p>"
            "<p>Thanks for reading! Drop a comment if you liked it.</p>"
        )
        calls = _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False,
                        "4": False, "5": True}),
        ])

        out = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=42, chapter_number=1,
        )
        assert "Drop a comment" not in out
        assert "Story paragraph one" in out
        assert "Story paragraph four" in out
        # Only the first-pass call; no verification round.
        assert len(calls) == 1

    def test_emits_outcome_when_paragraphs_stripped(
        self, tmp_path, monkeypatch,
    ):
        """User-visible "stripped X/Y paragraph(s) as A/N" message —
        without it, the GUI status pane only shows "classifying via …"
        and the user can't tell whether anything actually happened."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        html = (
            "<p>Story paragraph one.</p>"
            "<p>Story paragraph two.</p>"
            "<p>Story paragraph three.</p>"
            "<p>A/N: catch you next chapter</p>"
        )
        _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False, "4": True}),
        ])

        captured: list[str] = []
        exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=1, chapter_number=7,
            progress=captured.append,
        )
        outcome = [l for l in captured if "stripped" in l]
        assert outcome, captured
        # Format pin: "stripped 1/4 paragraph(s) as A/N"
        assert "1/4" in outcome[0]
        assert "as A/N" in outcome[0]

    def test_emits_outcome_when_nothing_flagged(
        self, tmp_path, monkeypatch,
    ):
        """Symmetric: a chapter with zero flags must say "no A/N
        paragraphs found" so the user knows the pass ran and decided
        nothing was a note — distinct from "ran but errored"."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        html = (
            "<p>Para one.</p>"
            "<p>Para two.</p>"
            "<p>Para three.</p>"
            "<p>Para four.</p>"
        )
        _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False, "4": False}),
        ])

        captured: list[str] = []
        exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=1, chapter_number=2,
            progress=captured.append,
        )
        assert any("no A/N paragraphs found" in l for l in captured), captured

    def test_outcome_emitted_on_cache_hit_too(
        self, tmp_path, monkeypatch,
    ):
        """Even a cache hit must report the per-chapter outcome — a
        re-export that's all cache-hits shouldn't go silent."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        html = (
            "<p>Para one.</p>"
            "<p>Para two.</p>"
            "<p>Para three.</p>"
            "<p>Para four.</p>"
        )
        # Prime the cache.
        _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False, "4": True}),
        ])
        exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=99, chapter_number=3,
        )
        # Second call: cache hit, no LLM round-trip.
        _stub_llm(monkeypatch, [])  # any call would IndexError
        captured: list[str] = []
        exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=99, chapter_number=3,
            progress=captured.append,
        )
        # Both the "cache hit" line AND the outcome line must appear.
        assert any("cache hit" in l for l in captured), captured
        assert any("stripped" in l for l in captured), captured

    def test_progress_calls_mirror_to_file_logger(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Every line the user sees in the GUI status pane is also
        written to the file logger — without this, a postmortem of
        ffn-dl.log can't tell what the LLM A/N pass actually did."""
        import logging as _logging
        _isolate_cache_dir(monkeypatch, tmp_path)
        html = (
            "<p>One.</p><p>Two.</p><p>Three.</p>"
            "<p>A/N: see you</p>"
        )
        _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False, "4": True}),
        ])
        with caplog.at_level(_logging.INFO, logger="ffn_dl.exporters"):
            exporters.strip_an_via_llm(
                html, llm_config=_llm_config(),
                site_name="ffn", story_id=5, chapter_number=4,
            )
        joined = "\n".join(r.message for r in caplog.records)
        assert "classifying via" in joined
        assert "stripped" in joined


class TestStripAnViaLlmCache:
    def test_cache_miss_then_hit_skips_second_call(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        html = (
            "<p>Para one.</p>"
            "<p>Para two.</p>"
            "<p>Para three.</p>"
            "<p>A/N: thanks for reading!</p>"
        )
        calls = _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False, "4": True}),
        ])

        first = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=99, chapter_number=1,
        )
        second = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=99, chapter_number=1,
        )
        assert first == second
        assert len(calls) == 1, (
            "second invocation must hit cache and not re-call the LLM"
        )

    def test_changed_chapter_invalidates_cache(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        html_v1 = (
            "<p>One.</p>"
            "<p>Two.</p>"
            "<p>Three.</p>"
            "<p>Four.</p>"
        )
        html_v2 = (
            "<p>One.</p>"
            "<p>Two.</p>"
            "<p>Three.</p>"
            "<p>Four — author edited this paragraph.</p>"
        )
        calls = _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False, "4": False}),
            json.dumps({"1": False, "2": False, "3": False, "4": False}),
        ])
        exporters.strip_an_via_llm(
            html_v1, llm_config=_llm_config(),
            site_name="ffn", story_id=100, chapter_number=1,
        )
        exporters.strip_an_via_llm(
            html_v2, llm_config=_llm_config(),
            site_name="ffn", story_id=100, chapter_number=1,
        )
        assert len(calls) == 2, "edited chapter content must re-classify"


# ── Verification round (the user's "extra check" gate) ───────────


class TestStripAnViaLlmVerification:
    """First pass flags > runaway threshold → second pass with stricter
    prompt → only paragraphs surviving both rounds are dropped. This
    is the safety net Matt asked for so the LLM can't declare a
    chapter worthless on a single judgement.
    """

    def test_verification_keeps_chapter_when_second_pass_clears_flags(
        self, tmp_path, monkeypatch,
    ):
        _isolate_cache_dir(monkeypatch, tmp_path)
        # Six paragraphs of story prose. The first-pass stub will
        # hallucinate that 5/6 are A/Ns (83% — well over the 40%
        # gate). The verification round will clear every flag.
        # Expected outcome: chapter content untouched.
        html = (
            "<p>Mira opened the door.</p>"
            "<p>The hallway was lit with flickering torches.</p>"
            "<p>She heard footsteps behind her.</p>"
            "<p>'Who's there?' she called out.</p>"
            "<p>No answer came.</p>"
            "<p>She drew her knife and pressed forward.</p>"
        )
        calls = _stub_llm(monkeypatch, [
            # First pass: five flagged out of six (the runaway case).
            json.dumps({"1": True, "2": True, "3": True,
                        "4": True, "5": True, "6": False}),
            # Verification round: classifier reconsiders, decides
            # none are author's notes after all.
            json.dumps({"1": False, "2": False, "3": False,
                        "4": False, "5": False}),
        ])

        out = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=200, chapter_number=1,
        )

        assert "Mira opened the door" in out
        assert "She drew her knife" in out
        assert out.count("<p>") == 6, (
            "verification cleared every flag — chapter must remain intact"
        )
        assert len(calls) == 2
        # The verification round must use the strict prompt.
        assert calls[1]["system_prompt"] == exporters._LLM_AN_VERIFY_PROMPT
        assert "HIGH CONFIDENCE" in calls[1]["system_prompt"]

    def test_verification_keeps_intersection_when_some_survive(
        self, tmp_path, monkeypatch,
    ):
        _isolate_cache_dir(monkeypatch, tmp_path)
        # First pass flags 3/5 (60%, over threshold). Verification
        # confirms 1 of the 3 — that's the only paragraph dropped.
        html = (
            "<p>Story line one.</p>"
            "<p>Disclaimer: I do not own this fandom.</p>"
            "<p>Story line two.</p>"
            "<p>Story line three.</p>"
            "<p>Story line four.</p>"
        )
        calls = _stub_llm(monkeypatch, [
            # First pass over-flags.
            json.dumps({"1": True, "2": True, "3": True,
                        "4": False, "5": False}),
            # Verification — only the disclaimer-shaped one (which
            # was input #2 in the verify prompt's renumbering, since
            # the verify pass re-numbers within the flagged subset)
            # survives. Paras passed in order [1,2,3] → verify
            # numbers them 1,2,3. We confirm only 2 (the disclaimer).
            json.dumps({"1": False, "2": True, "3": False}),
        ])

        out = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=201, chapter_number=1,
        )
        assert "Disclaimer" not in out
        assert "Story line one" in out
        assert "Story line two" in out
        assert out.count("<p>") == 4
        assert len(calls) == 2


# ── Failure modes ─────────────────────────────────────────────────


class TestStripAnViaLlmFailures:
    def test_no_op_without_llm_config(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls = _stub_llm(monkeypatch, [])
        html = "<p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p>"
        assert exporters.strip_an_via_llm(html, llm_config=None) == html
        assert calls == []

    def test_no_op_for_short_chapter(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        # Below ``_LLM_AN_MIN_PARAGRAPHS`` (4) → skip entirely.
        calls = _stub_llm(monkeypatch, [
            json.dumps({"1": True, "2": True}),  # would be aggressive if used
        ])
        html = "<p>Tiny.</p><p>Chapter.</p>"
        out = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=1, chapter_number=1,
        )
        assert out == html
        assert calls == [], "short chapters must skip the round-trip entirely"

    def test_transport_error_returns_input_unchanged(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)

        def boom(**_kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(attribution, "_llm_call", boom)
        html = "<p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p>"
        # Must not raise — LLM is purely additive on top of regex.
        out = exporters.strip_an_via_llm(
            html, llm_config=_llm_config(),
            site_name="ffn", story_id=1, chapter_number=1,
        )
        assert out == html

    def test_unavailable_endpoint_propagates_for_caller_to_short_circuit(
        self, tmp_path, monkeypatch,
    ):
        """A connection-refused / DNS / timeout failure isn't a
        per-call problem — the endpoint is just down, and every
        subsequent chapter in the same export will hit the same wall.
        ``strip_an_via_llm`` must let :class:`LLMUnavailable`
        propagate so the chapter-loop caller can disable the LLM for
        the rest of the run instead of logging a "connection refused"
        warning once per chapter (the bug the user reported on a
        116-chapter FFN download)."""
        _isolate_cache_dir(monkeypatch, tmp_path)

        def boom(**_kwargs):
            raise attribution.LLMUnavailable("endpoint down")

        monkeypatch.setattr(attribution, "_llm_call", boom)
        html = "<p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p>"
        with pytest.raises(attribution.LLMUnavailable):
            exporters.strip_an_via_llm(
                html, llm_config=_llm_config(),
                site_name="ffn", story_id=1, chapter_number=1,
            )


# ── Plumbing through _prepare_chapter_html ────────────────────────


class TestPrepareChapterHtmlWiring:
    """The export-pipeline entry point calls ``strip_an_via_llm`` only
    when both ``strip_notes`` and ``llm_config`` are set. Without
    that gate a user who hits Strip Author's Notes would silently
    burn LLM tokens whenever the audiobook backend was configured."""

    def test_runs_llm_when_strip_notes_and_llm_config_present(
        self, tmp_path, monkeypatch,
    ):
        _isolate_cache_dir(monkeypatch, tmp_path)
        # Five paragraphs of plain prose with no regex-detectable
        # A/N labels — the LLM is the only path that can flag them.
        # Chapter must have ≥ _LLM_AN_MIN_PARAGRAPHS (4) survivors
        # of the regex pass to trigger the round-trip.
        calls = _stub_llm(monkeypatch, [
            json.dumps({"1": False, "2": False, "3": False,
                        "4": False, "5": True}),
        ])
        html = (
            "<p>One.</p>"
            "<p>Two.</p>"
            "<p>Three.</p>"
            "<p>Four.</p>"
            "<p>Hey readers, hit me up on the discord for spoilers!</p>"
        )
        out = exporters._prepare_chapter_html(
            html, hr_as_stars=False, strip_notes=True,
            llm_config=_llm_config(),
            site_name="ffn", story_id=1, chapter_number=1,
        )
        assert "discord" not in out.lower()
        assert len(calls) == 1

    def test_skips_llm_when_strip_notes_off(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls = _stub_llm(monkeypatch, [
            json.dumps({"1": True, "2": True, "3": True, "4": True}),
        ])
        html = "<p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p>"
        out = exporters._prepare_chapter_html(
            html, hr_as_stars=False, strip_notes=False,
            llm_config=_llm_config(),
            site_name="ffn", story_id=1, chapter_number=1,
        )
        assert out == html
        assert calls == [], (
            "LLM must not run when the Strip Author's Notes toggle is off"
        )

    def test_skips_llm_when_no_config(self, tmp_path, monkeypatch):
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls = _stub_llm(monkeypatch, [])
        html = "<p>One.</p><p>Two.</p><p>Three.</p><p>Four.</p>"
        out = exporters._prepare_chapter_html(
            html, hr_as_stars=False, strip_notes=True, llm_config=None,
        )
        # Regex pass still runs (and finds nothing to strip here).
        assert out == html
        assert calls == []


# ── Connection-refused circuit breaker ────────────────────────────


class TestLlmCallTransportClassification:
    """``_llm_call`` distinguishes "endpoint refused the connection"
    from "endpoint replied with an error": only the former is a
    per-export-fatal failure. HTTPError stays a regular ``RuntimeError``
    so a transient 503 doesn't kill LLM use for the rest of the run."""

    def test_url_error_raises_llm_unavailable(self, monkeypatch):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(attribution.LLMUnavailable):
            attribution._llm_call(
                provider="ollama", model="m", api_key="",
                endpoint="http://127.0.0.1:11434",
                system_prompt="", user_prompt="",
            )

    def test_connection_refused_raises_llm_unavailable(self, monkeypatch):
        # Plain ``ConnectionRefusedError`` (subclass of OSError) — what
        # Python actually raises on Windows when nothing is listening
        # at the configured endpoint.
        def boom(req, timeout=None):
            raise ConnectionRefusedError(
                "[WinError 10061] No connection could be made"
            )

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(attribution.LLMUnavailable):
            attribution._llm_call(
                provider="ollama", model="m", api_key="",
                endpoint="http://127.0.0.1:11434",
                system_prompt="", user_prompt="",
            )

    def test_timeout_raises_llm_unavailable(self, monkeypatch):
        def boom(req, timeout=None):
            raise TimeoutError("read timed out")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(attribution.LLMUnavailable):
            attribution._llm_call(
                provider="ollama", model="m", api_key="",
                endpoint="http://127.0.0.1:11434",
                system_prompt="", user_prompt="",
            )

    def test_http_error_stays_runtime_error_not_unavailable(self, monkeypatch):
        # 503 from a reachable server is per-call; LLM use must NOT be
        # disabled for the whole run on this kind of failure.
        import io as _io
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                url="http://x", code=503, msg="Service Unavailable",
                hdrs=None, fp=_io.BytesIO(b""),
            )

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(RuntimeError) as exc_info:
            attribution._llm_call(
                provider="ollama", model="m", api_key="",
                endpoint="http://127.0.0.1:11434",
                system_prompt="", user_prompt="",
            )
        assert not isinstance(exc_info.value, attribution.LLMUnavailable)


class TestExportLoopCircuitBreaker:
    """End-to-end check on the chapter-loop short-circuit. The bug the
    user reported was a 116-chapter download that called the LLM 116
    times against an offline endpoint — once per chapter — logging a
    duplicate connection-refused warning each time. The fix lets
    ``LLMUnavailable`` propagate from ``strip_an_via_llm`` and has
    each exporter catch it once, then skip the LLM pass for the
    remaining chapters in that download."""

    def _multi_chapter_story(self, n: int):
        from ffn_dl.models import Chapter, Story
        s = Story(
            id=1,
            title="T",
            author="A",
            summary="",
            url="https://www.fanfiction.net/s/1",
        )
        for i in range(1, n + 1):
            s.chapters.append(Chapter(
                number=i,
                title=f"Ch {i}",
                # ≥ _LLM_AN_MIN_PARAGRAPHS (4) so the LLM round-trip
                # would actually fire in the absence of the breaker.
                html=(
                    "<p>Para one of chapter.</p>"
                    "<p>Para two of chapter.</p>"
                    "<p>Para three of chapter.</p>"
                    "<p>Para four of chapter.</p>"
                ),
            ))
        return s

    def test_export_html_calls_llm_once_when_endpoint_down(
        self, tmp_path, monkeypatch,
    ):
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls: list = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMUnavailable("endpoint down")

        monkeypatch.setattr(attribution, "_llm_call", boom)

        story = self._multi_chapter_story(5)
        progress_lines: list[str] = []
        exporters.export_html(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
            progress=progress_lines.append,
        )
        assert len(calls) == 1, (
            "After the first LLMUnavailable the chapter loop must "
            "stop calling the LLM for remaining chapters"
        )
        # The "skipping LLM for remaining chapters" notice fires once
        # so the user sees one line of context, not 5×.
        unreachable_lines = [
            l for l in progress_lines if "endpoint unreachable" in l
        ]
        assert len(unreachable_lines) == 1

    def test_export_txt_calls_llm_once_when_endpoint_down(
        self, tmp_path, monkeypatch,
    ):
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls: list = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMUnavailable("endpoint down")

        monkeypatch.setattr(attribution, "_llm_call", boom)

        story = self._multi_chapter_story(5)
        exporters.export_txt(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
        )
        assert len(calls) == 1

    def test_export_html_succeeds_with_llm_disabled_after_failure(
        self, tmp_path, monkeypatch,
    ):
        """Hitting LLMUnavailable must not abort the export — the
        chapter content (after the regex pass) still has to land in
        the file."""
        _isolate_cache_dir(monkeypatch, tmp_path)

        def boom(**_kwargs):
            raise attribution.LLMUnavailable("endpoint down")

        monkeypatch.setattr(attribution, "_llm_call", boom)
        story = self._multi_chapter_story(3)
        path = exporters.export_html(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
        )
        body = path.read_text(encoding="utf-8")
        # All three chapters' content survives.
        assert body.count("Para one of chapter") == 3
