"""GitHub-release self-update for the PyInstaller Windows build.

In-place update flow, Windows frozen-exe only:
  1. Rename the running exe to <name>.exe.old (NTFS permits renaming a
     running executable, just not overwriting or deleting it by name).
  2. Write the newly downloaded exe to the original path.
  3. Spawn the new exe and exit.
  4. On the next startup, cleanup_old_exe() deletes the .exe.old left
     behind (the previous process is gone by then, so the delete succeeds).

On other platforms or when not running frozen, check_for_update still works
— callers should open the release page in a browser instead of attempting
in-place replacement.
"""

import hashlib
import logging
import os
import re
import subprocess
import sys
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

    exe_asset = None
    for asset in data.get("assets") or []:
        if asset.get("name", "").lower().endswith(".exe"):
            exe_asset = asset
            break
    if not exe_asset:
        return None

    return {
        "tag": data["tag_name"],
        "download_url": exe_asset["browser_download_url"],
        "size": exe_asset.get("size", 0),
        "digest": exe_asset.get("digest"),  # "sha256:<hex>" when present
        "release_url": data.get("html_url"),
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


def download_and_replace(update_info, progress_cb=None) -> Path:
    """Download the new exe and swap it in. Returns the path of the new exe.

    Raises RuntimeError on anything that leaves the install in a recoverable
    state; on success, the caller should spawn the new exe and exit.
    """
    if not can_self_replace():
        raise RuntimeError(
            "In-place update is only supported for the Windows .exe build."
        )

    current_exe = Path(sys.executable)
    target_dir = current_exe.parent
    tmp_path = target_dir / (current_exe.stem + ".new.exe")
    old_path = current_exe.with_name(current_exe.stem + ".exe.old")

    # Clear any old backup and stale temp from a prior failed attempt
    for p in (tmp_path, old_path):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    resp = curl_requests.get(
        update_info["download_url"],
        impersonate="chrome",
        timeout=60,
        stream=True,
    )
    resp.raise_for_status()
    total = int(resp.headers.get("content-length") or update_info.get("size") or 0)

    done = 0
    with open(tmp_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if not chunk:
                continue
            f.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)

    try:
        _verify_digest(tmp_path, update_info.get("digest"))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    # Rename the running exe aside, then move the new one into place. If the
    # second rename fails we try to put the original back so the user isn't
    # left with a broken install.
    os.replace(str(current_exe), str(old_path))
    try:
        os.replace(str(tmp_path), str(current_exe))
    except OSError as exc:
        try:
            os.replace(str(old_path), str(current_exe))
        except OSError:
            pass
        raise RuntimeError(f"Failed to install new version: {exc}") from exc

    return current_exe


def restart() -> None:
    """Relaunch the current executable with the original args and exit."""
    args = [sys.executable] + sys.argv[1:]
    subprocess.Popen(args, close_fds=True)
    sys.exit(0)
