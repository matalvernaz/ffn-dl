"""Runtime installer for ffn-dl's optional PyPI extras.

Attribution backends (BookNLP, fastcoref) already have an inline
install button driven by :mod:`ffn_dl.attribution` +
:mod:`ffn_dl.neural_env`. The other optional extras declared in
``pyproject.toml`` — ``epub``, ``audio``, ``clipboard``, ``cf-solve``
— had no GUI story: a pip-installed user could run ``pip install
'ffn-dl[all]'`` but a frozen-Windows user had no way in.

This module gives every optional extra the same install pathway the
attribution backends use:

* **pip-installed ffn-dl** shells out to ``sys.executable -m pip
  install <pkg>``.
* **Frozen Windows build** routes through :mod:`ffn_dl.neural_env`,
  which lazily downloads an embeddable CPython and pip-installs into
  a user-writable ``deps/`` folder that ``ffn_dl/__init__.py`` adds to
  ``sys.path`` at startup.

Each feature optionally declares a ``post_install`` command — the
arguments to pass to the Python interpreter after the pip install
step. ``cf-solve`` uses this to run ``python -m playwright install
chromium``, which downloads the browser binary that ``playwright``
itself can't bring along.

The "gui" extra deliberately has no registry entry: if the user is
looking at this dialog, wxPython is already importable.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# ── Registry ─────────────────────────────────────────────────────

# The canonical list of user-installable optional features. The
# ``extra`` field mirrors the name used in ``pyproject.toml``'s
# ``[project.optional-dependencies]`` so the UI can print an
# equivalent ``pip install ffn-dl[<extra>]`` hint for users who
# prefer to do it by hand.
FEATURES: dict[str, dict] = {
    "epub": {
        "extra": "epub",
        "pip_name": "ebooklib>=0.18",
        "import_name": "ebooklib",
        "display": "EPUB export",
        "size_hint": "~2 MB",
        "description": (
            "Adds EPUB output (-f epub). Without this, downloads can "
            "still go to HTML, plain text, or audiobook — but EPUB "
            "is the default format and most ereaders want it."
        ),
        "post_install": None,
    },
    "audio": {
        "extra": "audio",
        "pip_name": "edge-tts>=7.0",
        "import_name": "edge_tts",
        "display": "Audiobook synthesis (edge-tts)",
        "size_hint": "~5 MB (plus ffmpeg on PATH)",
        "description": (
            "Enables -f audio: per-chapter Microsoft neural-voice "
            "synthesis concatenated into a chaptered M4B with embedded "
            "cover art. Needs ffmpeg and ffprobe on PATH — bundled in "
            "the Windows .exe, install separately on Linux / macOS."
        ),
        "post_install": None,
    },
    "clipboard": {
        "extra": "clipboard",
        "pip_name": "pyperclip>=1.8",
        "import_name": "pyperclip",
        "display": "Clipboard watch mode",
        "size_hint": "~100 KB",
        "description": (
            "Enables -w / GUI clipboard watch: ffn-dl polls the "
            "clipboard for recognised story URLs and downloads them "
            "automatically when you copy one."
        ),
        "post_install": None,
    },
    "cf-solve": {
        "extra": "cf-solve",
        "pip_name": "playwright>=1.40",
        "import_name": "playwright",
        "display": "Cloudflare challenge solver (Playwright)",
        "size_hint": "~40 MB pip + ~400 MB browser binary",
        "description": (
            "Enables --cf-solve: on a stubborn Cloudflare 403, launch "
            "a headless Chromium via Playwright, wait for the challenge "
            "to resolve, and inject the cookies into the scraper "
            "session. Solved cookies persist for 24h so later runs "
            "reuse them. Install pulls the pip package first, then "
            "automatically runs 'playwright install chromium' to "
            "download the browser binary itself."
        ),
        "post_install": ["-m", "playwright", "install", "chromium"],
    },
}


def available() -> list[str]:
    """Ordered feature names for the UI. Stable so a test asserting
    against the list order doesn't churn when the registry grows."""
    return ["epub", "audio", "clipboard", "cf-solve"]


def is_installed(feature: str) -> bool:
    """Cheap, import-free check based on :func:`importlib.util.find_spec`.

    Returns False for unknown features so the UI can treat "missing
    entry" the same as "not installed" without a separate branch.
    """
    info = FEATURES.get(feature)
    if not info:
        return False
    try:
        return importlib.util.find_spec(info["import_name"]) is not None
    except (ImportError, ValueError):
        return False


def install_unsupported_reason(feature: str) -> str | None:
    """Return a human-readable refusal message, or None if the install
    path is supported on the current build.

    The only unsupported configuration today is a frozen non-Windows
    build (we don't ship one, but the check future-proofs the code).
    Pip-installed users and the frozen Windows build both have a
    working path.
    """
    info = FEATURES.get(feature)
    if not info:
        return f"Unknown feature: {feature}"
    if _is_frozen():
        try:
            from . import neural_env
        except ImportError:
            return (
                "The embedded Python helper (neural_env) isn't "
                "available in this build — optional features can't "
                "be installed from the GUI."
            )
        if not neural_env.is_supported():
            return (
                "In-app installation from the frozen build is only "
                "supported on Windows. Install ffn-dl from PyPI on "
                "other platforms: "
                f"pip install 'ffn-dl[{info['extra']}]'"
            )
    return None


def pip_hint(feature: str) -> str | None:
    """Produce the equivalent ``pip install ffn-dl[...]`` command
    for users who'd rather install by hand. Returns None for unknown
    features."""
    info = FEATURES.get(feature)
    if not info:
        return None
    return f"pip install 'ffn-dl[{info['extra']}]'"


# ── Install ──────────────────────────────────────────────────────


Logger = Callable[[str], None]


def install(feature: str, log_callback: Logger | None = None) -> bool:
    """Install ``feature`` (pip package + optional post-install).

    Returns True on success. Failures stream through ``log_callback``
    so the GUI renders them inline rather than raising. Never raises —
    an install that can't run (unsupported platform, pip error,
    post-install failure) just returns False with an explanation in
    the log.
    """
    info = FEATURES.get(feature)
    if not info:
        if log_callback:
            log_callback(f"Unknown feature: {feature}")
        return False
    reason = install_unsupported_reason(feature)
    if reason:
        if log_callback:
            for line in reason.splitlines():
                log_callback(line)
        return False

    pip_name = info["pip_name"]
    if _is_frozen():
        from . import neural_env
        if not neural_env.pip_install([pip_name], log_callback=log_callback):
            return False
        # Re-activate so the fresh DEPS_DIR lands on sys.path if it
        # didn't exist at startup. is_installed() below needs to see
        # the newly-installed package via find_spec.
        neural_env.activate()
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", pip_name]
        if not _stream_subprocess(cmd, log_callback):
            return False

    post = info.get("post_install")
    if post:
        if log_callback:
            log_callback(
                f"\nRunning post-install step: {' '.join(post)}"
            )
        if not _run_post_install(post, log_callback):
            if log_callback:
                log_callback(
                    "Post-install step failed; the Python package "
                    "installed but may not be fully usable yet."
                )
            return False

    return True


def _stream_subprocess(
    cmd: Iterable[str],
    log_callback: Logger | None,
    *,
    env: dict | None = None,
) -> bool:
    """Run ``cmd``, forwarding merged stdout/stderr line-by-line to
    ``log_callback``. Returns True on exit code 0. Mirrors the
    streaming behaviour of :func:`attribution.install` so log output
    shape stays consistent across installers.

    ``env`` lets the caller inject extra environment variables (e.g.
    ``PLAYWRIGHT_BROWSERS_PATH`` for the post-install step) without
    leaking them into the parent process.
    """
    cmd = list(cmd)
    if log_callback:
        log_callback(f"\nRunning: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        if log_callback:
            log_callback(f"Failed to launch subprocess: {exc}")
        return False
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if log_callback and line:
            log_callback(line)
    rc = proc.wait()
    if rc != 0 and log_callback:
        log_callback(f"Subprocess exited with status {rc}")
    return rc == 0


def _run_post_install(args: list[str], log_callback: Logger | None) -> bool:
    """Invoke the Python interpreter with ``args``.

    Uses the embedded interpreter under the frozen Windows build
    (so post-install runs inside the same environment that just
    received the pip install), and ``sys.executable`` otherwise.

    Environment inheritance is left to the default — on frozen
    Windows, :func:`ffn_dl.portable.setup_env` has already pinned
    ``PLAYWRIGHT_BROWSERS_PATH`` inside the portable folder so the
    ~400 MB browser binary lands next to the .exe and survives an
    uninstall by "delete the folder" rather than lingering under
    ``%LOCALAPPDATA%\\ms-playwright``.
    """
    if _is_frozen():
        from . import neural_env
        python = str(neural_env.python_exe())
    else:
        python = sys.executable
    return _stream_subprocess([python, *args], log_callback)
