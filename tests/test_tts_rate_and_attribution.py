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
    assert attribution.available() == ["builtin", "fastcoref", "booknlp"]


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
