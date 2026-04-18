"""Regression tests for dialogue parsing and speaker attribution in tts.py.

These lock in behaviors that have broken in subtle ways in the past —
especially making sure the narrator reads character names aloud so a
listener knows who is speaking, and that titled/camelcase/unicode names
don't fall through the cracks.
"""
from collections import Counter

import pytest

from ffn_dl.tts import (
    _apply_pronunciation_map,
    _guess_gender_from_name,
    _load_pronunciation_map,
    consolidate_speakers,
    parse_segments,
)


def _speakers(text):
    return [s.speaker for s in parse_segments(text)]


def _narration_joined(text):
    return " ".join(s.text for s in parse_segments(text) if s.speaker is None)


def _speaking(text, name):
    """All speech segments spoken by `name`."""
    return [s.text for s in parse_segments(text) if s.speaker == name]


# ── attribution + narration preservation ────────────────────────────


def test_after_attribution_kept_in_narration():
    """Must hear 'Harry said' so listener knows who spoke."""
    text = '"Hello!" Harry said.'
    assert "Harry" in _speakers(text)
    assert "Harry said" in _narration_joined(text)


def test_before_attribution_kept_in_narration():
    text = 'Harry said, "Hello!"'
    assert "Harry" in _speakers(text)
    assert "Harry said" in _narration_joined(text)


def test_verb_first_attribution():
    text = '"Hello!" said Harry.'
    assert "Harry" in _speakers(text)
    assert "said Harry" in _narration_joined(text)


# ── camelcase / apostrophe / titled names ───────────────────────────


def test_camelcase_surname_with_title():
    text = '"Ten points!" Professor McGonagall said sharply.'
    speakers = set(_speakers(text)) - {None}
    assert any("McGonagall" in s for s in speakers)
    # Title + surname, not split into "Professor" and "McGonagall"
    assert "Professor" not in speakers


def test_mrs_with_period():
    text = '"Come in." Mrs. Weasley smiled warmly and stepped aside.'
    speakers = set(_speakers(text)) - {None}
    assert any("Weasley" in s for s in speakers)


def test_unicode_surname():
    text = '"Bonjour," Fleur Delacour said softly.'
    assert "Fleur Delacour" in _speakers(text)


# ── pronoun resolution ─────────────────────────────────────────────


def test_pronoun_resolves_to_gender_matching_name():
    """`he` should prefer a male name over a just-mentioned female."""
    text = 'Hermione called from the sofa. Harry paused. "Wait," he muttered.'
    assert "Harry" in _speakers(text)


def test_pronoun_she_prefers_female():
    text = 'Harry walked past. Hermione looked up. "Hi," she said.'
    assert "Hermione" in _speakers(text)


def test_possessive_fallback_for_pronoun():
    """'Harry\\'s hand tightened. "No," he snapped.' resolves to Harry."""
    text = "Harry's hand tightened on his wand. \"No,\" he snapped."
    assert "Harry" in _speakers(text)


# ── sentence-starter false positives ────────────────────────────────


@pytest.mark.parametrize("word", ["Where", "Why", "Who", "Which", "Whom"])
def test_question_word_not_a_speaker(word):
    text = f'Hermione spoke first. "{word} are you going?" she asked.'
    speakers = [s for s in _speakers(text) if s is not None]
    assert word not in speakers


# ── pre-action attribution ─────────────────────────────────────────


def test_pre_action_single_name_in_gap():
    """'Ron looked up. "Trouble?"' should attribute to Ron."""
    text = '"Not exactly," Harry said. Ron looked up from his chess game. "Trouble?"'
    assert "Ron" in _speaking(text, "Ron") or "Trouble?" in _speaking(text, "Ron")


def test_pre_action_mrs_weasley_bustles_in():
    text = (
        '"Brilliant!" Ron exclaimed.\n\n'
        "Mrs. Weasley bustled in with a plate of biscuits. "
        '"Eat, dears, you all look peaky."'
    )
    speakers = set(_speakers(text)) - {None}
    assert any("Weasley" in s for s in speakers)


# ── carry-forward and alternation ──────────────────────────────────


def test_carry_forward_with_pure_narration_gap():
    """Continuation through descriptive narration with no other names."""
    text = '"Hi," Harry said. The wind rattled the windows. "Come in."'
    assert _speaking(text, "Harry") == ["Hi,", "Come in."]


def test_two_speaker_alternation():
    """Unattributed back-and-forth between two speakers alternates."""
    text = (
        '"Where are you going?" Harry asked.\n'
        '"To the library," Hermione replied.\n'
        '"Why?"\n'
        '"To study."\n'
    )
    segs = [(s.speaker, s.text) for s in parse_segments(text) if s.speaker]
    # Must include Harry asking Why? and Hermione answering
    speakers_in_order = [sp for sp, _ in segs]
    assert speakers_in_order[:4] == ["Harry", "Hermione", "Harry", "Hermione"]


def test_new_character_in_gap_takes_over():
    """Don't carry-forward when a NEW named character breaks in."""
    text = '"Hi," Harry said. Ron turned. "What?"'
    # Ron just entered, so "What?" is Ron — not Harry
    assert _speaking(text, "Ron") == ["What?"]
    assert _speaking(text, "Harry") == ["Hi,"]


# ── unattributed dialogue ──────────────────────────────────────────


def test_unattributed_dialogue_keeps_quotes():
    """Truly unattributable dialogue must keep quote marks so narrator
    TTS reads it with dialogue-like intonation instead of exposition."""
    text = 'The crowd roared. "Yes! Yes!" they chanted.'
    # Check at least one segment preserves the quoted content verbatim
    narration = _narration_joined(text)
    assert '"Yes! Yes!"' in narration


# ── speaker consolidation ──────────────────────────────────────────


def test_title_variant_merging():
    """'Mr. Dumbledore' and 'Mr Dumbledore' must become a single speaker."""
    raw = Counter({"Mr. Dumbledore": 3, "Mr Dumbledore": 2})
    canon, merged = consolidate_speakers(raw)
    assert len(merged) == 1
    # Period-having variant wins on ties
    assert "Mr. Dumbledore" in merged


def test_short_into_long_merging():
    raw = Counter({"Ron": 10, "Ron Weasley": 3})
    canon, merged = consolidate_speakers(raw)
    assert canon["Ron"] == "Ron Weasley"


def test_ambiguous_surname_not_merged():
    """'Weasley' alone could be any of several Weasleys — don't merge."""
    raw = Counter({"Weasley": 2, "Harry Potter": 5})
    canon, merged = consolidate_speakers(raw)
    assert canon["Weasley"] == "Weasley"


# ── gender detection ───────────────────────────────────────────────


@pytest.mark.parametrize("name,expected", [
    ("Harry", "male"),
    ("Hermione", "female"),
    ("Mrs. Weasley", "female"),
    ("Professor McGonagall", "female"),
    ("Dumbledore", "male"),
    ("Draco", "male"),
    ("Luna", "female"),
    ("Mr. Smith", "male"),
])
def test_gender_detection(name, expected):
    assert _guess_gender_from_name(name) == expected


# ── emotion prosody ────────────────────────────────────────────────


@pytest.mark.parametrize("verb,emotion", [
    ("whispered", "whisper"),
    ("shouted", "shout"),
    ("laughed", "cheerful"),
    ("sobbed", "sad"),
    ("snapped", "angry"),
])
def test_emotion_mapping(verb, emotion):
    text = f'"Test," Harry {verb}.'
    segs = [s for s in parse_segments(text) if s.speaker == "Harry"]
    assert segs and segs[0].emotion == emotion


# ── pronunciation override map ─────────────────────────────────────


def test_pronunciation_map_literal_replacement():
    m = {"Hermione": "Her-my-oh-nee"}
    assert _apply_pronunciation_map("Hello Hermione.", m) == "Hello Her-my-oh-nee."


def test_pronunciation_map_longest_first():
    """Longer keys must be applied before shorter prefixes — otherwise
    'Hermione' would consume part of 'Hermione Granger' before the
    multi-word replacement gets a chance."""
    m = {"Hermione": "HER", "Hermione Granger": "HG"}
    assert _apply_pronunciation_map("Hermione Granger", m) == "HG"


def test_pronunciation_map_empty_map_noop():
    assert _apply_pronunciation_map("anything", {}) == "anything"
    assert _apply_pronunciation_map("anything", None) == "anything"


def test_pronunciation_map_comment_keys_ignored(tmp_path):
    p = tmp_path / "pron.json"
    p.write_text(
        '{"_comment": "ignore me", "Tom": "Tahm"}', encoding="utf-8"
    )
    loaded = _load_pronunciation_map(p)
    assert loaded == {"Tom": "Tahm"}


def test_pronunciation_map_bad_json_does_not_crash(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert _load_pronunciation_map(p) == {}


def test_pronunciation_map_missing_file_returns_empty(tmp_path):
    assert _load_pronunciation_map(tmp_path / "nope.json") == {}
