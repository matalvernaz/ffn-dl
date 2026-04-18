"""Regression tests for audiobook-mode text cleanup.

Author's notes and scene dividers are disasters when read aloud by TTS —
"A/N" becomes "slash n", "* * *" becomes "asterisk asterisk asterisk",
and fanfic authors invent a new divider glyph every Tuesday ("oOo",
"xXx", "o0o", em-dash runs). These tests lock in that the audiobook
pipeline strips notes, drops an <hr/> into a pause, and normalises
every text-based divider we've seen in the wild to the same pause.
"""
from ffn_dl.tts import (
    _html_to_audiobook_text,
    _is_scene_break_line,
    _segment_chapter_text,
    _SCENE_BREAK_MARKER,
)


def _segments(html, *, strip_notes=True, hr_as_stars=True):
    return _segment_chapter_text(
        _html_to_audiobook_text(html, strip_notes=strip_notes, hr_as_stars=hr_as_stars)
    )


def _texts(segments):
    return [s.text for s in segments if not s.scene_break]


def _break_count(segments):
    return sum(1 for s in segments if s.scene_break)


# ── scene-break line detector ────────────────────────────────────────


def test_detects_common_divider_punctuation():
    for line in ["---", "===", "***", "~~~", "###", "___",
                 "- - -", "* * *", "= = =", "•••", "**********"]:
        assert _is_scene_break_line(line), line


def test_detects_ornamental_letter_patterns():
    for line in ["oOo", "oOoOo", "xXx", "xXxXx", "o0o", "ooOoo", "OoOoO"]:
        assert _is_scene_break_line(line), line


def test_detects_em_dash_and_unicode_dividers():
    for line in ["— — —", "——————", "•·•·•", "*~*~*"]:
        assert _is_scene_break_line(line), line


def test_does_not_match_real_prose():
    for line in ["Chapter 1", "The end.", "He said hello.", "Oh.",
                 "OK", "A", "...", ". . .", "I",
                 "This is real prose that happens to be short."]:
        assert not _is_scene_break_line(line), line


def test_does_not_match_plain_letter_repetition():
    # ``ooo`` / ``OOO`` / ``xxx`` stay excluded — rating labels or
    # prose affection markers, ambiguous enough to be unsafe. Pure
    # uppercase X runs are handled separately below.
    for line in ["ooo", "OOO", "xxx"]:
        assert not _is_scene_break_line(line), line


def test_pure_uppercase_x_run_matches():
    # Overwhelmingly used as a scene break in fanfic; uppercase ``O``
    # runs stay excluded for disambiguation.
    for line in ["XXX", "XXXX", "XXXXX", "X X X", "X X X X"]:
        assert _is_scene_break_line(line), line


# ── HTML → audiobook text pipeline ───────────────────────────────────


def test_hr_tag_becomes_scene_break():
    segs = _segments("<p>Before.</p><hr/><p>After.</p>")
    assert _texts(segs) == ["Before.", "After."]
    assert _break_count(segs) == 1


def test_strips_author_note_paragraphs():
    html = (
        "<p>Real prose.</p>"
        "<p>A/N: thanks for reading!</p>"
        "<p>More prose.</p>"
    )
    joined = " ".join(_texts(_segments(html)))
    assert "Real prose." in joined
    assert "More prose." in joined
    assert "A/N" not in joined and "thanks for reading" not in joined


def test_text_based_scene_breaks_normalised():
    html = (
        "<p>Scene one.</p>"
        "<p>---</p>"
        "<p>Scene two.</p>"
        "<p>oOo</p>"
        "<p>Scene three.</p>"
        "<p>* * *</p>"
        "<p>Scene four.</p>"
    )
    segs = _segments(html)
    assert _texts(segs) == ["Scene one.", "Scene two.", "Scene three.", "Scene four."]
    assert _break_count(segs) == 3


def test_br_separated_scene_break_inside_paragraph():
    """Some fics put the divider on its own <br>-separated line inside a
    single <p>. Detector must still find it."""
    html = "<p>Line one.<br/>oOo<br/>Line two.</p>"
    segs = _segments(html)
    assert _break_count(segs) == 1
    # Both surrounding lines survive as narration.
    joined = " ".join(_texts(segs))
    assert "Line one" in joined and "Line two" in joined


def test_no_scene_break_marker_leaks_into_segment_text():
    """Whatever happens, the literal sentinel char must never end up in
    a Segment.text — it would get synthesised as a control-char artifact."""
    html = "<p>Before.</p><hr/><p>oOo</p><p>After.</p>"
    for seg in _segments(html):
        assert _SCENE_BREAK_MARKER not in seg.text


# ── opt-out: flags OFF means the listener gets the raw behaviour ─────


def test_flags_off_keeps_author_notes_in_narration():
    """If a listener deliberately leaves --strip-notes off, the A/N
    paragraph must still reach the narrator — don't strip behind their
    back."""
    html = "<p>Real prose.</p><p>A/N: hi everyone</p><p>More prose.</p>"
    joined = " ".join(_texts(_segments(html, strip_notes=False, hr_as_stars=False)))
    assert "A/N" in joined or "hi everyone" in joined


def test_flags_off_keeps_hr_as_literal_asterisks():
    """Without --hr-as-stars, <hr/> falls through to the legacy '* * *'
    string so the listener opted-in gets exactly the prior behaviour."""
    html = "<p>Before.</p><hr/><p>After.</p>"
    segs = _segments(html, strip_notes=False, hr_as_stars=False)
    assert _break_count(segs) == 0
    joined = " ".join(s.text for s in segs)
    assert "* * *" in joined


def test_flags_off_does_not_normalise_text_dividers():
    """Without the flag, `---` or `oOo` on their own line survive as
    narration text (will be read aloud)."""
    html = "<p>Before.</p><p>---</p><p>oOo</p><p>After.</p>"
    segs = _segments(html, strip_notes=False, hr_as_stars=False)
    assert _break_count(segs) == 0
    joined = " ".join(s.text for s in segs)
    assert "---" in joined
    assert "oOo" in joined


def test_strip_notes_independent_of_hr_as_stars():
    """Flags are independent — stripping A/Ns without touching dividers
    is a valid combination."""
    html = "<p>Prose.</p><p>A/N: note</p><hr/><p>More prose.</p>"
    segs = _segments(html, strip_notes=True, hr_as_stars=False)
    joined = " ".join(s.text for s in segs)
    assert "A/N" not in joined and "note" not in joined
    assert "* * *" in joined
    assert _break_count(segs) == 0
