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
