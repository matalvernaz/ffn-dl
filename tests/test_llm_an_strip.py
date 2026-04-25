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
