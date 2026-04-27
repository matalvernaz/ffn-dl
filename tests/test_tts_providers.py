"""Offline tests for the multi-provider TTS layer.

The provider abstraction (``ffn_dl/tts_providers/``) ships two
backends: edge-tts (cloud, the legacy default) and Piper (local ONNX,
lazy model download). These tests run without either being installed
on the host — every external dependency is monkeypatched to a stub.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ffn_dl import accent_map, character_profile, tts_providers


# ── Voice id namespacing / parsing ────────────────────────────────


def test_voice_id_round_trip():
    vid = tts_providers.voice_id("edge", "en-US-AvaNeural")
    assert vid == "edge:en-US-AvaNeural"
    assert tts_providers.parse_voice_id(vid) == ("edge", "en-US-AvaNeural")


def test_parse_voice_id_legacy_bare_name_is_edge():
    """Pre-2.2.0 voice maps don't have the provider prefix; bare names
    must keep resolving to edge so existing per-story maps still work."""
    assert tts_providers.parse_voice_id("en-US-AvaNeural") == (
        "edge", "en-US-AvaNeural",
    )


def test_parse_voice_id_handles_empty():
    assert tts_providers.parse_voice_id("") == ("edge", "")


# ── VoiceInfo dataclass ───────────────────────────────────────────


def test_voiceinfo_language_extracted_from_locale():
    v = tts_providers.VoiceInfo(
        id="edge:en-GB-RyanNeural", provider="edge",
        short_name="en-GB-RyanNeural", locale="en-GB",
        gender="Male", display="Ryan",
    )
    assert v.language == "en"


def test_voiceinfo_handles_locale_without_region():
    v = tts_providers.VoiceInfo(
        id="piper:fr-pierre", provider="piper",
        short_name="fr-pierre", locale="fr",
        gender="Male", display="Pierre",
    )
    assert v.language == "fr"


# ── Registry / dispatch ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    """Force the registry to rebuild between tests so a stub from one
    test can't leak into another."""
    monkeypatch.setattr(tts_providers, "_REGISTRY", {})
    monkeypatch.setattr(tts_providers, "_REGISTRY_BUILT", False)
    yield


class _FakeProvider:
    def __init__(self, name, voices, installed=True):
        self.name = name
        self._voices = voices
        self._installed = installed
        self.synth_calls = []

    def is_installed(self):
        return self._installed

    def list_voices(self):
        return list(self._voices)

    def synthesize(self, *, text, voice, output_path, rate=None,
                   volume=None, pitch=None):
        self.synth_calls.append({
            "text": text, "voice": voice,
            "output_path": output_path,
            "rate": rate, "volume": volume, "pitch": pitch,
        })


def _install_fake(monkeypatch, name, voices, installed=True):
    """Insert a stub provider directly into the registry."""
    p = _FakeProvider(name, voices, installed=installed)
    monkeypatch.setitem(tts_providers._REGISTRY, name, p)
    monkeypatch.setattr(tts_providers, "_REGISTRY_BUILT", True)
    return p


def _voice(provider, short, locale, gender):
    return tts_providers.VoiceInfo(
        id=f"{provider}:{short}", provider=provider,
        short_name=short, locale=locale, gender=gender,
        display=f"{short} ({locale})",
    )


def test_all_voices_aggregates_across_providers(monkeypatch):
    _install_fake(monkeypatch, "edge", [
        _voice("edge", "en-US-Ava", "en-US", "Female"),
    ])
    _install_fake(monkeypatch, "piper", [
        _voice("piper", "en_GB-alan", "en-GB", "Male"),
    ])
    out = tts_providers.all_voices()
    assert {v.id for v in out} == {
        "edge:en-US-Ava", "piper:en_GB-alan",
    }


def test_all_voices_skips_uninstalled(monkeypatch):
    _install_fake(monkeypatch, "edge", [_voice("edge", "x", "en-US", "Female")])
    _install_fake(
        monkeypatch, "piper", [_voice("piper", "y", "en-GB", "Male")],
        installed=False,
    )
    ids = {v.id for v in tts_providers.all_voices()}
    assert ids == {"edge:x"}


def test_all_voices_explicit_provider_list(monkeypatch):
    _install_fake(monkeypatch, "edge", [_voice("edge", "x", "en-US", "F")])
    _install_fake(monkeypatch, "piper", [_voice("piper", "y", "en-GB", "M")])
    ids = {v.id for v in tts_providers.all_voices(providers=["piper"])}
    assert ids == {"piper:y"}


def test_voice_by_id_resolves_namespaced(monkeypatch):
    _install_fake(monkeypatch, "edge", [
        _voice("edge", "en-US-Ava", "en-US", "Female"),
    ])
    v = tts_providers.voice_by_id("edge:en-US-Ava")
    assert v is not None
    assert v.locale == "en-US"


def test_voice_by_id_legacy_bare_name(monkeypatch):
    _install_fake(monkeypatch, "edge", [
        _voice("edge", "en-US-Ava", "en-US", "Female"),
    ])
    v = tts_providers.voice_by_id("en-US-Ava")
    assert v is not None
    assert v.provider == "edge"


def test_synthesize_dispatches_to_provider(monkeypatch, tmp_path):
    fake = _install_fake(
        monkeypatch, "edge",
        [_voice("edge", "en-US-Ava", "en-US", "Female")],
    )
    out = tmp_path / "x.mp3"
    tts_providers.synthesize(
        "edge:en-US-Ava", "Hello", out, rate="+5%",
    )
    assert len(fake.synth_calls) == 1
    call = fake.synth_calls[0]
    assert call["voice"] == "en-US-Ava"
    assert call["text"] == "Hello"
    assert call["output_path"] == out
    assert call["rate"] == "+5%"


def test_synthesize_legacy_bare_name_routes_to_edge(monkeypatch, tmp_path):
    fake = _install_fake(
        monkeypatch, "edge",
        [_voice("edge", "en-US-Ava", "en-US", "Female")],
    )
    tts_providers.synthesize("en-US-Ava", "Hello", tmp_path / "out.mp3")
    assert fake.synth_calls and fake.synth_calls[0]["voice"] == "en-US-Ava"


def test_synthesize_raises_on_uninstalled_provider(monkeypatch, tmp_path):
    _install_fake(
        monkeypatch, "edge",
        [_voice("edge", "x", "en-US", "F")], installed=False,
    )
    with pytest.raises(RuntimeError, match="not installed"):
        tts_providers.synthesize("edge:x", "Hi", tmp_path / "x.mp3")


# ── Voice pool builder + VoiceMapper ──────────────────────────────


def _seed_pool_test_catalog(monkeypatch):
    """Mixed catalog spanning two providers, two locales, both genders."""
    _install_fake(monkeypatch, "edge", [
        _voice("edge", "en-US-Ava", "en-US", "Female"),
        _voice("edge", "en-US-Guy", "en-US", "Male"),
        _voice("edge", "en-GB-Sonia", "en-GB", "Female"),
        _voice("edge", "en-GB-Ryan", "en-GB", "Male"),
        _voice("edge", "fr-FR-Henri", "fr-FR", "Male"),
    ])
    _install_fake(monkeypatch, "piper", [
        _voice("piper", "en_GB-alan", "en-GB", "Male"),
        _voice("piper", "fr_FR-tom", "fr-FR", "Male"),
    ])


def test_build_voice_pool_filters_by_accent_and_gender(monkeypatch):
    from ffn_dl.tts import _build_voice_pool

    _seed_pool_test_catalog(monkeypatch)
    pool = _build_voice_pool(
        characters=["Hagrid"],
        genders={"Hagrid": "male"},
        profiles={},
        accents={"Hagrid": "en-GB"},
        enabled_providers=None,
        narrator_voice="en-US-AriaNeural",
    )
    # Hagrid is male + en-GB → exactly two voices match (one edge, one piper).
    assert pool["Hagrid"] == ["edge:en-GB-Ryan", "piper:en_GB-alan"]


def test_build_voice_pool_falls_back_when_accent_has_no_matches(monkeypatch):
    from ffn_dl.tts import _build_voice_pool

    _seed_pool_test_catalog(monkeypatch)
    pool = _build_voice_pool(
        characters=["Klingon"],
        genders={"Klingon": "male"},
        profiles={},
        accents={"Klingon": "kl-KL"},  # no matching locale
        enabled_providers=None,
        narrator_voice="en-US-AriaNeural",
    )
    # All male voices should be in the pool — accent filter relaxed.
    assert pool["Klingon"]
    for vid in pool["Klingon"]:
        assert ":" in vid


def test_build_voice_pool_uses_profile_accent_when_no_explicit_accent(monkeypatch):
    from ffn_dl.tts import _build_voice_pool

    _seed_pool_test_catalog(monkeypatch)
    pool = _build_voice_pool(
        characters=["Fleur"],
        genders={"Fleur": "female"},
        profiles={"Fleur": {"accent": "fr-FR", "gender": "female"}},
        accents={},
        enabled_providers=None,
        narrator_voice="en-US-AriaNeural",
    )
    # No fr-FR female in catalog → falls back to all-female pool, but
    # the call must still produce some voices, not crash.
    assert pool.get("Fleur"), "Expected fallback pool, got empty"


def test_build_voice_pool_excludes_narrator_voice(monkeypatch):
    from ffn_dl.tts import _build_voice_pool

    _seed_pool_test_catalog(monkeypatch)
    pool = _build_voice_pool(
        characters=["Hermione"],
        genders={"Hermione": "female"},
        profiles={},
        accents={},
        enabled_providers=None,
        narrator_voice="en-US-AriaNeural",  # not in catalog, no-op effectively
    )
    assert pool["Hermione"]
    # Use Ava as the narrator and confirm she drops out.
    pool2 = _build_voice_pool(
        characters=["Hermione"],
        genders={"Hermione": "female"},
        profiles={},
        accents={},
        enabled_providers=None,
        narrator_voice="en-US-Ava",
    )
    assert "edge:en-US-Ava" not in pool2["Hermione"]


def test_build_voice_pool_respects_enabled_providers(monkeypatch):
    from ffn_dl.tts import _build_voice_pool

    _seed_pool_test_catalog(monkeypatch)
    pool = _build_voice_pool(
        characters=["Hagrid"],
        genders={"Hagrid": "male"},
        profiles={},
        accents={"Hagrid": "en-GB"},
        enabled_providers=["piper"],  # edge excluded
        narrator_voice="en-US-AriaNeural",
    )
    assert pool["Hagrid"] == ["piper:en_GB-alan"]


def test_voice_mapper_legacy_bare_names_get_namespaced(tmp_path):
    """A pre-2.2.0 voice map JSON has bare ``"en-US-AvaNeural"`` keys.
    Loading must auto-namespace them to ``edge:en-US-AvaNeural`` so
    the provider dispatcher can resolve them."""
    from ffn_dl.tts import VoiceMapper

    map_path = tmp_path / "voices.json"
    map_path.write_text('{"Harry": "en-US-Christopher"}', encoding="utf-8")
    mapper = VoiceMapper(map_path)
    assert mapper.mapping["Harry"] == "edge:en-US-Christopher"


def test_voice_mapper_set_voice_pool_rotates(tmp_path):
    from ffn_dl.tts import VoiceMapper

    mapper = VoiceMapper(tmp_path / "voices.json")
    mapper.set_voice_pool({
        "Hagrid": ["edge:en-GB-Ryan", "piper:en_GB-alan"],
    })
    # First assign → first pool entry.
    assert mapper.assign("Hagrid", "male") == "edge:en-GB-Ryan"


def test_voice_mapper_get_returns_namespaced_narrator(tmp_path):
    from ffn_dl.tts import VoiceMapper, NARRATOR_VOICE

    mapper = VoiceMapper(tmp_path / "voices.json")
    # Unmapped name falls through to narrator, namespaced.
    fallback = mapper.get("Unknown")
    assert fallback == f"edge:{NARRATOR_VOICE}"


# ── Accent map JSON ───────────────────────────────────────────────


def test_accent_map_round_trip(tmp_path):
    p = tmp_path / "accents.json"
    accent_map.save_accents(p, {"Harry Potter": "en-GB", "Fleur": "fr-FR"})
    loaded = accent_map.load_accents(p)
    user = accent_map.filter_user_entries(loaded)
    assert user == {"Harry Potter": "en-GB", "Fleur": "fr-FR"}


def test_accent_map_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "accents.json"
    p.write_text("not json", encoding="utf-8")
    assert accent_map.load_accents(p) == {}


def test_accent_map_load_missing_returns_empty(tmp_path):
    assert accent_map.load_accents(tmp_path / "nope.json") == {}


def test_profile_round_trip(tmp_path):
    p = tmp_path / "profiles.json"
    accent_map.save_profiles(p, {
        "Harry Potter": {
            "gender": "male", "age": "teen",
            "accent": "en-GB", "tone": "earnest",
        },
    })
    loaded = accent_map.load_profiles(p)
    user = accent_map.filter_user_entries(loaded)
    assert user["Harry Potter"]["accent"] == "en-GB"


# ── Character profile (LLM) ───────────────────────────────────────


def test_seed_profiles_via_llm_returns_empty_when_no_config():
    out = character_profile.seed_profiles_via_llm(
        character_list=["Harry"], full_text="text", llm_config=None,
    )
    assert out == {}


def test_seed_profiles_via_llm_calls_provider(monkeypatch):
    """End-to-end through the seeder with a stubbed HTTP layer.
    Verifies the prompt includes the cast list and the response
    parses into normalised profile dicts."""
    from ffn_dl import attribution

    def fake_call(*, user_prompt, **_kw):
        assert "Harry Potter" in user_prompt
        return (
            '{"Harry Potter": {"gender": "male", "age": "teen", '
            '"accent": "en-GB", "tone": "earnest"}, '
            '"Fleur Delacour": {"gender": "female", "age": "young adult", '
            '"accent": "fr-FR", "tone": "soft melodic"}}'
        )

    monkeypatch.setattr(attribution, "_llm_call", fake_call)
    out = character_profile.seed_profiles_via_llm(
        character_list=["Harry Potter", "Fleur Delacour"],
        full_text="Some story text.",
        llm_config={
            "provider": "ollama", "model": "llama3.1:8b",
            "api_key": "", "endpoint": "",
        },
    )
    assert out["Harry Potter"]["accent"] == "en-GB"
    assert out["Fleur Delacour"]["gender"] == "female"


def test_profile_response_drops_hallucinated_names(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call",
        lambda **_kw: '{"Harry": {"gender": "male"}, "Phantom OC": {"gender": "female"}}',
    )
    out = character_profile.seed_profiles_via_llm(
        character_list=["Harry Potter"],
        full_text="text",
        llm_config={"provider": "ollama", "model": "llama3.1:8b",
                    "api_key": "", "endpoint": ""},
    )
    # "Harry" matches "Harry Potter" via first-name fallback;
    # "Phantom OC" is not in the cast list and gets dropped.
    assert "Harry Potter" in out
    assert "Phantom OC" not in out


def test_profile_handles_unknown_age_gracefully(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call",
        lambda **_kw: '{"Harry Potter": {"age": "ancient", "gender": "M"}}',
    )
    out = character_profile.seed_profiles_via_llm(
        character_list=["Harry Potter"],
        full_text="text",
        llm_config={"provider": "ollama", "model": "x", "api_key": "", "endpoint": ""},
    )
    # Unknown age → falls back to "adult".
    assert out["Harry Potter"]["age"] == "adult"
    assert out["Harry Potter"]["gender"] == "male"


def test_derive_accents_skips_any():
    profiles = {
        "Harry": {"accent": "en-GB"},
        "Vague": {"accent": "any"},
        "Empty": {},
    }
    out = character_profile.derive_accents_from_profiles(profiles)
    assert out == {"Harry": "en-GB"}


# ── A/N classifier ────────────────────────────────────────────────


def test_classify_authors_notes_via_llm_returns_empty_with_no_config():
    from ffn_dl import attribution

    out = attribution.classify_authors_notes_via_llm(
        ["A paragraph."], llm_config=None,
    )
    assert out == set()


def test_classify_authors_notes_via_llm_parses_flags(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call",
        lambda **_kw: '{"1": false, "2": true, "3": false}',
    )
    flags = attribution.classify_authors_notes_via_llm(
        ["Story prose.", "A/N: thanks!", "More story."],
        llm_config={"provider": "ollama", "model": "llama3.1:8b",
                    "api_key": "", "endpoint": ""},
    )
    assert flags == {1}  # 0-based -> paragraph index 1


def test_classify_authors_notes_via_llm_ignores_out_of_range(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call",
        lambda **_kw: '{"99": true}',
    )
    flags = attribution.classify_authors_notes_via_llm(
        ["only one paragraph"],
        llm_config={"provider": "ollama", "model": "x", "api_key": "", "endpoint": ""},
    )
    assert flags == set()


# ── Piper voice manifest ──────────────────────────────────────────


def test_piper_provider_lists_full_manifest(monkeypatch):
    """Piper.list_voices reflects the curated manifest regardless of
    whether any voice file is on disk — the catalog drives the GUI's
    "click Install to download" surface."""
    from ffn_dl.tts_providers import piper as _piper

    provider = _piper.PiperProvider()
    voices = provider.list_voices()
    assert len(voices) >= 10
    # Spot-check the locales the curated list claims to cover.
    locales = {v.locale for v in voices}
    assert "en-GB" in locales
    assert "fr-FR" in locales


def test_piper_voice_files_paths_match_short_name(tmp_path, monkeypatch):
    from ffn_dl.tts_providers import piper as _piper

    monkeypatch.setattr(_piper, "piper_models_dir", lambda: tmp_path)
    onnx, cfg = _piper._voice_files("en_GB-alan-medium")
    assert onnx == tmp_path / "en_GB-alan-medium.onnx"
    assert cfg == tmp_path / "en_GB-alan-medium.onnx.json"


def test_piper_voice_is_downloaded_requires_both_files(tmp_path, monkeypatch):
    from ffn_dl.tts_providers import piper as _piper

    monkeypatch.setattr(_piper, "piper_models_dir", lambda: tmp_path)
    short = "en_GB-alan-medium"
    onnx, cfg = _piper._voice_files(short)
    onnx.parent.mkdir(parents=True, exist_ok=True)
    onnx.write_bytes(b"\0" * 2048)
    assert not _piper.voice_is_downloaded(short)
    cfg.write_text("{}", encoding="utf-8")
    assert _piper.voice_is_downloaded(short)


# ── Pronunciation seeder ──────────────────────────────────────────


def test_seed_pronunciations_returns_empty_with_no_config():
    out = character_profile.seed_pronunciations_via_llm(
        character_list=["Hermione"], full_text="text", llm_config=None,
    )
    assert out == {}


def test_seed_pronunciations_parses_json_response(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call",
        lambda **_kw: (
            '{"Hermione": "Her-MY-oh-nee", '
            '"Quidditch": "KWID-itch", "Harry": "Harry"}'
        ),
    )
    out = character_profile.seed_pronunciations_via_llm(
        character_list=["Hermione Granger", "Harry Potter"],
        full_text="story",
        llm_config={"provider": "ollama", "model": "llama3.1:8b",
                    "api_key": "", "endpoint": ""},
    )
    # Identity entries are dropped — they pollute the file without
    # changing anything downstream.
    assert "Harry" not in out
    assert out["Hermione"] == "Her-MY-oh-nee"
    assert out["Quidditch"] == "KWID-itch"


def test_seed_pronunciations_handles_garbage_reply(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call", lambda **_kw: "I cannot help with that.",
    )
    out = character_profile.seed_pronunciations_via_llm(
        character_list=["Hermione"], full_text="story",
        llm_config={"provider": "ollama", "model": "x",
                    "api_key": "", "endpoint": ""},
    )
    assert out == {}


# ── Narrator suggestion ───────────────────────────────────────────


def test_suggest_narrator_returns_profile(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call",
        lambda **_kw: (
            '{"gender": "male", "accent": "en-GB", '
            '"tone": "warm storyteller", '
            '"rationale": "British-coded Hogwarts setting"}'
        ),
    )
    profile = character_profile.suggest_narrator_via_llm(
        full_text="The great hall fell silent.",
        llm_config={"provider": "ollama", "model": "x",
                    "api_key": "", "endpoint": ""},
    )
    assert profile["gender"] == "male"
    assert profile["accent"] == "en-GB"
    assert profile["tone"]


def test_suggest_narrator_returns_none_on_failure(monkeypatch):
    from ffn_dl import attribution

    def boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(attribution, "_llm_call", boom)
    profile = character_profile.suggest_narrator_via_llm(
        full_text="text",
        llm_config={"provider": "ollama", "model": "x",
                    "api_key": "", "endpoint": ""},
    )
    assert profile is None


def test_pick_narrator_voice_filters_catalog(monkeypatch):
    _seed_pool_test_catalog(monkeypatch)
    voice = character_profile.pick_narrator_voice_for_profile(
        profile={"gender": "male", "accent": "en-GB", "tone": ""},
        enabled_providers=None,
        fallback="edge:en-US-AriaNeural",
    )
    assert voice in {"edge:en-GB-Ryan", "piper:en_GB-alan"}


def test_pick_narrator_voice_falls_back_when_no_match(monkeypatch):
    _seed_pool_test_catalog(monkeypatch)
    voice = character_profile.pick_narrator_voice_for_profile(
        profile={"gender": "female", "accent": "ja-JP", "tone": ""},
        enabled_providers=None,
        fallback="edge:en-US-AriaNeural",
    )
    assert voice == "edge:en-US-AriaNeural"


def test_pick_narrator_voice_handles_no_profile():
    voice = character_profile.pick_narrator_voice_for_profile(
        profile=None,
        enabled_providers=None,
        fallback="edge:en-US-AriaNeural",
    )
    assert voice == "edge:en-US-AriaNeural"


# ── Unified per-story analysis ────────────────────────────────────


def test_analyze_story_via_llm_returns_empty_shape_on_no_config():
    out = character_profile.analyze_story_via_llm(
        character_list=["Harry"], full_text="text", llm_config=None,
    )
    # Always returns the three keys so callers can destructure
    # without re-checking — empty values stand in for "no data".
    assert out == {"profiles": {}, "pronunciations": {}, "narrator": None}


def test_analyze_story_via_llm_parses_combined_reply(monkeypatch):
    """One round-trip should yield profiles + pronunciations +
    narrator from a single JSON object, each section running through
    the same normalisers as the legacy split helpers."""
    from ffn_dl import attribution

    captured: dict = {}

    def fake_call(*, system_prompt, user_prompt, **_kw):
        captured["system_prompt"] = system_prompt
        captured["user_prompt"] = user_prompt
        return (
            '{"profiles": {"Harry Potter": {"gender": "male", '
            '"age": "teen", "accent": "en-GB", "tone": "earnest"}}, '
            '"pronunciations": {"Hermione": "Her-MY-oh-nee", '
            '"Harry": "Harry"}, '
            '"narrator": {"gender": "male", "accent": "en-GB", '
            '"tone": "warm storyteller", "rationale": "British canon"}}'
        )

    monkeypatch.setattr(attribution, "_llm_call", fake_call)
    out = character_profile.analyze_story_via_llm(
        character_list=["Harry Potter"],
        full_text="Story excerpt",
        llm_config={"provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "api_key": "k", "endpoint": ""},
    )
    assert out["profiles"]["Harry Potter"]["accent"] == "en-GB"
    # Identity entry dropped, just like the legacy parser.
    assert "Harry" not in out["pronunciations"]
    assert out["pronunciations"]["Hermione"] == "Her-MY-oh-nee"
    assert out["narrator"]["accent"] == "en-GB"
    # The cast list and excerpt land in a single user prompt.
    assert "Harry Potter" in captured["user_prompt"]
    assert "Story excerpt" in captured["user_prompt"]


def test_analyze_story_via_llm_returns_empty_shape_on_garbage(monkeypatch):
    from ffn_dl import attribution

    monkeypatch.setattr(
        attribution, "_llm_call", lambda **_kw: "not json",
    )
    out = character_profile.analyze_story_via_llm(
        character_list=["Harry"],
        full_text="text",
        llm_config={"provider": "ollama", "model": "llama3.1:8b",
                    "api_key": "", "endpoint": ""},
    )
    assert out == {"profiles": {}, "pronunciations": {}, "narrator": None}


def test_analyze_story_via_llm_returns_empty_shape_on_transport_error(monkeypatch):
    from ffn_dl import attribution

    def boom(**_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(attribution, "_llm_call", boom)
    out = character_profile.analyze_story_via_llm(
        character_list=["Harry"],
        full_text="text",
        llm_config={"provider": "ollama", "model": "x",
                    "api_key": "", "endpoint": ""},
    )
    assert out == {"profiles": {}, "pronunciations": {}, "narrator": None}


# ── Per-model limits ──────────────────────────────────────────────


def test_model_limits_known_anthropic_models():
    """The lookup table drives Anthropic ``max_tokens`` budget.
    Sonnet 4.6 must report >4096 so big A/N batches don't truncate."""
    from ffn_dl import attribution

    _ctx, sonnet_out = attribution._model_limits("claude-sonnet-4-6")
    assert sonnet_out > 4096
    _ctx, opus_out = attribution._model_limits("claude-opus-4-7")
    assert opus_out > 4096


def test_model_limits_unknown_model_falls_back():
    from ffn_dl import attribution

    ctx, out = attribution._model_limits("some-random-future-model")
    assert ctx == attribution._DEFAULT_CONTEXT_TOKENS
    assert out == attribution._DEFAULT_MAX_OUTPUT_TOKENS


def test_max_output_tokens_for_model_floors_at_4096():
    from ffn_dl import attribution

    # Even an empty-string model must return a usable budget.
    assert attribution._max_output_tokens_for_model("") >= 4096
    # Known models bump it.
    assert attribution._max_output_tokens_for_model(
        "claude-sonnet-4-6"
    ) > 4096


# ── Provider-aware chunk / batch sizes ────────────────────────────


def test_chunk_chars_for_provider_cloud_vs_local():
    from ffn_dl import attribution

    cloud = attribution._chunk_chars_for_provider("anthropic")
    local = attribution._chunk_chars_for_provider("ollama")
    assert cloud > local


def test_an_batch_size_for_provider_cloud_vs_local():
    from ffn_dl import attribution

    cloud = attribution._an_batch_size_for_provider("openai")
    local = attribution._an_batch_size_for_provider("ollama")
    assert cloud > local


def test_piper_length_scale_from_rate():
    from ffn_dl.tts_providers import piper as _piper

    assert _piper._length_scale_from_rate(None) == 1.0
    # +20% rate → 0.8 length_scale (faster speech)
    assert abs(_piper._length_scale_from_rate("+20%") - 0.8) < 1e-6
    # -10% → 1.1 length_scale (slower)
    assert abs(_piper._length_scale_from_rate("-10%") - 1.1) < 1e-6
    # Garbage falls back to identity.
    assert _piper._length_scale_from_rate("bogus") == 1.0
