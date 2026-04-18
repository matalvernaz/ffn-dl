"""Tests for the portable-build path resolver."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from ffn_dl import portable


@pytest.fixture(autouse=True)
def reset_cache():
    """portable_root() caches on first call — clear between tests so
    each test sees a fresh resolution."""
    portable._cached_root = None
    portable._env_set = False
    yield
    portable._cached_root = None
    portable._env_set = False


def test_not_frozen_uses_home_dotdir(monkeypatch, tmp_path):
    monkeypatch.setattr(portable, "is_frozen", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    root = portable.portable_root()
    assert root == tmp_path / ".ffn-dl"
    assert root.exists()


def test_frozen_writable_exe_dir_wins(monkeypatch, tmp_path):
    exe_dir = tmp_path / "ffn-dl"
    exe_dir.mkdir()
    monkeypatch.setattr(portable, "is_frozen", lambda: True)
    monkeypatch.setattr(portable, "_exe_dir", lambda: exe_dir)
    root = portable.portable_root()
    assert root == exe_dir


def test_frozen_readonly_exe_falls_back(monkeypatch, tmp_path):
    """If the exe dir isn't writable (e.g. Program Files), fall back
    to the user-local app data dir so the app can still save settings."""
    fake_exe_dir = tmp_path / "readonly"
    fallback = tmp_path / "local-appdata" / "ffn-dl"

    monkeypatch.setattr(portable, "is_frozen", lambda: True)
    monkeypatch.setattr(portable, "_exe_dir", lambda: fake_exe_dir)
    monkeypatch.setattr(portable, "_is_writable", lambda p: p != fake_exe_dir)
    monkeypatch.setattr(portable, "_fallback_root", lambda: fallback)

    root = portable.portable_root()
    assert root == fallback
    assert fallback.exists()


def test_subpaths_rooted_at_portable_root(monkeypatch, tmp_path):
    monkeypatch.setattr(portable, "is_frozen", lambda: True)
    monkeypatch.setattr(portable, "_exe_dir", lambda: tmp_path)
    assert portable.settings_file() == tmp_path / "settings.ini"
    assert portable.cache_dir() == tmp_path / "cache"
    assert portable.neural_dir() == tmp_path / "neural"
    assert portable.booknlp_home() == tmp_path


def test_setup_env_redirects_home_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(portable, "is_frozen", lambda: True)
    monkeypatch.setattr(portable, "_exe_dir", lambda: tmp_path)
    monkeypatch.setenv("HOME", "/original-home")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\Original")
    monkeypatch.setattr(portable.sys, "platform", "win32")

    portable.setup_env()

    import os
    assert os.environ["HOME"] == str(tmp_path)
    # USERPROFILE is only overridden on Windows (to make
    # os.path.expanduser("~") resolve to the portable root for
    # libraries like BookNLP that hardcode "~/booknlp_models").
    assert os.environ["USERPROFILE"] == str(tmp_path)


def test_setup_env_leaves_home_alone_when_not_frozen(monkeypatch, tmp_path):
    """Pip-installed / dev users' HOME must not be rewritten."""
    monkeypatch.setattr(portable, "is_frozen", lambda: False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    portable.setup_env()
    import os
    # The env var is left exactly as the user had it — we didn't touch it.
    assert os.environ["HOME"] == str(tmp_path)


def test_setup_env_creates_standard_subdirs(monkeypatch, tmp_path):
    monkeypatch.setattr(portable, "is_frozen", lambda: True)
    monkeypatch.setattr(portable, "_exe_dir", lambda: tmp_path)
    portable.setup_env()
    for sub in ("cache", "neural"):
        assert (tmp_path / sub).is_dir()
    # booknlp_models is intentionally NOT pre-created — BookNLP makes
    # it on first download so users who never run neural attribution
    # don't end up with a mystery empty folder.
    assert not (tmp_path / "booknlp_models").exists()


def test_setup_env_is_idempotent(monkeypatch, tmp_path):
    """Repeated calls must not repeatedly mutate the environment — the
    module is imported from multiple places indirectly."""
    monkeypatch.setattr(portable, "is_frozen", lambda: True)
    monkeypatch.setattr(portable, "_exe_dir", lambda: tmp_path)
    portable.setup_env()
    portable.setup_env()  # second call is a no-op
    # _env_set flag is the proof we short-circuit
    assert portable._env_set is True
