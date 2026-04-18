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


def test_install_unsupported_reason_when_frozen(monkeypatch):
    """Frozen .exe builds can't run `sys.executable -m pip` — the
    registry must surface a reason instead of returning a bogus
    install command."""
    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    assert attribution.install_command("fastcoref") is None
    assert attribution.install_command("booknlp") is None
    assert attribution.install_unsupported_reason("fastcoref")
    assert attribution.install_unsupported_reason("booknlp")


def test_install_unsupported_reason_none_when_not_frozen(monkeypatch):
    monkeypatch.setattr(attribution, "_is_frozen", lambda: False)
    assert attribution.install_unsupported_reason("fastcoref") is None
    assert attribution.install_unsupported_reason("booknlp") is None


def test_install_refuses_cleanly_when_frozen(monkeypatch):
    """install() must not Popen sys.executable as a pip runner when
    frozen — it would route pip flags to the exe's argparse and fail."""
    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    lines = []
    ok = attribution.install("fastcoref", log_callback=lines.append)
    assert ok is False
    # The first logged line should be the first line of the frozen
    # explanation — the user gets a clear message, not a confused
    # argparse traceback.
    assert lines, "expected a user-facing log message"
    assert "standalone .exe" in lines[0]


def test_install_builtin_noop_when_frozen(monkeypatch):
    """builtin has nothing to install — the frozen guard must not
    accidentally start rejecting builtin."""
    monkeypatch.setattr(attribution, "_is_frozen", lambda: True)
    assert attribution.install("builtin") is True
