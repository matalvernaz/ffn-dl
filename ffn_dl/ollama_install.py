"""Install Ollama from inside ffn-dl on Windows via ``winget``.

The LLM author's-note backstop and the audiobook attribution backend
both default to a local Ollama daemon. New users who tick "Use LLM"
without realising they need a separate installer hit a 116-line
"connection refused" wall — the same bug the 2.2.6 circuit breaker
papered over after the fact. The settings dialog now offers a
one-click install that wraps Microsoft's package manager (built into
Windows 10 1809+ / Windows 11) so the user doesn't have to leave the
app, hunt down ``OllamaSetup.exe``, click through SmartScreen, and
come back.

Pure helpers — no GUI deps, callable from a worker thread, every
network or subprocess hop wrapped in a callback so the dialog can
stream progress to a read-only text control. Linux/macOS get a
graceful "not supported here, use the web installer" path because
``winget`` is Windows-only.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Callable, Iterable

OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
"""Browser-fallback URL when ``winget`` isn't available — exposed so
the dialog can offer the same link from a "Get Ollama" button without
duplicating the string."""

WINGET_PACKAGE_ID = "Ollama.Ollama"
"""Microsoft Store / Microsoft community-repo package id. Stable since
Ollama published their winget manifest; pinning the constant means a
typo here can be unit-tested separately from the subprocess plumbing."""


def winget_supported() -> bool:
    """``True`` when ``winget`` is on PATH (Windows 10 1809+ ships it
    as App Installer, Windows 11 has it preinstalled). Linux and macOS
    return ``False`` so callers know to fall back to the browser
    installer instead of trying a subprocess that'll just FileNotFound."""
    if not sys.platform.startswith("win"):
        return False
    return shutil.which("winget") is not None


def winget_install_command() -> list[str]:
    """The exact argv used by :func:`install_ollama_via_winget`. Split
    out so tests can pin the flag set without having to monkey-patch
    ``subprocess.Popen`` first.

    ``--silent`` runs the Ollama installer non-interactively (Ollama's
    NSIS package supports it), and the two ``--accept`` flags suppress
    winget's first-run TOS prompts that would otherwise block the
    background process forever waiting for stdin."""
    return [
        "winget",
        "install",
        "--id", WINGET_PACKAGE_ID,
        "--exact",
        "--silent",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--disable-interactivity",
    ]


def install_ollama_via_winget(
    log_callback: Callable[[str], None] | None = None,
) -> bool:
    """Run the winget install command and stream stdout to
    ``log_callback`` line-by-line. Returns ``True`` on success.

    Designed to be invoked from a worker thread. ``log_callback`` is
    expected to marshal to the GUI thread itself (the GUI side uses
    ``wx.CallAfter``); this helper does no thread juggling of its own.

    The "already installed, no upgrade available" exit codes from
    winget (``0x8a15002b`` / decimal ``-1978335189``) are treated as
    success — a user who already has Ollama and clicks Install
    shouldn't see a red error.
    """
    log = log_callback or (lambda _line: None)

    if not winget_supported():
        log(
            "winget not found. Open "
            f"{OLLAMA_DOWNLOAD_URL} and run the installer manually."
        )
        return False

    cmd = winget_install_command()
    log("Running: " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        # Race: winget vanished between shutil.which and Popen
        # (uninstalled mid-flight). Treat the same as unsupported.
        log(
            "winget vanished from PATH. Open "
            f"{OLLAMA_DOWNLOAD_URL} and run the installer manually."
        )
        return False

    return _consume_winget_output(proc, log)


def _consume_winget_output(
    proc: "subprocess.Popen[str]",
    log: Callable[[str], None],
) -> bool:
    """Drain ``proc.stdout`` to ``log`` and return whether the install
    succeeded.

    Split out for testability: the ``Popen`` instance can be a stub
    that yields a fixed sequence of lines and a recorded return code,
    avoiding the real subprocess and the real winget."""
    if proc.stdout is not None:
        for line in proc.stdout:
            log(line.rstrip())
    proc.wait()
    return _winget_exit_is_success(proc.returncode)


# Winget exit codes that mean "no install action needed" rather than
# "install failed". The user's intent ("get me ollama") is satisfied
# either way, so the dialog reports success.
_WINGET_NO_OP_CODES = frozenset(
    {
        # APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE — ``winget
        # install`` flags this when the package is already at-or-above
        # the available version.
        -1978335189,
        # Same value, expressed unsigned (winget on some Windows
        # builds reports it via the unsigned cast).
        0x8A15002B,
    }
)


def _winget_exit_is_success(code: int | None) -> bool:
    """Treat exit code 0 *and* the "already installed" no-op codes as
    success. Everything else (including ``None`` from a process that
    exited weirdly) counts as a failure for the dialog."""
    if code == 0:
        return True
    if code is None:
        return False
    return code in _WINGET_NO_OP_CODES


def winget_unavailable_reason() -> str:
    """Human-readable explanation of why the Install button is disabled
    on this platform. Used by the dialog to set a tooltip / status
    line so screen-reader users get the same context sighted users
    pick up from a greyed-out button."""
    if not sys.platform.startswith("win"):
        return (
            "Automatic install needs winget, which is Windows-only. "
            f"Use {OLLAMA_DOWNLOAD_URL} for the macOS / Linux installer."
        )
    if shutil.which("winget") is None:
        return (
            "winget isn't on PATH. Install 'App Installer' from the "
            "Microsoft Store, or use the Download Ollama button to "
            "get the installer directly."
        )
    return ""


__all__ = [
    "OLLAMA_DOWNLOAD_URL",
    "WINGET_PACKAGE_ID",
    "install_ollama_via_winget",
    "winget_install_command",
    "winget_supported",
    "winget_unavailable_reason",
]
