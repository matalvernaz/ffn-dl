"""GitHub-release self-update for the portable Windows build.

The app is unpacked to a folder that contains ``ffn-dl.exe``,
``_internal/`` (PyInstaller bundle), and user data directories. A
running ffn-dl.exe holds a lock on itself and on the DLLs inside
``_internal/``, so we can't replace them while the app is running —
instead we:

  1. Download the new ``ffn-dl-portable.zip`` into a temp directory.
  2. Extract it to ``<temp>/ffn-dl-new/``.
  3. Write an updater batch script that:
       - waits for ffn-dl.exe to exit,
       - copies every file from ``ffn-dl-new/`` over the current install
         (user data folders are left untouched),
       - deletes the temp dir,
       - relaunches ffn-dl.exe,
       - self-deletes.
  4. Spawn that batch script detached and exit.

On other platforms or when not running frozen, ``check_for_update`` still
works — callers should open the release page in a browser instead of
attempting in-place replacement.
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from curl_cffi import requests as curl_requests

from . import __version__

logger = logging.getLogger(__name__)

REPO = "matalvernaz/ffn-dl"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


def _parse_version(tag: str):
    """Parse 'v1.2.3' → (1, 2, 3). Returns None for unrecognised formats."""
    if not tag:
        return None
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def check_for_update():
    """Fetch the GitHub latest-release JSON.

    Returns a dict {tag, download_url, size, digest} when a newer
    version exists than the currently running one, else None. Network
    errors raise; callers should catch broadly and skip silently so a
    transient failure doesn't bother the user.
    """
    resp = curl_requests.get(LATEST_URL, impersonate="chrome", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    latest = _parse_version(data.get("tag_name", ""))
    current = _parse_version(__version__)
    if not latest or not current or latest <= current:
        return None

    # Prefer the portable zip (current distribution format); fall back
    # to a single-file .exe only if one is still attached to an old
    # release so 1.9.x clients keep working.
    zip_asset = None
    exe_asset = None
    for asset in data.get("assets") or []:
        name = asset.get("name", "").lower()
        if name.endswith(".zip") and "portable" in name:
            zip_asset = asset
            break
        if name.endswith(".exe") and exe_asset is None:
            exe_asset = asset
    chosen = zip_asset or exe_asset
    if not chosen:
        return None

    return {
        "tag": data["tag_name"],
        "download_url": chosen["browser_download_url"],
        "size": chosen.get("size", 0),
        "digest": chosen.get("digest"),  # "sha256:<hex>" when present
        "release_url": data.get("html_url"),
        "is_zip": zip_asset is not None,
    }


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def can_self_replace() -> bool:
    """True only when running as a frozen Windows executable."""
    return is_frozen() and sys.platform.startswith("win")


def _verify_digest(path: Path, digest: str) -> None:
    if not digest or ":" not in digest:
        return
    algo, expected = digest.split(":", 1)
    if algo.lower() != "sha256":
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest().lower() != expected.lower():
        raise RuntimeError(
            "Downloaded update failed SHA-256 verification. The file was "
            "not installed; the running version is unchanged."
        )


def cleanup_old_exe() -> None:
    """Remove any <name>.exe.old left behind by a previous in-place update."""
    if not is_frozen():
        return
    try:
        current = Path(sys.executable)
        old = current.with_name(current.stem + ".exe.old")
        if old.exists():
            old.unlink()
    except OSError as exc:
        logger.debug("Could not remove stale old exe: %s", exc)


# User-data directories inside the install folder that MUST survive
# an update. Keep this list in sync with ffn_dl/portable.py.
_USER_DATA_DIRS = {
    "cache",
    "neural",
    "booknlp_models",
}
_USER_DATA_FILES = {
    "settings.ini",
}


def _download(url: str, dest: Path, progress_cb=None, expected_size: int = 0) -> None:
    """Stream ``url`` to ``dest``; raises on HTTP errors."""
    resp = curl_requests.get(url, impersonate="chrome", timeout=60, stream=True)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length") or expected_size or 0)
    done = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)


def _write_updater_batch(
    script_path: Path,
    exe_path: Path,
    new_dir: Path,
    install_dir: Path,
    parent_pid: int,
) -> None:
    """Write the .bat helper that runs after we exit and swaps files.

    robocopy-based because it's stock Windows and handles locked files
    better than ``copy`` / ``xcopy``. The /XD flag excludes user data
    dirs so an update preserves the user's settings, chapter cache,
    and any installed neural backends.

    Waiting for the lock to release is the subtle part. A running PE
    on Windows can still be *renamed* (directory-entry ops don't touch
    the mapped image section), so the previous rename-probe exited the
    wait loop immediately while the exe was still locked, and robocopy
    then failed with ERROR 32 on ffn-dl.exe / _internal DLLs. We now
    poll ``tasklist`` for the parent PID instead — the image-section
    lock is dropped in the same kernel path that tears the process
    record down, so "PID gone" is a sound proxy for "files writable."
    """
    xd = " ".join(f'"{install_dir / d}"' for d in _USER_DATA_DIRS)
    # settings.ini lives at the top level — robocopy's /XF excludes files by name.
    xf = " ".join(f'"{f}"' for f in _USER_DATA_FILES)
    script = f"""@echo off
setlocal
set "EXE={exe_path}"
set "INSTALL={install_dir}"
set "NEW={new_dir}"
set "PID={parent_pid}"
rem Wait up to 120 seconds for the parent ffn-dl.exe process to exit.
rem A running PE can still be renamed, so probe the PID directly.
set /a tries=0
:waitloop
set /a tries+=1
if %tries% GTR 120 goto :giveup
tasklist /FI "PID eq %PID%" /NH 2>nul | find "%PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto :waitloop
)
rem Give the kernel a moment to tear down the image section after
rem the process record is gone; robocopy's own retries cover the rest.
timeout /t 1 /nobreak >nul

rem Copy new files over the install, preserving user data.
robocopy "%NEW%" "%INSTALL%" /E /IS /IT /R:30 /W:1 /NFL /NDL /NJH /NJS /NP /XD {xd} /XF {xf} >nul
rem robocopy exit codes 0-7 are success; 8+ are failures.
if errorlevel 8 goto :giveup

rem Clean up and relaunch.
rmdir /S /Q "%NEW%" >nul 2>&1
start "" "%EXE%"
del "%~f0"
exit /b 0

:giveup
rem Leave the new files for the user to inspect; don't relaunch.
echo Update failed. New version is at: %NEW%
del "%~f0"
exit /b 1
"""
    script_path.write_text(script, encoding="utf-8")


def download_and_replace(update_info, progress_cb=None) -> Path:
    """Download the new portable zip and spawn the updater script.

    Returns the install directory (for logging). Caller MUST exit
    shortly after — the updater batch waits for the process to release
    its file locks before it can proceed.
    """
    if not can_self_replace():
        raise RuntimeError(
            "In-place update is only supported for the Windows portable build."
        )

    if not update_info.get("is_zip"):
        # A prior-release .exe asset — we can't swap a single exe into a
        # one-folder install safely, so fall back to directing the user
        # at the release page.
        raise RuntimeError(
            "Update asset is not a portable zip. Please download the new "
            "version manually from the release page."
        )

    current_exe = Path(sys.executable).resolve()
    install_dir = current_exe.parent
    workdir = Path(tempfile.mkdtemp(prefix="ffn-dl-update-"))
    zip_path = workdir / "ffn-dl-portable.zip"
    new_dir = workdir / "ffn-dl-new"

    try:
        _download(
            update_info["download_url"],
            zip_path,
            progress_cb=progress_cb,
            expected_size=update_info.get("size", 0),
        )
        _verify_digest(zip_path, update_info.get("digest"))

        new_dir.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(new_dir)
        zip_path.unlink(missing_ok=True)

        # The release zip's top-level is "ffn-dl/" — unwrap one level if
        # present so new_dir mirrors install_dir's layout directly.
        inner = list(new_dir.iterdir())
        if len(inner) == 1 and inner[0].is_dir():
            src = inner[0]
            for item in src.iterdir():
                item.rename(new_dir / item.name)
            src.rmdir()

        script_path = workdir / "apply-update.bat"
        _write_updater_batch(
            script_path, current_exe, new_dir, install_dir, os.getpid()
        )

        # Launch the updater detached so it survives our exit.
        creationflags = 0x8 | 0x200  # DETACHED + CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            ["cmd.exe", "/c", str(script_path)],
            close_fds=True,
            creationflags=creationflags,
            cwd=str(workdir),
        )
    except Exception:
        # Clean up workdir on any pre-spawn failure so we don't leave
        # partial downloads accumulating in %TEMP%.
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    return install_dir


def restart() -> None:
    """Relaunch the current executable with the original args and exit.

    On Windows the child is spawned DETACHED so it doesn't inherit the
    parent's console, handles, or process group. PyInstaller onefile
    builds extract to a random ``_MEI<rand>`` temp dir at startup and
    the bootloader cleans that dir on exit — if the child's extraction
    races with the parent's cleanup (both touching %TEMP% at once),
    DLLs and data files can end up half-written. Detaching the child
    plus letting the parent finish its `sys.exit` keeps the two
    processes' teardown / startup from stepping on each other, which
    otherwise shows up as "app restarted but network/search is broken"
    on the first post-update launch.

    On POSIX we use os.execv, which replaces the current process image
    in place — same PID, no race, nothing to detach.
    """
    args = [sys.executable] + sys.argv[1:]

    if sys.platform.startswith("win"):
        # DETACHED_PROCESS (0x8) — no console inheritance.
        # CREATE_NEW_PROCESS_GROUP (0x200) — Ctrl-C in a dying parent
        # console can't propagate to the child.
        # CREATE_BREAKAWAY_FROM_JOB (0x1000000) — if the parent is in a
        # Job object (installer, AV sandbox) the child escapes the
        # lifetime tie that would otherwise kill it with us.
        creationflags = 0x8 | 0x200 | 0x1000000
        try:
            subprocess.Popen(
                args,
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError:
            # Job-breakaway isn't always permitted (some installers
            # run inside a Job with JOB_OBJECT_LIMIT_BREAKAWAY_OK
            # disabled). Retry without the breakaway flag — we still
            # get detach + new-group, which is the important part.
            subprocess.Popen(
                args,
                close_fds=True,
                creationflags=0x8 | 0x200,
            )
        sys.exit(0)

    # POSIX: replace the running image with the new exe. Same PID, no
    # second process, no race. The `flush` call matches what the
    # wxPython shutdown would normally do via its atexit hooks.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, args)
