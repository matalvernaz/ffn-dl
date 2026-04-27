"""Regression tests for speech-rate plumbing and the attribution
backend registry / dispatcher. These do NOT hit the network or install
anything — they exercise the dispatcher and shape only."""
from unittest import mock

import pytest

from ffn_dl import attribution
from ffn_dl.tts import (
    Segment,
    _combine_rate,
    _rate_str,
)


@pytest.fixture(autouse=True)
def _reset_attribution_state():
    """The dispatcher dedupes repeated failures per process; tests need
    a clean slate so an earlier test's synthetic failure doesn't mute
    a later test's real call."""
    attribution._failed_runs.clear()
    attribution._booknlp_cache.clear()
    attribution._spacy_model_checked.clear()
    yield
    attribution._failed_runs.clear()
    attribution._booknlp_cache.clear()
    attribution._spacy_model_checked.clear()


# ── speech rate ────────────────────────────────────────────────────


def test_rate_str_zero_and_none_return_none():
    assert _rate_str(0) is None
    assert _rate_str(None) is None


@pytest.mark.parametrize("pct,expected", [
    (10, "+10%"),
    (-15, "-15%"),
    (100, "+100%"),
    (-50, "-50%"),
])
def test_rate_str_formatting(pct, expected):
    assert _rate_str(pct) == expected


def test_combine_rate_sums_user_and_emotion():
    # Shouting emotion is +10%, user set +20 → total +30%
    assert _combine_rate(20, "+10%") == "+30%"


def test_combine_rate_honors_user_alone():
    assert _combine_rate(25, None) == "+25%"


def test_combine_rate_honors_emotion_alone():
    assert _combine_rate(0, "-20%") == "-20%"
    assert _combine_rate(None, "-20%") == "-20%"


def test_combine_rate_cancels_to_none():
    assert _combine_rate(10, "-10%") is None


def test_combine_rate_bad_emotion_string_falls_back_to_user():
    assert _combine_rate(20, "bogus") == "+20%"


# ── attribution backend registry ──────────────────────────────────


def test_available_lists_all_backends():
    assert attribution.available() == ["builtin", "fastcoref", "booknlp", "llm"]


def test_builtin_is_always_installed():
    assert attribution.is_installed("builtin") is True


def test_unknown_backend_not_installed():
    assert attribution.is_installed("made_up_model") is False


def test_builtin_has_no_install_command():
    assert attribution.install_command("builtin") is None


def test_fastcoref_install_command_shape():
    cmd = attribution.install_command("fastcoref")
    assert cmd is not None
    assert cmd[-1] == "fastcoref"
    assert "install" in cmd


def test_booknlp_install_command_shape():
    cmd = attribution.install_command("booknlp")
    assert cmd is not None
    assert cmd[-1] == "booknlp"


# ── dispatcher behavior ───────────────────────────────────────────


def test_refine_builtin_is_noop():
    segs = [Segment("Hello", speaker="Harry")]
    out = attribution.refine_speakers(segs, "Hello, he said.", backend="builtin")
    assert out is segs  # no copy, no change
    assert out[0].speaker == "Harry"


def test_refine_uninstalled_falls_back_without_raising(caplog):
    """Asking for a non-installed backend must not raise — the render
    always continues with the builtin parser."""
    segs = [Segment("Hi", speaker="Harry")]
    with mock.patch.object(attribution, "is_installed", return_value=False):
        out = attribution.refine_speakers(segs, "Hi", backend="fastcoref")
    assert out is segs


def test_refine_unknown_backend_falls_back():
    segs = [Segment("Hi", speaker="Harry")]
    out = attribution.refine_speakers(segs, "Hi", backend="definitely_not_real")
    # Unknown backend passes through is_installed=False then the unknown path
    assert out is segs


def test_refine_exception_in_backend_falls_back(monkeypatch):
    segs = [Segment("Hi", speaker="Harry")]

    def boom(*args, **kwargs):
        raise RuntimeError("simulated backend explosion")

    monkeypatch.setattr(attribution, "is_installed", lambda b: True)
    monkeypatch.setattr(attribution, "_refine_with_fastcoref", boom)

    out = attribution.refine_speakers(segs, "Hi", backend="fastcoref")
    assert out is segs  # segments preserved, no crash


def test_refine_none_backend_is_builtin():
    segs = [Segment("Hi", speaker="Harry")]
    assert attribution.refine_speakers(segs, "Hi", backend=None) is segs
    assert attribution.refine_speakers(segs, "Hi", backend="") is segs


def test_has_failed_reports_uninstalled_backend():
    segs = [Segment("Hi", speaker="Harry")]
    assert not attribution.has_failed("booknlp", "big")
    with mock.patch.object(attribution, "is_installed", return_value=False):
        attribution.refine_speakers(segs, "Hi", backend="booknlp", model_size="big")
    assert attribution.has_failed("booknlp", "big")
    # Different size variant tracked separately.
    assert not attribution.has_failed("booknlp", "small")


def test_has_failed_reports_runtime_exception(monkeypatch):
    segs = [Segment("Hi", speaker="Harry")]
    monkeypatch.setattr(attribution, "is_installed", lambda b: True)
    monkeypatch.setattr(
        attribution, "_refine_with_fastcoref",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    attribution.refine_speakers(segs, "Hi", backend="fastcoref")
    assert attribution.has_failed("fastcoref")


# ── post-attribution self-intro & junk-speaker passes ─────────────


def test_self_intro_reassigns_carryforward_speaker():
    """The "I'm Ron, by the way, Ron Weasley." pattern — carryforward
    from the previous speaker is replaced by the name inside the quote
    when that name shows up elsewhere as a confirmed speaker."""
    # Ron must appear as a confirmed speaker (count >= 2) for the
    # validator to trust the single-token self-intro path.
    chapter = [
        Segment("I'm Ron, by the way, Ron Weasley.", speaker="Harry"),
        Segment("Harry replied and smiled."),
        Segment("Hmmm...", speaker="Ron"),
        Segment("Yeah, sure thing.", speaker="Ron"),
    ]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Ron"


def test_self_intro_does_not_overwrite_distinct_speaker():
    """A line whose speaker differs from the previous segment's
    speaker was explicitly attributed by the backend — leave it."""
    chapter = [
        Segment("I'm Ron.", speaker="Ron"),
        Segment("Nice to meet you.", speaker="Harry"),
        # "I am Sirius", but speaker is Draco (already distinct) — the
        # guard should skip this since the backend deliberately picked
        # a different speaker. We don't second-guess them.
        Segment("I am Sirius, though.", speaker="Draco"),
        Segment("And I.", speaker="Sirius"),
        Segment("Another.", speaker="Sirius"),
    ]
    attribution.post_refine([chapter])
    assert chapter[2].speaker == "Draco"


def test_self_intro_rejects_common_adjective_after_im():
    """'I'm Sorry' / 'I'm Cold' must not hijack attribution even on a
    carryforward — neither is a confirmed speaker in the book."""
    chapter = [
        Segment("I'm sorry,", speaker="Harry"),  # lowercase "sorry" — won't match
        Segment("I'm Cold.", speaker="Harry"),  # single token, not confirmed elsewhere
        Segment("Next line.", speaker="Harry"),
    ]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Harry"
    assert chapter[1].speaker == "Harry"


def test_self_intro_accepts_first_last_name_without_corroboration():
    """A full "First Last" pair is strong enough to trust even if
    that character only speaks once. 'I'm Hermione Granger.' —
    unmistakable even before Hermione speaks again."""
    chapter = [
        Segment("I'm Hermione Granger.", speaker="Harry"),
        Segment("Harry replied."),
    ]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Hermione Granger"


def test_my_name_is_pattern():
    chapter = [
        Segment("My name is Alastor Moody.", speaker="Harry"),
        Segment("Next.", speaker="Moody"),
        Segment("And another.", speaker="Moody"),
    ]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Alastor Moody"


def test_call_me_pattern():
    chapter = [
        Segment("Call me Tom.", speaker="Ron"),
        Segment("Tom nodded."),
        Segment("Pleasure.", speaker="Tom"),
        Segment("Absolutely.", speaker="Tom"),
    ]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Tom"


def test_junk_speaker_demoted_to_narrator():
    """'Cruciatus' appearing once as a speaker is almost certainly
    BookNLP mis-tagging a spell name. Demote to narrator."""
    chapter_a = [
        Segment("Cast the spell!", speaker="Cruciatus"),
        Segment("Harry ducked.", speaker="Harry"),
        Segment("Follow-up.", speaker="Harry"),
    ]
    chapter_b = [Segment("Next.", speaker="Harry")]
    attribution.post_refine([chapter_a, chapter_b])
    assert chapter_a[0].speaker is None


def test_junk_speaker_kept_when_recurring():
    """Demotion only fires on single-occurrence junk tokens.
    If 'Wizard' somehow spoke twice — implausible in normal fic but
    we stay conservative — don't touch them."""
    chapter = [
        Segment("Line one.", speaker="Wizard"),
        Segment("Line two.", speaker="Wizard"),
    ]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Wizard"
    assert chapter[1].speaker == "Wizard"


def test_junk_filter_ignores_multi_word_names():
    """'Captain Hook' is a two-word name — the junk list is a
    single-word case-insensitive check only."""
    chapter = [Segment("Yaarr.", speaker="Captain Hook")]
    attribution.post_refine([chapter])
    assert chapter[0].speaker == "Captain Hook"


# ── model-size variants ───────────────────────────────────────────


def test_sizes_for_builtin_is_none():
    assert attribution.sizes_for("builtin") is None


def test_sizes_for_fastcoref_is_none():
    assert attribution.sizes_for("fastcoref") is None


def test_sizes_for_booknlp_has_small_and_big():
    sizes = attribution.sizes_for("booknlp")
    assert sizes is not None
    assert set(sizes.keys()) == {"small", "big"}
    # Every size entry carries a user-facing display label.
    for v in sizes.values():
        assert "display" in v


def test_default_size_booknlp_is_small():
    assert attribution.default_size("booknlp") == "small"


# ── BookNLP model manifest / resumable downloader ─────────────────


def test_booknlp_model_manifest_shape():
    """The manifest is consulted to validate on-disk files before
    BookNLP's own broken downloader runs; sizes must be stable ints."""
    assert set(attribution._BOOKNLP_MODELS.keys()) == {"small", "big"}
    for size, files in attribution._BOOKNLP_MODELS.items():
        assert len(files) == 3, f"{size} should list 3 model files"
        for fname, nbytes in files:
            assert fname.endswith(".model")
            assert isinstance(nbytes, int) and nbytes > 0


def test_ensure_booknlp_models_skips_complete_files(monkeypatch, tmp_path):
    """If every expected file is already on disk at the right size,
    we must not re-download."""
    monkeypatch.setattr(attribution, "_booknlp_model_dir", lambda: tmp_path)
    for fname, size in attribution._BOOKNLP_MODELS["small"]:
        (tmp_path / fname).write_bytes(b"\0" * size)

    called = []
    monkeypatch.setattr(
        attribution, "_download_booknlp_file",
        lambda *a, **k: called.append(a),
    )
    attribution._ensure_booknlp_models("small")
    assert called == []


def test_ensure_booknlp_models_redownloads_short_file(monkeypatch, tmp_path):
    """A truncated file (matches BookNLP's ``is_file()`` guard but is
    smaller than Content-Length) must be deleted and re-fetched — this
    is the exact hang scenario we're defending against."""
    monkeypatch.setattr(attribution, "_booknlp_model_dir", lambda: tmp_path)
    manifest = attribution._BOOKNLP_MODELS["small"]
    # First file truncated to half its expected size.
    short_name, short_size = manifest[0]
    (tmp_path / short_name).write_bytes(b"\0" * (short_size // 2))
    # Second and third fully present.
    for fname, size in manifest[1:]:
        (tmp_path / fname).write_bytes(b"\0" * size)

    called = []

    def fake_download(url, dest, expected_size):
        called.append((dest.name, expected_size))
        dest.write_bytes(b"\0" * expected_size)

    monkeypatch.setattr(attribution, "_download_booknlp_file", fake_download)
    attribution._ensure_booknlp_models("small")
    assert called == [(short_name, short_size)]


def test_default_size_no_variants_returns_none():
    assert attribution.default_size("builtin") is None
    assert attribution.default_size("fastcoref") is None


def test_normalize_size_clamps_unknown_to_default():
    assert attribution.normalize_size("booknlp", "enormous") == "small"
    assert attribution.normalize_size("booknlp", None) == "small"
    assert attribution.normalize_size("booknlp", "big") == "big"


def test_normalize_size_for_no_variant_backend_is_none():
    assert attribution.normalize_size("builtin", "small") is None
    assert attribution.normalize_size("fastcoref", "big") is None


def test_refine_passes_size_through_to_booknlp(monkeypatch):
    """model_size should reach the BookNLP adapter after normalization."""
    seen = {}

    def fake(segments, full_text, model_size="small"):
        seen["size"] = model_size
        return segments

    monkeypatch.setattr(attribution, "is_installed", lambda b: True)
    monkeypatch.setattr(attribution, "_refine_with_booknlp", fake)

    segs = [Segment("Hi", speaker="Harry")]
    attribution.refine_speakers(segs, "Hi", backend="booknlp", model_size="big")
    assert seen == {"size": "big"}


def test_refine_ignores_size_for_fastcoref(monkeypatch):
    """Sizes-less backends must be invoked without a size argument."""
    called = {}

    def fake(segments, full_text):
        called["ok"] = True
        return segments

    monkeypatch.setattr(attribution, "is_installed", lambda b: True)
    monkeypatch.setattr(attribution, "_refine_with_fastcoref", fake)

    segs = [Segment("Hi", speaker="Harry")]
    attribution.refine_speakers(segs, "Hi", backend="fastcoref", model_size="big")
    assert called == {"ok": True}


# ── frozen-exe handling ────────────────────────────────────────────


def test_install_command_none_when_frozen(monkeypatch):
    """Frozen builds don't use sys.executable for pip — it points at
    the .exe bootloader. install_command must return None so callers
    don't mistakenly Popen the exe with pip flags."""
    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    assert attribution.install_command("fastcoref") is None
    assert attribution.install_command("booknlp") is None


def test_install_unsupported_reason_none_on_windows_frozen(monkeypatch):
    """On Windows-frozen, install is supported via neural_env — no reason to refuse."""
    from ffn_dl import neural_env

    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    monkeypatch.setattr(neural_env, "is_supported", lambda: True)
    assert attribution.install_unsupported_reason("fastcoref") is None
    assert attribution.install_unsupported_reason("booknlp") is None


def test_install_unsupported_reason_non_windows_frozen(monkeypatch):
    """If we ever ship a frozen build on a platform neural_env doesn't
    handle, install() must refuse with an explanation instead of
    silently no-opping."""
    from ffn_dl import neural_env

    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    monkeypatch.setattr(neural_env, "is_supported", lambda: False)
    reason = attribution.install_unsupported_reason("fastcoref")
    assert reason and "Windows" in reason


def test_install_routes_through_neural_env_when_frozen(monkeypatch):
    """Frozen install() must NOT Popen sys.executable — it must call
    neural_env.pip_install with the backend's pip_name and the
    CPU-torch extra-index-url so it doesn't pull 2.5 GB of CUDA."""
    from ffn_dl import neural_env

    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    monkeypatch.setattr(neural_env, "is_supported", lambda: True)

    seen = {}

    def fake_pip(packages, log_callback=None, extra_args=None):
        seen["packages"] = list(packages)
        seen["extra_args"] = list(extra_args or [])
        return True

    monkeypatch.setattr(neural_env, "pip_install", fake_pip)

    ok = attribution.install("fastcoref", log_callback=lambda _l: None)
    assert ok is True
    assert seen["packages"] == ["fastcoref"]
    # CPU torch index must be passed to keep the install sane-sized.
    assert "--extra-index-url" in seen["extra_args"]
    assert any("cpu" in a for a in seen["extra_args"])


def test_install_builtin_noop_when_frozen(monkeypatch):
    """builtin is nothing to install — the frozen guard must not
    accidentally start rejecting it."""
    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    assert attribution.install("builtin") is True


# ── character-list grounding ──────────────────────────────────────


def test_character_tokens_handles_ffn_initial_suffix():
    """FFN tags arrive as 'Harry P., Hermione G.' — both the full
    string and the first-name-only form should count as known speakers
    so a backend emitting 'Harry' is still grounded."""
    out = attribution._character_tokens(["Harry P.", "Hermione G."])
    assert "Harry P." in out
    assert "Harry" in out
    assert "Hermione G." in out
    assert "Hermione" in out


def test_character_tokens_handles_ao3_full_names():
    out = attribution._character_tokens(["Harry Potter", "Hermione Granger"])
    assert "Harry Potter" in out
    assert "Harry" in out
    assert "Potter" in out
    assert "Granger" in out


def test_character_tokens_empty_input():
    assert attribution._character_tokens(None) == set()
    assert attribution._character_tokens([]) == set()
    assert attribution._character_tokens(["", "  "]) == set()


def test_post_refine_self_intro_trusts_cast_list_first_name():
    """Without a cast list, a single-token self-intro for a never-
    elsewhere-mentioned name is rejected. With the name on the cast
    list it should be trusted on first occurrence."""
    chapter = [
        Segment("I'm Padma.", speaker="Harry"),
        Segment("Padma kept silent."),
    ]
    # Without cast: rejected (Padma only appears once, no two-token form).
    attribution.post_refine([list(chapter)])
    assert chapter[0].speaker == "Harry"
    # With cast: accepted.
    attribution.post_refine([chapter], character_list=["Padma Patil"])
    assert chapter[0].speaker == "Padma"


def test_post_refine_junk_filter_spares_cast_member():
    """'Captain' is on the junk list. If the story's character tag is
    'Captain America', the single-occurrence junk demotion must NOT
    fire — that's a real cast member."""
    chapter_a = [
        Segment("Listen up.", speaker="Captain"),
        Segment("Steve nodded.", speaker="Steve"),
        Segment("Right.", speaker="Steve"),
    ]
    attribution.post_refine(
        [chapter_a], character_list=["Captain America", "Steve Rogers"],
    )
    assert chapter_a[0].speaker == "Captain"


def test_post_refine_junk_filter_still_demotes_unrelated_word():
    """A junk word with no cast match still gets demoted on a single
    occurrence — the cast list is a positive override, not a switch."""
    chapter = [
        Segment("Boom!", speaker="Cruciatus"),
        Segment("Harry winced.", speaker="Harry"),
        Segment("Again.", speaker="Harry"),
    ]
    attribution.post_refine(
        [chapter], character_list=["Harry Potter", "Hermione Granger"],
    )
    assert chapter[0].speaker is None


# ── LLM backend ───────────────────────────────────────────────────


def test_llm_provider_supported():
    assert attribution._llm_provider_supported("ollama")
    assert attribution._llm_provider_supported("openai")
    assert attribution._llm_provider_supported("anthropic")
    assert attribution._llm_provider_supported("openai-compatible")
    assert not attribution._llm_provider_supported("totally_made_up")


def test_llm_default_endpoints():
    assert "11434" in attribution._llm_default_endpoint("ollama")
    assert "api.openai.com" in attribution._llm_default_endpoint("openai")
    assert "api.anthropic.com" in attribution._llm_default_endpoint("anthropic")


def test_llm_normalize_endpoint_strips_trailing_slash():
    norm = attribution._llm_normalize_endpoint(
        "openai", "https://example.com/v1/",
    )
    assert norm == "https://example.com/v1"


def test_llm_normalize_endpoint_falls_back_to_default():
    norm = attribution._llm_normalize_endpoint("ollama", None)
    assert "11434" in norm
    norm = attribution._llm_normalize_endpoint("openai", "")
    assert "api.openai.com" in norm


def test_llm_parse_speaker_map_legacy_string_shape():
    """Older prompts emitted bare strings; the parser must keep
    accepting them so anyone whose model still returns the older
    shape isn't broken."""
    out = attribution._llm_parse_speaker_map('{"1": "Harry", "2": "Ron"}')
    assert out == {"1": {"speaker": "Harry"}, "2": {"speaker": "Ron"}}


def test_llm_parse_speaker_map_object_shape_with_emotion():
    """New prompt shape — speaker + emotion per quote."""
    reply = (
        '{"1": {"speaker": "Harry", "emotion": "shouting"}, '
        '"2": {"speaker": "Ron"}}'
    )
    out = attribution._llm_parse_speaker_map(reply)
    assert out == {
        "1": {"speaker": "Harry", "emotion": "shouting"},
        "2": {"speaker": "Ron"},
    }


def test_llm_parse_speaker_map_strips_markdown_fence():
    reply = '```json\n{"1": "Harry"}\n```'
    assert attribution._llm_parse_speaker_map(reply) == {"1": {"speaker": "Harry"}}


def test_llm_parse_speaker_map_isolates_first_object():
    reply = 'Sure! Here is your JSON: {"1": "Harry"} Done.'
    assert attribution._llm_parse_speaker_map(reply) == {"1": {"speaker": "Harry"}}


def test_llm_parse_speaker_map_handles_garbage():
    assert attribution._llm_parse_speaker_map("") == {}
    assert attribution._llm_parse_speaker_map("not json at all") == {}
    assert attribution._llm_parse_speaker_map('{"1": 42}') == {}


def test_llm_normalise_emotion_aliases_to_prosody_keys():
    assert attribution._llm_normalise_emotion("shouting") == "shout"
    assert attribution._llm_normalise_emotion("WHISPERED") == "whisper"
    assert attribution._llm_normalise_emotion("furious") == "angry"
    assert attribution._llm_normalise_emotion("sobbing") == "sad"


def test_llm_normalise_emotion_neutral_collapses_to_none():
    assert attribution._llm_normalise_emotion("neutral") is None
    assert attribution._llm_normalise_emotion("calm") is None
    assert attribution._llm_normalise_emotion("") is None
    assert attribution._llm_normalise_emotion(None) is None


def test_llm_normalise_emotion_unknown_drops():
    """Unknown labels must NOT pass through — they'd confuse the
    prosody table lookup downstream and yield no audible change."""
    assert attribution._llm_normalise_emotion("contemplative") is None
    assert attribution._llm_normalise_emotion("confused") is None


def test_llm_canonicalise_name_narrator_variants():
    cl = ["Harry Potter"]
    tokens = attribution._character_tokens(cl)
    assert attribution._llm_canonicalise_name("Narrator", cl, tokens) is None
    assert attribution._llm_canonicalise_name("UNKNOWN", cl, tokens) is None
    assert attribution._llm_canonicalise_name("", cl, tokens) is None


def test_llm_canonicalise_name_exact_case_insensitive():
    cl = ["Harry Potter", "Hermione Granger"]
    tokens = attribution._character_tokens(cl)
    assert attribution._llm_canonicalise_name(
        "harry potter", cl, tokens,
    ) == "Harry Potter"


def test_llm_canonicalise_name_token_match():
    cl = ["Harry Potter"]
    tokens = attribution._character_tokens(cl)
    assert attribution._llm_canonicalise_name(
        "harry", cl, tokens,
    ) == "Harry"


def test_llm_canonicalise_name_unknown_passes_through():
    """An OC the story didn't tag — preserve verbatim rather than
    dropping. The voice mapper will still get something to assign."""
    cl = ["Harry Potter"]
    tokens = attribution._character_tokens(cl)
    assert attribution._llm_canonicalise_name(
        "Original Character Name", cl, tokens,
    ) == "Original Character Name"


def test_llm_cache_token_distinguishes_provider_and_model():
    a = attribution.llm_cache_token("ollama", "llama3.1:8b")
    b = attribution.llm_cache_token("openai", "gpt-4o-mini")
    assert a != b
    assert "ollama" in a
    assert "openai" in b


def test_llm_cache_token_filesystem_safe():
    """Slashes, colons, spaces in model ids must not break the cache
    path on any platform."""
    token = attribution.llm_cache_token("openai-compatible", "meta/llama-3:8b")
    assert "/" not in token
    assert ":" not in token
    assert " " not in token


def test_refine_llm_requires_config():
    """The dispatcher must surface a missing-config as a fall-back,
    not crash. ``has_failed`` then reports it so the cache-saver
    skips persisting bogus segments."""
    segs = [Segment('"Hi."', speaker=None)]
    out = attribution.refine_speakers(
        segs, '"Hi."', backend="llm", llm_config=None,
    )
    assert out is segs
    assert attribution.has_failed("llm")


def test_refine_llm_routes_to_adapter(monkeypatch):
    """Wiring check: backend=='llm' must invoke ``_refine_with_llm``
    with the config dict spread as kwargs and the character_list
    passed through."""
    seen = {}

    def fake_llm(segments, full_text, *, character_list=None, **kwargs):
        seen["full_text"] = full_text
        seen["character_list"] = list(character_list or [])
        seen["kwargs"] = dict(kwargs)
        return segments

    monkeypatch.setattr(attribution, "_refine_with_llm", fake_llm)

    segs = [Segment('"Hi."', speaker=None)]
    cfg = {
        "provider": "ollama", "model": "llama3.1:8b",
        "api_key": "", "endpoint": "",
    }
    attribution.refine_speakers(
        segs, '"Hi."', backend="llm", llm_config=cfg,
        character_list=["Harry Potter"],
    )
    assert seen["full_text"] == '"Hi."'
    assert seen["character_list"] == ["Harry Potter"]
    assert seen["kwargs"]["provider"] == "ollama"
    assert seen["kwargs"]["model"] == "llama3.1:8b"


def test_refine_with_llm_calls_provider_and_applies_labels(monkeypatch):
    """End-to-end through ``_refine_with_llm`` with a stubbed HTTP
    layer. Verifies the prompt round-trip, the JSON parse, and the
    per-segment overwrite."""
    full_text = (
        'Chapter one. "Hello there," said the boy. The room was '
        'silent. "I am cold," said the girl.'
    )
    segments = [
        Segment("Chapter one."),
        Segment('"Hello there,"'),
        Segment("The room was silent."),
        Segment('"I am cold,"'),
    ]

    def fake_call(*, provider, model, api_key, endpoint,
                  system_prompt, user_prompt, request_timeout_s=None,
                  response_schema=None, options=None):
        # Sanity-check the prompt contains the cast list and quotes.
        assert "Harry Potter" in user_prompt
        assert "Hello there" in user_prompt
        return '{"1": "Harry Potter", "2": "Hermione Granger"}'

    monkeypatch.setattr(attribution, "_llm_call", fake_call)

    out = attribution._refine_with_llm(
        segments, full_text,
        provider="ollama", model="llama3.1:8b",
        api_key=None, endpoint=None,
        character_list=["Harry Potter", "Hermione Granger"],
    )
    assert out[0].speaker is None  # narration untouched
    assert out[1].speaker == "Harry Potter"
    assert out[2].speaker is None  # narration untouched
    assert out[3].speaker == "Hermione Granger"


def test_install_reactivates_deps_dir_after_pip_install(monkeypatch):
    """First-ever neural install creates DEPS_DIR *after* startup's
    activate() already no-oped — so DEPS_DIR isn't on sys.path and the
    post-install _ensure_spacy_model can't see the model it just
    downloaded. install() must re-run activate() after pip_install
    succeeds."""
    from ffn_dl import neural_env

    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    monkeypatch.setattr(neural_env, "is_supported", lambda: True)
    monkeypatch.setattr(
        neural_env, "pip_install",
        lambda packages, log_callback=None, extra_args=None: True,
    )
    # Pretend the spaCy model is already present so install() doesn't
    # try to download it during this unit test.
    monkeypatch.setattr(attribution, "_ensure_spacy_model", lambda *a, **k: True)

    activate_calls = []
    monkeypatch.setattr(
        neural_env, "activate", lambda: activate_calls.append(True),
    )

    ok = attribution.install("booknlp", log_callback=lambda _l: None)
    assert ok is True
    assert activate_calls, "install() must re-activate neural_env after pip_install"
