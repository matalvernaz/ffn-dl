"""Tests for the runtime-install helper used by the frozen .exe.

We don't exercise the real network download in CI — it would pull
~10 MB of Python embeddable on every run and is flaky. These tests
stub the download + subprocess layer and verify the logic around
path setup, pth rewriting, and idempotency.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from unittest import mock

import pytest

from ffn_dl import neural_env


# ── is_supported / path computation ─────────────────────────────────


def test_is_supported_requires_frozen(monkeypatch):
    """Runtime install only applies to frozen builds — pip-installed
    users get the direct sys.executable path in attribution.py."""
    monkeypatch.setattr(neural_env.sys, "platform", "win32")
    monkeypatch.setattr(neural_env.sys, "frozen", False, raising=False)
    assert neural_env.is_supported() is False


def test_is_supported_requires_windows(monkeypatch):
    monkeypatch.setattr(neural_env.sys, "platform", "linux")
    monkeypatch.setattr(neural_env.sys, "frozen", True, raising=False)
    assert neural_env.is_supported() is False


def test_is_supported_windows_frozen(monkeypatch):
    monkeypatch.setattr(neural_env.sys, "platform", "win32")
    monkeypatch.setattr(neural_env.sys, "frozen", True, raising=False)
    assert neural_env.is_supported() is True


# ── activate() is a safe no-op when deps dir missing ────────────────


def test_activate_noop_when_deps_dir_missing(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(neural_env, "DEPS_DIR", missing)
    # Should not raise regardless of whether site.addsitedir exists.
    neural_env.activate()


def test_activate_calls_addsitedir_when_deps_exist(tmp_path, monkeypatch):
    deps = tmp_path / "deps"
    deps.mkdir()
    monkeypatch.setattr(neural_env, "DEPS_DIR", deps)
    with mock.patch.object(neural_env.site, "addsitedir") as mock_add:
        neural_env.activate()
        mock_add.assert_called_once_with(str(deps))


def test_activate_swallows_exceptions(tmp_path, monkeypatch):
    """Must never block package import even if site is weird."""
    deps = tmp_path / "deps"
    deps.mkdir()
    monkeypatch.setattr(neural_env, "DEPS_DIR", deps)
    with mock.patch.object(neural_env.site, "addsitedir", side_effect=RuntimeError("boom")):
        neural_env.activate()  # should not raise


# ── _enable_site_in_pth rewrites the embeddable's ._pth ─────────────


def test_enable_site_in_pth_uncomments_import_site(tmp_path):
    (tmp_path / "python312._pth").write_text(
        "python312.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n",
        encoding="utf-8",
    )
    assert neural_env._enable_site_in_pth(tmp_path) is True
    body = (tmp_path / "python312._pth").read_text()
    assert "\nimport site\n" in body
    assert "#import site" not in body


def test_enable_site_in_pth_adds_import_site_if_missing(tmp_path):
    (tmp_path / "python312._pth").write_text(
        "python312.zip\n.\n",
        encoding="utf-8",
    )
    assert neural_env._enable_site_in_pth(tmp_path) is True
    body = (tmp_path / "python312._pth").read_text()
    assert body.rstrip().endswith("import site")


def test_enable_site_in_pth_missing_file_reports(tmp_path):
    logged = []
    assert neural_env._enable_site_in_pth(tmp_path, log_callback=logged.append) is False
    assert logged


# ── ensure_embed_python is idempotent via sentinel ──────────────────


def test_ensure_embed_python_is_idempotent(tmp_path, monkeypatch):
    """Second call after a successful bootstrap must NOT re-download
    or re-run get-pip.py."""
    py_dir = tmp_path / "py"
    py_dir.mkdir()
    (py_dir / "python.exe").write_text("fake", encoding="utf-8")
    sentinel = py_dir / ".ffn-dl-bootstrap-ok"
    sentinel.write_text("ok", encoding="utf-8")

    monkeypatch.setattr(neural_env, "PY_DIR", py_dir)
    monkeypatch.setattr(neural_env, "BOOTSTRAP_DONE", sentinel)

    with mock.patch.object(neural_env, "_download") as dl, \
         mock.patch.object(neural_env.subprocess, "run") as srun:
        ok = neural_env.ensure_embed_python()
        assert ok is True
        dl.assert_not_called()
        srun.assert_not_called()


# ── pip_install surfaces errors without raising ─────────────────────


def test_pip_install_reports_failure_when_bootstrap_fails(monkeypatch):
    monkeypatch.setattr(neural_env, "ensure_embed_python", lambda log_callback=None: False)
    ok = neural_env.pip_install(["fastcoref"], log_callback=lambda _l: None)
    assert ok is False
