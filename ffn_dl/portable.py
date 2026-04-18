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
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> Path:
    """Directory containing ffn-dl.exe (or the launcher script in dev)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.home() / ".ffn-dl"  # dev fallback — never used in practice


# Windows folders where unprivileged processes can't write by policy.
# We fall back to %LOCALAPPDATA% only when the exe actually lives
# inside one of these — NOT based on a probe file, because post-update
# the exe dir can be briefly "un-writable" (AV scanning the freshly
# extracted ffn-dl.exe, OneDrive sync, residual handles from
# ZipExtractor). A transient probe failure used to trip the fallback
# and leave a ghost ``%LOCALAPPDATA%\ffn-dl\`` with empty ``cache/``
# and ``neural/`` subdirs next to an otherwise-healthy portable
# install.
_SYSTEM_PROTECTED_ENV_ROOTS = (
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "SystemRoot",
)


def _system_protected_roots() -> list[str]:
    """List of normalized Windows system directory prefixes that are
    read-only for unprivileged users. Empty on non-Windows."""
    if sys.platform != "win32":
        return []
    roots: list[str] = []
    for env in _SYSTEM_PROTECTED_ENV_ROOTS:
        v = os.environ.get(env)
        if v:
            roots.append(os.path.normcase(os.path.normpath(v)))
    # WindowsApps (Microsoft Store sandbox) — writes fail silently or
    # are redirected to a per-package virtualized location. Not
    # somewhere a portable unzip would normally land, but users do
    # surprising things.
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        roots.append(os.path.normcase(os.path.normpath(
            str(Path(localappdata) / "Microsoft" / "WindowsApps")
        )))
    return roots


def _is_system_protected(p: Path) -> bool:
    """True when ``p`` lives inside a path where unprivileged writes
    fail by OS policy rather than by transient locks."""
    try:
        here = os.path.normcase(os.path.normpath(str(p.resolve())))
    except OSError:
        here = os.path.normcase(os.path.normpath(str(p)))
    for root in _system_protected_roots():
        root_trim = root.rstrip("\\/")
        if here == root_trim or here.startswith(root_trim + os.sep):
            return True
    return False


def _fallback_root() -> Path:
    """Used only when the exe dir is inside a system-protected path."""
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
        _cached_root = _fallback_root() if _is_system_protected(here) else here
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
