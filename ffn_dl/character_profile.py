"""LLM-driven character analysis for richer voice mapping.

When the LLM attribution backend is enabled, ffn-dl runs a single
extra round-trip per story to classify every detected speaker into a
profile dict the audiobook generator uses as a prior:

    {
      "Harry Potter": {"gender": "male", "age": "teen",
                       "accent": "en-GB", "tone": "earnest"},
      "Hagrid":       {"gender": "male", "age": "elder",
                       "accent": "en-GB", "tone": "warm rural"},
      ...
    }

The profile feeds VoiceMapper's per-character voice pool — combined
with the user's enabled TTS providers, the cast list, and the
detected gender we pick voices that match each character's age /
accent / tone rather than just their gender.

This module reuses ``ffn_dl.attribution._llm_call`` for transport so
the same provider config (Ollama / OpenAI / Anthropic / OpenAI-
compatible) drives both attribution and profiling — there's no second
config surface for the user to fill in.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

__all__ = [
    "derive_accents_from_profiles",
    "pick_narrator_voice_for_profile",
    "analyze_story_via_llm",
]


# Cap how many chars of the story we feed into the profile prompt.
# Profiling doesn't need every chapter — the first ~40 KB of text plus
# the cast list is plenty for a model to identify canon characters and
# infer accents from in-text dialogue cues. Larger feeds wouldn't
# improve accuracy and would burn tokens on cloud providers.
_PROFILE_SAMPLE_CHARS = 40000


def _truncate_sample(full_text: str) -> str:
    if len(full_text) <= _PROFILE_SAMPLE_CHARS:
        return full_text
    # Take the head of the text — fanfic exposition (where canon
    # characters get introduced) sits at the front.
    return full_text[:_PROFILE_SAMPLE_CHARS]


def _resolve_name(raw: object, cl_lower: dict[str, str]) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    low = raw.strip().lower()
    if low in cl_lower:
        return cl_lower[low]
    # Allow first-name match (model returns "Harry" against "Harry Potter").
    for full_low, full in cl_lower.items():
        if low == full_low.split()[0]:
            return full
    return None


def _clean_gender(raw: object) -> str:
    if not isinstance(raw, str):
        return "neutral"
    low = raw.strip().lower()
    if low in {"male", "m", "man"}:
        return "male"
    if low in {"female", "f", "woman"}:
        return "female"
    return "neutral"


_AGE_VALUES = {"child", "teen", "young adult", "adult", "elder"}


def _clean_age(raw: object) -> str:
    if not isinstance(raw, str):
        return "adult"
    low = raw.strip().lower()
    if low in _AGE_VALUES:
        return low
    if low in {"old", "elderly", "senior"}:
        return "elder"
    if low in {"young", "youth", "youngster"}:
        return "young adult"
    if low in {"kid", "small child", "infant", "toddler"}:
        return "child"
    return "adult"


_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")


def _clean_accent(raw: object) -> str:
    if not isinstance(raw, str):
        return "any"
    val = raw.strip()
    if val.lower() in {"any", "", "unknown", "none"}:
        return "any"
    if _LOCALE_RE.match(val):
        # Normalise to xx-XX casing.
        if "-" in val:
            lang, region = val.split("-", 1)
            return f"{lang.lower()}-{region.upper()}"
        return val.lower()
    return "any"


def derive_accents_from_profiles(profiles: dict[str, dict]) -> dict[str, str]:
    """Pull just the accent field out of every profile, dropping
    ``"any"`` so the user's accent map stays small. Used to seed
    ``.ffn-accents-*.json`` from a freshly-computed profile dict."""
    out: dict[str, str] = {}
    for name, profile in profiles.items():
        accent = (profile or {}).get("accent")
        if isinstance(accent, str) and accent and accent != "any":
            out[name] = accent
    return out


def pick_narrator_voice_for_profile(
    *, profile: dict | None,
    enabled_providers: list[str] | None,
    fallback: str,
) -> str:
    """Translate a narrator profile into an actual voice id by
    filtering the live provider catalog.

    Returns the namespaced voice id (``edge:en-GB-RyanNeural`` or
    similar). On no match the legacy ``fallback`` is returned
    unchanged so audiobook rendering never blocks on this — narration
    has to keep working even if the LLM picks an accent we can't
    fulfil."""
    if not profile:
        return fallback
    try:
        from . import tts_providers
    except ImportError:
        return fallback
    catalog = tts_providers.all_voices(providers=enabled_providers)
    if not catalog:
        return fallback
    target_gender = (profile.get("gender") or "neutral").lower()
    target_accent = (profile.get("accent") or "any").lower()
    target_lang = target_accent.split("-", 1)[0] if "-" in target_accent else target_accent
    for v in catalog:
        if target_gender in ("male", "female") and v.gender.lower() != target_gender:
            continue
        if target_accent in ("any", "") or v.locale.lower() == target_accent:
            return v.id
    # Locale fallback to language match.
    for v in catalog:
        if target_gender in ("male", "female") and v.gender.lower() != target_gender:
            continue
        if v.language.lower() == target_lang:
            return v.id
    return fallback


# ── Unified per-story analysis ────────────────────────────────────


_UNIFIED_SYSTEM_PROMPT = (
    "You are an expert at preparing fanfiction for audiobook "
    "narration. From the story excerpt and character list provided, "
    "produce three analyses in a single JSON response.\n\n"
    "1. 'profiles': for each character name listed, infer "
    "{gender: 'male'|'female'|'neutral', "
    "age: 'child'|'teen'|'young adult'|'adult'|'elder', "
    "accent: BCP-47 locale code (e.g. 'en-GB' for most Hogwarts "
    "characters, 'en-GB' for Hagrid (West Country), 'fr-FR' for "
    "Fleur Delacour, 'en-US' for American characters, 'en-IE' for "
    "Irish, 'en-IN' for Indian, 'en-AU' for Australian — or 'any' "
    "if you genuinely have no signal), "
    "tone: short free-form descriptor like 'warm rural', 'crisp "
    "posh', 'gruff', 'sardonic'}.\n\n"
    "2. 'pronunciations': map of original spelling -> phonetic "
    "respelling for words a TTS engine will mangle (made-up "
    "character names like 'Hermione' or 'Daenerys', fandom terms "
    "like 'Quidditch' or 'Avada Kedavra', foreign loanwords, place "
    "names). Use plain English letters with hyphens between "
    "syllables; capitalize the stressed syllable; do NOT use IPA. "
    "Skip ordinary English words and obvious names any TTS would "
    "handle (Harry, Ron, Tom, John). Cap at 25 entries.\n\n"
    "3. 'narrator': single object {gender, accent, tone, "
    "rationale: '<one-sentence why>'} recommending a narrator voice "
    "that fits the story's tone (dark / cozy / dramatic / "
    "lighthearted / literary / pulpy / etc.). Default to 'en-GB' "
    "for British-coded fandoms (Harry Potter, Sherlock, Doctor "
    "Who) and 'en-US' otherwise unless the text clearly points "
    "elsewhere.\n\n"
    "Respond with ONLY a single JSON object with exactly three "
    "top-level keys, no extra fields:\n"
    '{"profiles": {...}, "pronunciations": {...}, "narrator": {...}}.'
)


def _empty_analysis() -> dict:
    """Shape callers can structurally destructure even on failure —
    saves every call site from re-checking for None / KeyError."""
    return {"profiles": {}, "pronunciations": {}, "narrator": None}


def analyze_story_via_llm(
    *, character_list: list[str],
    full_text: str,
    llm_config: dict,
) -> dict:
    """One LLM round-trip producing profile + pronunciation +
    narrator analysis. Returns:

        {
            "profiles":       {<name>: {gender, age, accent, tone}, ...},
            "pronunciations": {<word>: <phonetic respelling>, ...},
            "narrator":       {gender, accent, tone, rationale}
                              | None,
        }

    All three sections share the same 40 KB story excerpt and
    character list, so a single call covers all three rather than
    paying for three duplicate excerpts on Anthropic/OpenAI.

    Returns :func:`_empty_analysis` on any failure so callers can
    treat the result as load-bearing without guarding every key.
    """
    if not llm_config:
        return _empty_analysis()
    try:
        from .attribution import _llm_call, _llm_normalize_endpoint
    except ImportError:
        return _empty_analysis()

    provider = llm_config.get("provider", "")
    model = llm_config.get("model", "")
    if not provider or not model:
        return _empty_analysis()
    endpoint = _llm_normalize_endpoint(provider, llm_config.get("endpoint"))

    sample = _truncate_sample(full_text)
    cast_lines = "\n".join(f"- {n}" for n in character_list) or "(none)"
    user_prompt = (
        "Character list (use these names exactly as keys in "
        "'profiles'; also treat them as candidates for "
        "'pronunciations'):\n"
        + cast_lines
        + "\n\nStory excerpt (read for canon-character cues, "
        "fandom terms, place names, foreign words, and overall "
        "tone):\n"
        + sample
        + "\n\nReturn JSON only."
    )
    try:
        reply = _llm_call(
            provider=provider, model=model,
            api_key=llm_config.get("api_key"),
            endpoint=endpoint,
            system_prompt=_UNIFIED_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
    except Exception as exc:  # noqa: BLE001 — never fail the render
        logger.warning("LLM unified story analysis failed: %s", exc)
        return _empty_analysis()
    return _parse_unified_response(reply, character_list)


def _parse_unified_response(reply: str, character_list: list[str]) -> dict:
    """Pull the three sections out of the unified reply, applying the
    same per-section normalisers as the legacy split helpers so the
    output is byte-for-byte compatible with what they used to return.

    Tolerant of fenced ``` code blocks and pre/post chatter — same
    extraction strategy as the per-section parsers."""
    if not reply:
        return _empty_analysis()
    text = reply.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace <= first_brace:
        return _empty_analysis()
    blob = text[first_brace : last_brace + 1]
    try:
        parsed = json.loads(blob)
    except (ValueError, json.JSONDecodeError):
        return _empty_analysis()
    if not isinstance(parsed, dict):
        return _empty_analysis()

    profiles_raw = parsed.get("profiles") or {}
    pron_raw = parsed.get("pronunciations") or {}
    narrator_raw = parsed.get("narrator")

    profiles = (
        _profiles_from_parsed(profiles_raw, character_list)
        if isinstance(profiles_raw, dict) else {}
    )
    pronunciations = (
        _pronunciations_from_parsed(pron_raw)
        if isinstance(pron_raw, dict) else {}
    )
    narrator = (
        _narrator_from_parsed(narrator_raw)
        if isinstance(narrator_raw, dict) else None
    )
    return {
        "profiles": profiles,
        "pronunciations": pronunciations,
        "narrator": narrator,
    }


def _profiles_from_parsed(
    raw: dict, character_list: list[str],
) -> dict[str, dict]:
    """Validate the ``profiles`` section of the unified reply.

    Drops entries with non-dict values, hallucinated names not in
    ``character_list``, and out-of-range gender/age/accent values
    (each per-field cleaner returns a safe default)."""
    cl_lower = {c.lower(): c for c in character_list}
    out: dict[str, dict] = {}
    for raw_name, raw_attrs in raw.items():
        if not isinstance(raw_attrs, dict):
            continue
        canonical = _resolve_name(raw_name, cl_lower)
        if canonical is None:
            continue
        tone = (raw_attrs.get("tone") or "").strip() if isinstance(
            raw_attrs.get("tone"), str
        ) else ""
        out[canonical] = {
            "gender": _clean_gender(raw_attrs.get("gender")),
            "age": _clean_age(raw_attrs.get("age")),
            "accent": _clean_accent(raw_attrs.get("accent")),
            "tone": tone,
        }
    return out


def _pronunciations_from_parsed(raw: dict) -> dict[str, str]:
    """Validate the ``pronunciations`` section of the unified reply.

    Drops non-string keys/values and identity entries
    (``"Harry" -> "Harry"``) since they pollute the override map
    without changing TTS output."""
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = k.strip()
        val = v.strip()
        if not key or not val or key == val:
            continue
        out[key] = val
    return out


def _narrator_from_parsed(raw: dict) -> dict:
    """Validate the ``narrator`` section of the unified reply.

    Returns a profile with cleaned gender/accent and string-checked
    tone/rationale. Caller decides whether the resulting dict is
    actionable."""
    return {
        "gender": _clean_gender(raw.get("gender")),
        "accent": _clean_accent(raw.get("accent")),
        "tone": (raw.get("tone") or "").strip()
        if isinstance(raw.get("tone"), str) else "",
        "rationale": (raw.get("rationale") or "").strip()
        if isinstance(raw.get("rationale"), str) else "",
    }
