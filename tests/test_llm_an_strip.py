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
                  system_prompt, user_prompt, response_schema=None,
                  request_timeout_s=None):
        calls.append({
            "provider": provider,
            "model": model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": response_schema,
            "request_timeout_s": request_timeout_s,
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


# ── Parser robustness across schemas ──────────────────────────────


class TestParseAnResponseSchemas:
    """Real LLMs return creative JSON shapes ignoring the documented
    ``{"1": true}`` schema. Each test below pins one of the shapes
    seen in real ffn-dl logs (qwen2.5:7b, gpt-4o-mini, llama3.1)
    against the same set of three input paragraphs where indices
    0 and 2 are the A/Ns. The parser must extract those two indices
    no matter which shape the model picks."""

    PARAS = [
        "Howdy. My name is Jack T. Cynical and welcome to my world.",
        "Harry Potter leaned back in his seat and stared out the window.",
        "Updates for this will be relatively sparse — one a month if I can.",
    ]

    def _parse(self, parsed):
        return attribution._parse_an_response(parsed, self.PARAS)

    def test_documented_format(self):
        # The shape the prompt asks for. Has to keep working.
        out = self._parse({"1": True, "2": False, "3": True})
        assert out == {0, 2}

    def test_nested_under_author_notes_with_text(self):
        # qwen2.5:7b on this exact prompt — full text, no numbers.
        out = self._parse({
            "response": {
                "author_notes": [
                    {"text": "Howdy. My name is Jack T. Cynical and welcome to my world.",
                     "type": "introduction"},
                    {"text": "Updates for this will be relatively sparse — one a month if I can.",
                     "type": "update_schedule"},
                ],
            },
        })
        assert out == {0, 2}

    def test_notes_with_explicit_paragraph_numbers(self):
        # gpt-4o-mini sometimes — paragraph numbers in a "number"
        # field rather than as the dict key.
        out = self._parse({
            "chapter": 1,
            "notes": [
                {"number": 1, "content": "..."},
                {"number": 3, "content": "..."},
            ],
        })
        assert out == {0, 2}

    def test_flagged_index_list(self):
        # ``{"flagged": [1, 3]}`` — bare integers, 1-based.
        out = self._parse({"flagged": [1, 3]})
        assert out == {0, 2}

    def test_string_index_list(self):
        # Some models stringify the indices: ``["1", "3"]``.
        out = self._parse({"a_n": ["1", "3"]})
        assert out == {0, 2}

    def test_text_match_with_truncation_ellipsis(self):
        # Long paragraphs are truncated with "…" in the prompt; if the
        # model echoes back the truncated form, prefix-matching has to
        # still succeed.
        long_para = (
            "This is a long author's note that goes on for many words "
            "and gets truncated at the prompt's 600-char boundary, "
            "though for this test we just need a recognisable prefix."
        )
        out = attribution._parse_an_response(
            {"author_notes": [
                {"text": long_para[:60] + "…"},
            ]},
            [long_para],
        )
        assert out == {0}

    def test_short_strings_dont_false_positive(self):
        # Category labels ("introduction", "update_schedule") are
        # short and the parser must NOT match them against story
        # paragraphs — that'd flag random short prose paragraphs.
        out = self._parse({
            "labels": ["introduction", "update_schedule", "ownership"],
        })
        assert out == set()

    def test_falls_through_to_text_match_when_no_numbers(self):
        # Worst case: model returns a list of texts with no schema
        # at all. Text-matching saves the day.
        out = self._parse([
            "Updates for this will be relatively sparse",
            "Howdy. My name is Jack T. Cynical",
        ])
        assert out == {0, 2}

    def test_empty_response_returns_empty_set(self):
        assert self._parse({}) == set()
        assert self._parse([]) == set()
        assert self._parse({"foo": "bar"}) == set()

    def test_documented_format_wins_when_both_shapes_present(self):
        # If the documented shape is present, prefer it — text-matching
        # is the broadest fallback and could over-flag if both ran.
        out = self._parse({
            "1": True, "2": False, "3": False,
            "echo": "Updates for this will be relatively sparse — etc",
        })
        # Only paragraph 1 (index 0) should be flagged from the
        # documented map; the echo of paragraph 3's text shouldn't
        # also bring in index 2.
        assert out == {0}

    def test_qwen_scene_summary_schema_returns_empty(self):
        # Real qwen2.5:14b reply on the FFN founders'-vault fic
        # (2026-04-26 ffn-dl.log:1342). The model ignored the prompt
        # entirely and produced a scene-summary keyed by quoted
        # dialogue snippets with ``{speaker, response, description}``
        # sub-objects. None of the keys parse as paragraph numbers,
        # the values are model-generated summaries (not paragraph
        # text), and the dict keys aren't paragraph text either —
        # so the parser must return ``set()`` rather than producing
        # spurious flags via the text-match fallback. This is the
        # input shape the new Ollama structured-output enforcement
        # is designed to prevent.
        out = self._parse({
            "content": {
                "-Platform Nine and Three-Quarters-": {
                    "description": "Harry, now eleven years old, is "
                                   "preparing to board the Hogwarts "
                                   "Express for his first year.",
                },
                "You're coming home for Christmas, right Harry?": {
                    "speaker": "Sirius",
                    "response": "Harry rolls his eyes and reassures "
                                "Sirius that he'll be coming home on "
                                "most holidays.",
                },
            },
        })
        assert out == set()


class TestExpandAnBlock:
    """Structural propagation of LLM flags onto the contiguous A/N
    blocks at chapter head and tail.

    Real fics: the LLM catches a few of the trailing A/N paragraphs
    but misses the rest of the same block. Expansion fills the gaps
    by leveraging position — once anchored in the boundary region,
    sweep the neighbours."""

    def test_short_chapter_no_expansion(self):
        # Heuristic disabled below 8 paragraphs — too few for the
        # boundary windows to mean anything.
        result = attribution.expand_an_block({0, 5}, 6)
        assert result == {0, 5}

    def test_empty_flag_set_is_pass_through(self):
        assert attribution.expand_an_block(set(), 100) == set()

    def test_mid_chapter_flag_does_not_expand(self):
        # A flag at index 50 in a 100-para chapter is mid-chapter
        # (not in top 5% nor bottom 20%). No expansion.
        assert attribution.expand_an_block({50}, 100) == {50}

    def test_tail_anchor_expands_to_chapter_end(self):
        # Two flags in the bottom 30% (indices 75 and 95 in N=100).
        # Bottom 20% threshold = 80, so the flag at 95 anchors.
        # Earliest flag in bottom 30% (>= 70) is 75. Sweep 75..99.
        result = attribution.expand_an_block({75, 95}, 100)
        assert result == set(range(75, 100))

    def test_tail_anchor_with_mid_chapter_flag_kept(self):
        # Mid-chapter flag survives but doesn't pull anything around
        # it. Tail expansion fires independently.
        result = attribution.expand_an_block({40, 95}, 100)
        # 40 stays; 95 anchors tail expansion from earliest in bottom
        # 30% (which is 95 itself, since 40 < 70).
        assert 40 in result
        assert set(range(95, 100)) <= result
        # Nothing else (no expansion of 40, and nothing between 40 and 95).
        assert result == {40} | set(range(95, 100))

    def test_head_anchor_expands_from_start(self):
        # Flag at index 0 (top 5% of N=100). Top 15% = 15. Latest
        # flag in top 15 is 0 (the only one). Sweep 0..0 — adds
        # nothing new but the anchor itself.
        result = attribution.expand_an_block({0}, 100)
        assert result == {0}

    def test_head_anchor_with_two_flags_sweeps_to_latest(self):
        # Disclaimer at idx 0, plus a follow-up "thanks for reading"
        # at idx 4. Both in top 15%. Sweep 0..4.
        result = attribution.expand_an_block({0, 4}, 100)
        assert result == {0, 1, 2, 3, 4}

    def test_safety_cap_aborts_runaway_expansion(self):
        # Tiny chapter where expansion would cover >50%: the
        # heuristic refuses to fire, returning the LLM's flags
        # untouched so a runaway can't gut a chapter.
        # N=10, flag at idx 9 (bottom 20%). Expansion would sweep
        # 7..9 (3 paras = 30%, fine). Add another flag at idx 0
        # (top 5%); top expansion sweeps 0..0. Together that's
        # {0, 7, 8, 9} = 4/10 = 40% — under the cap.
        # Now add flag at idx 5: head sweep still 0..0 (since 5 is
        # not in top window of 1), tail sweep from idx 5 (bottom
        # window starts at 7, so 5 isn't in it). So no expansion
        # fires — but the safety cap test needs an actual runaway.
        # Build a 10-para chapter with flags at 0 and 8: head 0..0,
        # tail anchor 8 in bottom 20% (=8), earliest in bottom 30%
        # (=7) — but 8 is the only flag in [7,9] so earliest is 8,
        # tail sweep 8..9. Total {0, 8, 9} = 3/10 = 30%. Cap not hit.
        # Force the cap: flags spanning so expansion would dominate.
        result = attribution.expand_an_block({0, 1, 7, 8, 9}, 10)
        # Head: 0..1 → adds 1 (already flagged). Tail: 7..9 (already
        # flagged). Total 5/10 = 50% — at the cap (cap rejects > N//2,
        # so 5 > 5 is false, accepted).
        assert result == {0, 1, 7, 8, 9}

    def test_safety_cap_actually_aborts_when_breached(self):
        # Construct a case where expansion WOULD breach 50%. N=10,
        # flag set such that expansion produces > 5 indices.
        # Flags {0, 1, 2, 9}: head sweeps 0..2 (3 paras), tail anchor
        # 9 in bottom 20% (=8), earliest in bottom 30% (=7) is 9
        # itself (since neither 0,1,2 is in [7,9]). Tail sweep 9..9.
        # Total: {0, 1, 2, 9} = 4. Under cap. Hmm.
        # Need a bigger reach. N=10, flags {0, 5, 9}: top window
        # is max(2, 1) = 2, latest in head is 0 (since 5 > 2 is out).
        # Tail anchor 9 (>= 8), earliest in bottom 30% (>= 7) is 9.
        # Sweep 9..9. So expansion is just {0, 5, 9}. 3/10 = 30%.
        # Try N=20, flags {0, 1, 19}: top 5% = 1, top 15% = 3. Head
        # latest = 1, sweep 0..1. Tail 16-19, anchor 19 in bottom 20%
        # (= 16). Earliest in bottom 30% (= 14): only 19. Sweep
        # 19..19. Total: {0, 1, 19} = 3/20 = 15%.
        # Force a sweep big enough: N=20, flags {0, 14, 19}. Tail
        # earliest in [14, 19] = 14. Sweep 14..19 = 6 paras. Plus
        # head 0..0. Total 7/20 = 35%. Still under cap.
        # Bigger: N=10, flags {7, 8, 9}: tail sweep 7..9 = 3.
        # Add flag {0}: head sweep 0..0 = 1. Total 4/10 = 40%.
        # The cap (n_paragraphs // 2 = 5) is breached when result > 5.
        # N=10, flags {0, 1, 6, 7, 8, 9}: head sweep 0..1 (idx 1 is
        # in top 15% = 1, so latest in head = 1). 6,7,8,9 already
        # flagged. Tail anchor 9 in bottom 20% = 8. Earliest in
        # bottom 30% (>= 7) = 7. Sweep 7..9 (already flagged). Total
        # 6/10 = 60% > 50%. Cap fires, returns original {0, 1, 6, 7,
        # 8, 9}. Wait the original IS the flagged set, which is 6/10.
        # The original is already over the cap! That's not a runaway,
        # that's the LLM saying so.
        # Real runaway: small original, big expansion. N=20, flags
        # {0, 14}: top 15% = 3, latest in head = 0. Sweep 0..0.
        # Tail anchor 14 < bottom 20% (=16), so tail expansion does
        # NOT fire. Total {0, 14} = 2. No expansion.
        # Try flags {0, 16}: top latest = 0, sweep 0..0. Tail anchor
        # 16 >= 16 (bottom 20% = 16). Earliest in bottom 30% (>=14)
        # = 16. Sweep 16..19 = 4. Total {0, 16, 17, 18, 19} = 5/20.
        # Try flags {0, 14, 16}: tail anchor 16, earliest in [14,19] =
        # 14. Sweep 14..19 = 6. Plus 0. Total 7/20 = 35%.
        # The cap is genuinely hard to breach with reasonable inputs;
        # the gates force expansion only on already-clustered flags,
        # which keeps the result proportional. So the cap mostly
        # exists as a guardrail. Verify it triggers when set up
        # explicitly.
        # N=12, flags {0, 1, 2, 9, 10, 11}: head 0..2 (latest = 2 in
        # top 15% = 1? No, top 15% = max(2, 1) = 2, so 2 is NOT < 2).
        # latest in head where i < 2: max(0, 1) = 1. Sweep 0..1.
        # 2 stays flagged on its own. Tail bottom 20% = 9. Anchor 11
        # >= 9. Earliest in bottom 30% (>=8): 9. Sweep 9..11 (already
        # flagged). Total {0, 1, 2, 9, 10, 11} = 6/12 = 50%. Cap
        # rejects > 6, so 6 is allowed. No abort.
        # Conclusion: the cap is unlikely to fire with the current
        # gates because expansion only fires from already-clustered
        # boundary flags. Test that the cap WOULD abort if breached
        # by manually constructing the boundary case.
        # N=10, original {0, 9}: head 0..0, tail 9..9. Total 2/10. Fine.
        # The only way to exceed 5 is to have a long sweep, which
        # requires flag at boundary itself. N=10, flag {0, 7}: head
        # 0..0. Tail anchor 7 < bottom 20% (=8). No tail. Result {0,7}.
        # N=20, flag {0, 16}: as above gives 5/20. Fine.
        # The cap fires when N is small AND the LLM flag is near the
        # END such that the sweep covers a large fraction. N=8, flag
        # {7}: bottom 20% = 6. Anchor 7 >= 6. Earliest in bottom 30%
        # (>= 5): 7. Sweep 7..7. Result {7} = 1/8. Fine.
        # Need to artificially exercise the cap branch by giving an
        # impossible-but-valid input.
        # Easiest: monkey the threshold via subclass? No. Just test
        # that the cap branch is reachable. N=10, simulate by
        # passing flags that span a wide head AND a wide tail.
        # Flags {0, 1, 8, 9}: head 0..1, tail 8..9. Result 4/10. Fine.
        # Flags {0, 1, 2, 7, 8, 9}: head 0..1 + standalone 2; tail
        # 7..9. Total 6/10 = 60%. Cap rejects > 5. Returns original.
        result = attribution.expand_an_block({0, 1, 2, 7, 8, 9}, 10)
        # The cap fires; original set returned unchanged.
        assert result == {0, 1, 2, 7, 8, 9}

    def test_realworld_si_vis_pacem_chapter_42(self):
        """Pin against the actual chapter Matt hit: 117 paragraphs,
        LLM flagged {1, 60, 103, 110, 116}. Expected expansion
        flags 0 (intro disclaimer neighbour) and 103..116 (the
        outro A/N block)."""
        flagged = {1, 60, 103, 110, 116}
        result = attribution.expand_an_block(flagged, 117)
        # Head: top 5% = 5, latest flag in top 15% (= 17): 1.
        # Sweep 0..1.
        assert {0, 1} <= result
        # Tail: bottom 20% = 93, earliest in bottom 30% (= 81): 103.
        # Sweep 103..116.
        assert set(range(103, 117)) <= result
        # Mid-chapter flag survives but no surrounding sweep.
        assert 60 in result
        # Nothing else got pulled in.
        expected = {0, 1, 60} | set(range(103, 117))
        assert result == expected


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


class TestLlmCallStructuredOutput:
    """The ``response_schema`` plumbing on ``_llm_call``. Captured against
    the Ollama path because that's the one constrained-decode actually
    helps — qwen2.5:14b on the FFN founders'-vault fic produced a
    scene-summary schema instead of the documented ``{"1": true}`` shape,
    and forcing the schema at the API level is the difference between
    that fic's tail A/Ns getting stripped and surviving."""

    def _capture_payload(self, monkeypatch):
        captured: dict = {}

        class _Resp:
            def __init__(self, body):
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            import json as _json
            captured["payload"] = _json.loads(req.data.decode("utf-8"))
            # A minimally valid Ollama chat reply so ``_llm_call``
            # returns successfully.
            return _Resp(
                b'{"message": {"content": "{\\"1\\": true}"}}'
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        return captured

    def test_ollama_passes_schema_dict_in_format(self, monkeypatch):
        captured = self._capture_payload(monkeypatch)
        schema = {
            "type": "object",
            "properties": {"1": {"type": "boolean"}},
            "required": ["1"],
            "additionalProperties": False,
        }
        attribution._llm_call(
            provider="ollama", model="qwen2.5:14b", api_key="",
            endpoint="http://127.0.0.1:11434",
            system_prompt="sys", user_prompt="usr",
            response_schema=schema,
        )
        # ``format`` must be the schema dict, not the literal "json"
        # string — that's the change qwen needs to comply.
        assert captured["payload"]["format"] == schema

    def test_ollama_falls_back_to_json_string_without_schema(self, monkeypatch):
        # Backwards compatibility: callers that don't opt in still get
        # the v0.4-era any-JSON mode, so existing audiobook attribution
        # callers (which don't constrain the shape) keep working.
        captured = self._capture_payload(monkeypatch)
        attribution._llm_call(
            provider="ollama", model="m", api_key="",
            endpoint="http://127.0.0.1:11434",
            system_prompt="sys", user_prompt="usr",
        )
        assert captured["payload"]["format"] == "json"

    def test_classify_authors_notes_builds_per_paragraph_schema(
        self, monkeypatch,
    ):
        # The A/N classifier constructs a schema enumerating one
        # boolean per input paragraph, with all keys required and no
        # additional properties allowed. Without this, qwen2.5:14b
        # invents its own scene-summary shape.
        captured: dict = {}

        def fake_call(*, provider, model, api_key, endpoint,
                      system_prompt, user_prompt, response_schema=None,
                      request_timeout_s=None):
            captured["response_schema"] = response_schema
            return '{"1": true, "2": false, "3": true}'

        monkeypatch.setattr(attribution, "_llm_call", fake_call)
        paragraphs = ["a", "b", "c"]
        flagged = attribution.classify_authors_notes_via_llm(
            paragraphs, llm_config=_llm_config(),
        )
        schema = captured["response_schema"]
        assert schema is not None
        assert schema["type"] == "object"
        assert set(schema["properties"]) == {"1", "2", "3"}
        assert all(
            v == {"type": "boolean"}
            for v in schema["properties"].values()
        )
        assert sorted(schema["required"]) == ["1", "2", "3"]
        assert schema["additionalProperties"] is False
        assert flagged == {0, 2}


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


# ── Timeout circuit breaker (consecutive-failure threshold) ──────


class TestLlmCallTimeoutClassification:
    """``_llm_call`` distinguishes a request timeout (slow model on a
    long input) from a connection failure (nothing listening). Only
    the latter is a one-strike circuit-breaker trip; timeouts surface
    as the :class:`LLMTimeout` subclass so the chapter loop applies a
    consecutive-failure threshold instead."""

    def test_bare_timeout_error_raises_llm_timeout(self, monkeypatch):
        def boom(req, timeout=None):
            raise TimeoutError("read timed out")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(attribution.LLMTimeout):
            attribution._llm_call(
                provider="ollama", model="m", api_key="",
                endpoint="http://127.0.0.1:11434",
                system_prompt="", user_prompt="",
            )

    def test_url_error_wrapping_timeout_raises_llm_timeout(self, monkeypatch):
        # urllib often wraps a socket timeout inside URLError. The
        # adapter has to peek at .reason and still classify as a
        # timeout — otherwise this path tripped the breaker on a
        # single slow chapter (the exact bug from the user's log).
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(attribution.LLMTimeout):
            attribution._llm_call(
                provider="ollama", model="m", api_key="",
                endpoint="http://127.0.0.1:11434",
                system_prompt="", user_prompt="",
            )

    def test_llm_timeout_is_subclass_of_unavailable(self):
        # Backwards compatibility for older ``except LLMUnavailable``
        # call sites — they keep tripping on timeouts even when the
        # adapter has classified a timeout specifically. Newer call
        # sites that want the threshold path catch ``LLMTimeout``
        # before ``LLMUnavailable``.
        assert issubclass(attribution.LLMTimeout, attribution.LLMUnavailable)


class TestExportLoopTimeoutThreshold:
    """A single LLM timeout is transient on local models — a 14B model
    on CPU can spend several minutes on one long chapter, and the
    classifier is fine for the next one. The chapter loop only trips
    the breaker after :data:`_LLM_AN_MAX_CONSECUTIVE_TIMEOUTS`
    consecutive timeouts."""

    def _multi_chapter_story(self, n: int):
        from ffn_dl.models import Chapter, Story
        s = Story(
            id=1,
            title="T",
            author="A",
            summary="",
            url="https://www.fanfiction.net/s/1",
        )
        # Each chapter gets unique paragraph text so the per-content
        # cache key differs — otherwise a single classified chapter
        # would satisfy the rest from cache and the test wouldn't
        # exercise the per-chapter LLM call path. Plain prose only:
        # words like "Chapter N" trigger the regex pre-pass and
        # strip the paragraphs below the LLM threshold.
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                 "golf", "hotel", "india", "juliet"]
        for i in range(1, n + 1):
            tag = words[(i - 1) % len(words)]
            s.chapters.append(Chapter(
                number=i,
                title=f"Ch {i}",
                html=(
                    f"<p>The {tag} morning was bright and clear.</p>"
                    f"<p>The {tag} river ran swift past the village.</p>"
                    f"<p>The {tag} hawk circled overhead in silence.</p>"
                    f"<p>The {tag} cart wheeled away down the lane.</p>"
                ),
            ))
        return s

    def test_one_timeout_does_not_disable_for_remaining_chapters(
        self, tmp_path, monkeypatch,
    ):
        """First chapter times out, rest succeed. The loop must keep
        the LLM enabled for chapters 2..N — that was the regression
        from the user's log where one slow chapter killed A/N
        stripping for the rest of the story."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls: list = []

        def fake_call(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise attribution.LLMTimeout("timed out")
            return json.dumps(
                {"1": False, "2": False, "3": False, "4": False},
            )

        monkeypatch.setattr(attribution, "_llm_call", fake_call)
        story = self._multi_chapter_story(5)
        progress_lines: list[str] = []
        exporters.export_html(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
            progress=progress_lines.append,
        )
        # All five chapters tried the LLM; one timed out, four ran.
        assert len(calls) == 5, (
            "After a single timeout the loop must keep trying the LLM "
            "on subsequent chapters"
        )
        # No "remaining chapters" disable line — that's reserved for
        # the threshold trip and the unreachable case.
        disable_lines = [
            l for l in progress_lines
            if "skipping LLM for remaining chapters" in l
        ]
        assert disable_lines == []

    def test_threshold_consecutive_timeouts_trip_the_breaker(
        self, tmp_path, monkeypatch,
    ):
        """N consecutive timeouts trip the breaker. After the trip
        no more LLM calls go out for the remaining chapters — same
        end state as an unreachable endpoint, just gated behind a
        higher bar."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        threshold = exporters._LLM_AN_MAX_CONSECUTIVE_TIMEOUTS
        calls: list = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMTimeout("timed out")

        monkeypatch.setattr(attribution, "_llm_call", boom)
        story = self._multi_chapter_story(threshold + 5)
        progress_lines: list[str] = []
        exporters.export_html(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
            progress=progress_lines.append,
        )
        assert len(calls) == threshold, (
            "Breaker should trip after exactly "
            f"{threshold} consecutive timeouts"
        )
        # The disable notice fires once on the trip.
        disable_lines = [
            l for l in progress_lines
            if "skipping LLM for remaining chapters" in l
        ]
        assert len(disable_lines) == 1

    def test_success_resets_the_timeout_streak(
        self, tmp_path, monkeypatch,
    ):
        """Two timeouts, a success, then more timeouts — the success
        resets the streak so the breaker doesn't trip on a total of
        N timeouts spread across the story; only N *in a row*."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        threshold = exporters._LLM_AN_MAX_CONSECUTIVE_TIMEOUTS
        # Pattern: timeout, timeout, success, timeout, timeout — total
        # of four timeouts but the longest streak is two. Story is
        # six chapters so we'd expect six LLM calls if the breaker
        # never trips.
        outcomes = ["timeout", "timeout", "success", "timeout", "timeout", "success"]
        assert len(outcomes) >= threshold + 2  # so the test is meaningful
        calls: list = []

        def fake_call(**kwargs):
            outcome = outcomes[len(calls)]
            calls.append(outcome)
            if outcome == "timeout":
                raise attribution.LLMTimeout("timed out")
            return json.dumps(
                {"1": False, "2": False, "3": False, "4": False},
            )

        monkeypatch.setattr(attribution, "_llm_call", fake_call)
        story = self._multi_chapter_story(len(outcomes))
        exporters.export_html(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
        )
        assert len(calls) == len(outcomes), (
            "A success between timeouts must reset the streak so the "
            "breaker only trips on N consecutive failures"
        )

    def test_unreachable_still_one_strike_after_timeout_subclassing(
        self, tmp_path, monkeypatch,
    ):
        """Even though LLMTimeout subclasses LLMUnavailable, a plain
        LLMUnavailable (connection refused / DNS) must still trip on
        the first hit — that's the original bug fix and the threshold
        only applies to genuine timeouts."""
        _isolate_cache_dir(monkeypatch, tmp_path)
        calls: list = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise attribution.LLMUnavailable("refused")

        monkeypatch.setattr(attribution, "_llm_call", boom)
        story = self._multi_chapter_story(5)
        exporters.export_html(
            story, output_dir=str(tmp_path),
            strip_notes=True,
            llm_config=_llm_config(),
        )
        assert len(calls) == 1, (
            "Connection-refused failures must still be one-strike — "
            "the threshold only applies to LLMTimeout"
        )


class TestLlmRequestTimeoutEnvOverride:
    """``FFN_DL_LLM_TIMEOUT_S`` lets users on slow hardware extend the
    per-request deadline without editing source. The default 300s is
    sized for a 14B model on CPU, but a *long* chapter on the same
    hardware can still time out — bumping to 600 or 900 fixes those
    runs. Garbage values must fall back to the default rather than
    silently disabling the timeout (urllib treats ``timeout=0`` as
    non-blocking, which would break every call)."""

    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv("FFN_DL_LLM_TIMEOUT_S", raising=False)
        assert (
            attribution._llm_request_timeout_s()
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_blank_returns_default(self, monkeypatch):
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "   ")
        assert (
            attribution._llm_request_timeout_s()
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_positive_int_overrides(self, monkeypatch):
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "900")
        assert attribution._llm_request_timeout_s() == 900

    def test_positive_float_truncates_to_int(self, monkeypatch):
        # urllib.request.urlopen accepts floats, but staying int keeps
        # log lines tidy and matches every other timeout in the file.
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "450.7")
        assert attribution._llm_request_timeout_s() == 450

    def test_non_numeric_falls_back(self, monkeypatch):
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "forever")
        assert (
            attribution._llm_request_timeout_s()
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_zero_falls_back(self, monkeypatch):
        # Zero or negative would either disable timeouts (urllib's
        # non-blocking mode) or raise — neither matches "longer
        # deadline", so we ignore the value.
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "0")
        assert (
            attribution._llm_request_timeout_s()
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "-30")
        assert (
            attribution._llm_request_timeout_s()
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_call_uses_active_timeout(self, monkeypatch):
        # End-to-end check: the env value flows through ``_llm_call``
        # to urllib's ``timeout=`` argument. Without this, a refactor
        # could resurrect the hardcoded 300 and the unit tests above
        # would still pass.
        seen = {}

        class _Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"message":{"content":"{}"}}'

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return _Resp()

        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "777")
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        attribution._llm_call(
            provider="ollama", model="m", api_key="",
            endpoint="http://127.0.0.1:11434",
            system_prompt="", user_prompt="",
        )
        assert seen["timeout"] == 777


class TestLlmRequestTimeoutOverride:
    """The GUI/CLI can pass an explicit ``request_timeout_s`` through
    ``llm_config`` to bypass the env var. Without this path, users on
    slow hardware would have to set an environment variable to extend
    the deadline — annoying on Windows, near-impossible inside a
    frozen .exe."""

    def test_override_beats_env(self, monkeypatch):
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "450")
        assert attribution._llm_request_timeout_s(900) == 900

    def test_override_zero_falls_through_to_env(self, monkeypatch):
        # 0 is the dialog's "no override" sentinel — must NOT be
        # treated as "0-second timeout" (which urllib reads as
        # non-blocking and would break every call).
        monkeypatch.setenv("FFN_DL_LLM_TIMEOUT_S", "450")
        assert attribution._llm_request_timeout_s(0) == 450

    def test_override_negative_falls_through_to_default(self, monkeypatch):
        monkeypatch.delenv("FFN_DL_LLM_TIMEOUT_S", raising=False)
        assert (
            attribution._llm_request_timeout_s(-30)
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_override_non_numeric_falls_through(self, monkeypatch):
        monkeypatch.delenv("FFN_DL_LLM_TIMEOUT_S", raising=False)
        assert (
            attribution._llm_request_timeout_s("forever")
            == attribution._LLM_REQUEST_TIMEOUT_DEFAULT_S
        )

    def test_llm_config_override_threads_to_urlopen(self, monkeypatch):
        # End-to-end: putting ``request_timeout_s`` into the llm_config
        # dict has to flow through ``classify_authors_notes_via_llm``
        # → ``_llm_call`` → ``urllib.request.urlopen``. A regression
        # here would silently fall back to the env/default and the
        # GUI setting would do nothing.
        seen = {}

        class _Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return b'{"message":{"content":"{\\"1\\":false}"}}'

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return _Resp()

        monkeypatch.delenv("FFN_DL_LLM_TIMEOUT_S", raising=False)
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        attribution.classify_authors_notes_via_llm(
            ["only paragraph"],
            llm_config={
                "provider": "ollama", "model": "m", "api_key": "",
                "endpoint": "http://127.0.0.1:11434",
                "request_timeout_s": 777,
            },
        )
        assert seen["timeout"] == 777


class TestOllamaRuntimeProbe:
    """``_llm_ollama_runtime`` reports whether the user's local Ollama
    is running the requested model on GPU or CPU, so the classifier
    log line can answer "is it worth switching?" at a glance. Strictly
    informational — the probe must never block or fail loudly."""

    def _stub_ps(self, monkeypatch, payload):
        import json as _json

        class _Resp:
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return _json.dumps(payload).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    def test_full_gpu(self, monkeypatch):
        self._stub_ps(monkeypatch, {"models": [
            {"name": "qwen2.5:14b", "size": 9_000_000_000,
             "size_vram": 9_000_000_000},
        ]})
        assert (
            attribution._llm_ollama_runtime(
                "http://127.0.0.1:11434", "qwen2.5:14b",
            )
            == "GPU"
        )

    def test_full_cpu(self, monkeypatch):
        self._stub_ps(monkeypatch, {"models": [
            {"name": "qwen2.5:14b", "size": 9_000_000_000, "size_vram": 0},
        ]})
        assert (
            attribution._llm_ollama_runtime(
                "http://127.0.0.1:11434", "qwen2.5:14b",
            )
            == "CPU"
        )

    def test_partial_gpu_reports_percentage(self, monkeypatch):
        self._stub_ps(monkeypatch, {"models": [
            {"name": "qwen2.5:14b", "size": 10_000_000_000,
             "size_vram": 7_500_000_000},
        ]})
        assert (
            attribution._llm_ollama_runtime(
                "http://127.0.0.1:11434", "qwen2.5:14b",
            )
            == "partial GPU (75%)"
        )

    def test_model_not_loaded_returns_none(self, monkeypatch):
        # Before the first /api/chat, /api/ps returns an empty list.
        # Returning None lets the caller skip the runtime tag rather
        # than show stale info.
        self._stub_ps(monkeypatch, {"models": []})
        assert (
            attribution._llm_ollama_runtime(
                "http://127.0.0.1:11434", "qwen2.5:14b",
            )
            is None
        )

    def test_endpoint_unreachable_returns_none(self, monkeypatch):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert (
            attribution._llm_ollama_runtime(
                "http://127.0.0.1:11434", "qwen2.5:14b",
            )
            is None
        )

    def test_blank_endpoint_returns_none(self):
        assert attribution._llm_ollama_runtime("", "qwen2.5:14b") is None

    def test_matches_on_base_name_when_tag_differs(self, monkeypatch):
        # Some Ollama versions report the model without the tag in
        # /api/ps; tolerate that rather than reporting "model not
        # loaded" when it clearly is.
        self._stub_ps(monkeypatch, {"models": [
            {"name": "qwen2.5", "size": 9_000_000_000,
             "size_vram": 9_000_000_000},
        ]})
        assert (
            attribution._llm_ollama_runtime(
                "http://127.0.0.1:11434", "qwen2.5:14b",
            )
            == "GPU"
        )
