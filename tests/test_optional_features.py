"""Tests for the optional-features installer registry.

Real pip calls don't run here — we mock the subprocess / neural_env
path so the tests don't touch the network and don't need an
embedded Python on disk. The goal is to pin the decision logic:
what gets pip-installed, what runs a post-install hook, how
unsupported platforms refuse cleanly, and how errors are surfaced.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from ffn_dl import optional_features as of


# ── Registry shape ───────────────────────────────────────────────


def test_available_lists_every_registered_feature():
    names = of.available()
    assert set(names) == set(of.FEATURES.keys())
    # Stability: the order surfaces in the GUI rows, so a regression
    # that reordered the registry shouldn't silently rearrange the UI.
    assert names == ["epub", "audio", "clipboard", "cf-solve"]


def test_every_feature_has_required_registry_fields():
    required = {
        "extra", "pip_name", "import_name",
        "display", "size_hint", "description",
    }
    for name, info in of.FEATURES.items():
        missing = required - info.keys()
        assert not missing, f"{name} missing {missing}"
        # post_install is optional but must be a list or None
        assert info.get("post_install", None) is None or isinstance(
            info["post_install"], list,
        )


def test_pip_hint_matches_extra_name():
    for name, info in of.FEATURES.items():
        hint = of.pip_hint(name)
        assert hint is not None
        assert f"ffn-dl[{info['extra']}]" in hint


def test_pip_hint_returns_none_for_unknown():
    assert of.pip_hint("nope") is None


# ── is_installed ─────────────────────────────────────────────────


def test_is_installed_false_for_unknown_feature():
    assert of.is_installed("nope") is False


def test_is_installed_uses_find_spec(monkeypatch):
    """Don't actually import the package; find_spec is the cheap
    check the UI uses on every refresh. Regression: an earlier design
    tried to ``import`` the package, which triggered the package's
    side effects every time the dialog refreshed."""
    sentinel = object()
    calls = []

    def fake_find_spec(name):
        calls.append(name)
        return sentinel if name == "ebooklib" else None

    monkeypatch.setattr(of.importlib.util, "find_spec", fake_find_spec)
    assert of.is_installed("epub") is True
    assert of.is_installed("audio") is False
    assert calls == ["ebooklib", "edge_tts"]


# ── install_unsupported_reason ───────────────────────────────────


def test_unsupported_reason_for_unknown_feature():
    reason = of.install_unsupported_reason("not-a-feature")
    assert reason is not None
    assert "Unknown feature" in reason


def test_unsupported_reason_none_on_non_frozen(monkeypatch):
    """Pip-installed ffn-dl has a usable ``sys.executable`` so every
    feature is installable."""
    monkeypatch.setattr(of, "_is_frozen", lambda: False)
    for name in of.available():
        assert of.install_unsupported_reason(name) is None


# ── install() ────────────────────────────────────────────────────


def test_install_pip_path_on_non_frozen(monkeypatch):
    """Non-frozen install shells out to ``sys.executable -m pip
    install --upgrade <pkg>``. No post-install for the plain extras,
    so the happy path stops at the single subprocess."""
    monkeypatch.setattr(of, "_is_frozen", lambda: False)
    commands: list[list[str]] = []

    def fake_stream(cmd, log_cb):
        commands.append(list(cmd))
        return True

    monkeypatch.setattr(of, "_stream_subprocess", fake_stream)
    ok = of.install("epub")
    assert ok is True
    assert len(commands) == 1
    assert commands[0][:5] == [
        sys.executable, "-m", "pip", "install", "--upgrade",
    ]
    assert "ebooklib" in commands[0][-1]


def test_install_runs_post_install_hook_for_cf_solve(monkeypatch):
    """cf-solve has a post_install step — the dialog must spawn
    ``python -m playwright install chromium`` after the pip install
    or the pip-only result isn't usable (no browser binary)."""
    monkeypatch.setattr(of, "_is_frozen", lambda: False)
    commands: list[list[str]] = []

    def fake_stream(cmd, log_cb):
        commands.append(list(cmd))
        return True

    monkeypatch.setattr(of, "_stream_subprocess", fake_stream)
    ok = of.install("cf-solve")
    assert ok is True
    assert len(commands) == 2
    pip_cmd, post_cmd = commands
    assert "playwright" in pip_cmd[-1]
    assert post_cmd[1:] == ["-m", "playwright", "install", "chromium"]


def test_install_bails_when_pip_step_fails(monkeypatch):
    """If pip fails there's no point running the post-install step —
    the import wouldn't resolve. Regression: an earlier draft ran
    both unconditionally, producing a confusing "Chromium downloaded
    but the Python package isn't installed" state."""
    monkeypatch.setattr(of, "_is_frozen", lambda: False)
    calls: list[list[str]] = []

    def fake_stream(cmd, log_cb):
        calls.append(list(cmd))
        return False  # pip fails

    monkeypatch.setattr(of, "_stream_subprocess", fake_stream)
    ok = of.install("cf-solve")
    assert ok is False
    assert len(calls) == 1  # pip step ran; post-install did not


def test_install_bails_when_post_install_fails(monkeypatch):
    """Pip succeeded but ``playwright install chromium`` failed —
    return False so the UI reports the install as incomplete. The
    Python package is on sys.path (so a retry can skip pip and just
    re-run the post-install) but the feature isn't usable yet."""
    monkeypatch.setattr(of, "_is_frozen", lambda: False)
    results = iter([True, False])  # pip ok, post-install fails
    logged: list[str] = []

    def fake_stream(cmd, log_cb):
        if log_cb:
            log_cb(f"cmd: {cmd[-1]}")
        return next(results)

    monkeypatch.setattr(of, "_stream_subprocess", fake_stream)
    ok = of.install("cf-solve", log_callback=logged.append)
    assert ok is False
    assert any("Post-install step failed" in line for line in logged)


def test_install_refuses_unknown_feature():
    logged: list[str] = []
    ok = of.install("bogus", log_callback=logged.append)
    assert ok is False
    assert any("Unknown feature" in line for line in logged)


def test_install_refuses_unsupported_platform(monkeypatch):
    """When the platform is refused, the error goes through
    ``log_callback`` rather than raising — the dialog is streaming
    log output into a pane, not a try/except."""
    monkeypatch.setattr(
        of,
        "install_unsupported_reason",
        lambda f: "Simulated unsupported reason",
    )
    logged: list[str] = []
    ok = of.install("epub", log_callback=logged.append)
    assert ok is False
    assert any("Simulated unsupported reason" in line for line in logged)


# ── Frozen-build routing ─────────────────────────────────────────


def test_install_frozen_routes_through_neural_env(monkeypatch):
    """On frozen Windows, install must go through
    :mod:`ffn_dl.neural_env` rather than ``sys.executable -m pip`` —
    the frozen .exe's ``sys.executable`` points at the PyInstaller
    bootloader, not at a Python interpreter."""
    import ffn_dl
    import ffn_dl.neural_env as real_neural_env

    monkeypatch.setattr(of, "_is_frozen", lambda: True)
    calls: dict[str, list] = {"pip": [], "activate": 0}

    class FakeNeural:
        @staticmethod
        def is_supported():
            return True

        @staticmethod
        def pip_install(pkgs, log_callback=None):
            calls["pip"].append(list(pkgs))
            return True

        @staticmethod
        def activate():
            calls["activate"] += 1

        @staticmethod
        def python_exe():
            return "/fake/python.exe"

    # ``from . import neural_env`` in :mod:`ffn_dl.optional_features`
    # resolves via ``getattr(ffn_dl, 'neural_env')`` once the module
    # is already loaded, so patching sys.modules alone isn't enough —
    # we also have to swap the package attribute.
    monkeypatch.setattr(ffn_dl, "neural_env", FakeNeural, raising=False)
    monkeypatch.setitem(sys.modules, "ffn_dl.neural_env", FakeNeural)
    monkeypatch.setattr(of, "install_unsupported_reason", lambda f: None)
    monkeypatch.setattr(of, "_stream_subprocess", lambda cmd, log_cb: True)

    ok = of.install("epub")
    assert ok is True
    assert calls["pip"] == [["ebooklib>=0.18"]]
    assert calls["activate"] == 1
    # Sanity: the real module wasn't replaced permanently — undo'd
    # by monkeypatch cleanup, but let's prove we didn't just delete
    # the real reference.
    assert real_neural_env.__name__ == "ffn_dl.neural_env"
