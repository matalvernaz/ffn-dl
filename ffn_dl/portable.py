"""Portable-build path resolution.

The Windows release ships as a zip that unpacks to a single folder:

    ffn-dl/
      ffn-dl.exe              <- sys.executable when frozen
      _internal/              <- PyInstaller bundle
      settings.ini            <- GUI preferences (was: Windows registry)
      cache/                  <- chapter cache (was: ~/.cache/ffn-dl)
      neural/
        py/                   <- embedded Python for neural backends
        deps/                 <- pip-installed neural backends
      booknlp_models/         <- BookNLP weights (was: ~/booknlp_models)

``portable_root()`` returns the folder everything should live in. For a
frozen build that's the exe's directory when it's writable; if the user
unzipped into something read-only like ``C:\\Program Files\\`` we fall
back to ``%LOCALAPPDATA%\\ffn-dl\\`` so the app still works. For a
pip-installed ffn-dl we return ``~/.ffn-dl/`` so the two install flavors
don't stomp on each other's data.

``setup_env()`` is called once from :mod:`ffn_dl.__init__` before any
other submodule is imported. It creates the root directory and — only
for frozen builds — overrides ``HOME``/``USERPROFILE`` so third-party
libraries that resolve ``~`` (notably BookNLP, which hardcodes
``~/booknlp_models``) land inside the portable folder rather than the
user's actual home directory.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> Path:
    """Directory containing ffn-dl.exe (or the launcher script in dev)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.home() / ".ffn-dl"  # dev fallback — never used in practice


def _is_writable(p: Path) -> bool:
    """True if we can create and remove a probe file inside ``p``.

    The portable layout puts user data next to the exe, but nothing
    stops a user from unzipping into ``C:\\Program Files\\ffn-dl\\``
    where non-admin writes fail. Detecting that here lets us fall
    back to a writable location instead of crashing on first save.
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=p, delete=True):
            pass
        return True
    except OSError:
        return False


def _fallback_root() -> Path:
    """Used when the exe dir isn't writable (e.g. Program Files)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ffn-dl"
    return Path.home() / ".ffn-dl"


_cached_root: Path | None = None


def portable_root() -> Path:
    """Directory that holds all portable data. Cached after first call."""
    global _cached_root
    if _cached_root is not None:
        return _cached_root
    if is_frozen():
        here = _exe_dir()
        _cached_root = here if _is_writable(here) else _fallback_root()
    else:
        _cached_root = Path.home() / ".ffn-dl"
    _cached_root.mkdir(parents=True, exist_ok=True)
    return _cached_root


def settings_file() -> Path:
    return portable_root() / "settings.ini"


def cache_dir() -> Path:
    return portable_root() / "cache"


def neural_dir() -> Path:
    return portable_root() / "neural"


def booknlp_home() -> Path:
    """Directory BookNLP will see as the user's home, so its hardcoded
    ~/booknlp_models lands inside the portable folder. BookNLP creates
    the ``booknlp_models`` subdirectory itself on first run."""
    return portable_root()


_env_set = False


def setup_env() -> None:
    """Create subdirs and redirect ``HOME``/``USERPROFILE`` so libraries
    that expand ``~`` (BookNLP) land inside the portable folder.

    Only mutates the environment for frozen builds — pip-installed
    ffn-dl keeps the user's real home untouched. Idempotent.
    """
    global _env_set
    if _env_set:
        return
    root = portable_root()
    # Always ensure the core subdirs exist — cheap and makes the folder
    # self-explanatory when the user browses into it. booknlp_models is
    # omitted: BookNLP creates it itself on first download, and pre-
    # creating leaves an empty folder for users who never run neural
    # attribution.
    for sub in ("cache", "neural"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    if is_frozen():
        # BookNLP's model loader does os.path.expanduser("~/booknlp_models").
        # On Windows that checks USERPROFILE first, HOMEDRIVE+HOMEPATH
        # second, HOME last; on POSIX it checks HOME. Set both so the
        # override works on every platform we might run on.
        home_str = str(root)
        os.environ["HOME"] = home_str
        if sys.platform == "win32":
            os.environ["USERPROFILE"] = home_str
    _env_set = True
