"""GitHub-release self-update for the portable Windows build.

Uses the ZipExtractor.exe pattern popularised by Libation /
ravibpatel's AutoUpdater.NET: we ship a tiny signed helper .exe
next to ffn-dl.exe, copy it to ``%TEMP%`` so it isn't locked in the
install dir, spawn it via ``ShellExecuteW`` (with UAC elevation only
when the install dir isn't user-writable), and exit. The helper
waits for our process handle via ``Process.WaitForExit``, extracts
the update zip over the install with Windows Restart Manager-based
locked-file diagnosis, writes a ``ZipExtractor.log`` to its own
directory, and relaunches ffn-dl.exe.

The old approach (batch script + ``tasklist`` polling + ``robocopy``)
was fragile for several reasons — silent failures, no logging,
Defender heuristics on batch-in-%TEMP%-touching-files-elsewhere, no
UAC path. All of those go away here.

Helper source: ravibpatel/AutoUpdater.NET v1.9.2 (MIT). Built from
source in CI; see ``.github/workflows/build-windows.yml``.
"""

import ctypes
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from curl_cffi import requests as curl_requests

from . import __version__

logger = logging.getLogger(__name__)

REPO = "matalvernaz/ffn-dl"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Bundled alongside ffn-dl.exe in the portable zip; built in CI from
# ravibpatel/AutoUpdater.NET. If it's ever missing we refuse to
# self-replace and direct the user to the release page instead.
ZIP_EXTRACTOR_EXE = "ZipExtractor.exe"


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
    """True only when we're a frozen Windows build with the helper bundled."""
    if not (is_frozen() and sys.platform.startswith("win")):
        return False
    return (Path(sys.executable).parent / ZIP_EXTRACTOR_EXE).exists()


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
    """Remove debris from earlier update flows.

    - ``<name>.exe.old`` left behind by the pre-1.10 rename-in-place path.
    - ``%TEMP%/ffn-dl-update-*`` workdirs older than 24 hours that the
      batch-script updater used to leave on disk.
    """
    if not is_frozen():
        return
    try:
        current = Path(sys.executable)
        old = current.with_name(current.stem + ".exe.old")
        if old.exists():
            old.unlink()
    except OSError as exc:
        logger.debug("Could not remove stale old exe: %s", exc)

    try:
        temp = Path(tempfile.gettempdir())
        cutoff = time.time() - 24 * 3600
        for d in temp.glob("ffn-dl-update-*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    except OSError as exc:
        logger.debug("Could not sweep stale update workdirs: %s", exc)


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


def _is_writable(path: Path) -> bool:
    """Probe whether the current process can create/remove a file in ``path``.

    If the user unzipped into ``C:\\Program Files\\`` we need to elevate
    via UAC; in the common case (Downloads, Desktop, their home) we
    don't, and skipping the prompt makes the update one-click.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".ffn-dl-update-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _repack_flat(src_dir: Path, dest_zip: Path) -> None:
    """Re-zip ``src_dir``'s *contents* at the archive root.

    The release zip has ``ffn-dl/`` as the top-level entry so humans
    who double-click to extract get a tidy folder. ZipExtractor
    unpacks the archive as-is into the install dir, though, so if
    we handed it the wrapped zip we'd end up with
    ``install/ffn-dl/ffn-dl.exe``. Re-packing flat is a few seconds
    on a 30 MB archive and keeps both paths working from one release.
    """
    with zipfile.ZipFile(
        dest_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3,
    ) as zf:
        for path in src_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir))


def _shell_execute(verb: str, file: Path, params: str, cwd: Path) -> None:
    """Thin wrapper around ``ShellExecuteW`` that raises on failure.

    ``subprocess.Popen`` can't request the ``runas`` verb; Win32
    ``ShellExecuteW`` is the only stdlib-accessible way to trigger a
    UAC elevation prompt from Python.
    """
    SW_SHOWNORMAL = 1
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, verb, str(file), params, str(cwd), SW_SHOWNORMAL,
    )
    # ShellExecuteW returns > 32 on success; values <= 32 are Win32
    # error codes (2 = ENOENT, 5 = access denied, 1223 = UAC cancelled
    # — though the UAC-cancel case usually returns SE_ERR_ACCESSDENIED).
    if rc <= 32:
        raise RuntimeError(f"ShellExecuteW failed (code {rc}) launching {file}")


def _spawn_extractor(
    extractor: Path, zip_path: Path, install_dir: Path, exe: Path,
) -> None:
    """Launch the bundled helper to swap files + relaunch ffn-dl.

    Uses ``runas`` only when the install dir isn't writable — the
    common case (user unzipped to Downloads, Desktop, home) doesn't
    need elevation and skipping the prompt makes the flow one-click.
    """
    def _q(p):
        return f'"{p}"'

    params = (
        f"--input {_q(zip_path)} "
        f"--output {_q(install_dir)} "
        f"--current-exe {_q(exe)}"
    )
    verb = "open" if _is_writable(install_dir) else "runas"
    # ZipExtractor writes its log (ZipExtractor.log) to AppDomain.
    # CurrentDomain.BaseDirectory — i.e. its own folder. Point it at
    # the per-update workdir so the log lives next to the binary.
    _shell_execute(verb, extractor, params, extractor.parent)


def download_and_replace(update_info, progress_cb=None) -> Path:
    """Download the new portable zip and spawn the update helper.

    Returns the install directory (for logging). Caller MUST exit
    shortly after — the helper blocks on our PID before it touches
    any file in the install.
    """
    if not can_self_replace():
        raise RuntimeError(
            "In-place update is only supported for the Windows portable "
            "build with ZipExtractor.exe bundled. Please download the "
            "new version manually from the release page."
        )

    if not update_info.get("is_zip"):
        raise RuntimeError(
            "Update asset is not a portable zip. Please download the new "
            "version manually from the release page."
        )

    current_exe = Path(sys.executable).resolve()
    install_dir = current_exe.parent
    extractor_src = install_dir / ZIP_EXTRACTOR_EXE

    workdir = Path(tempfile.mkdtemp(prefix="ffn-dl-update-"))
    zip_path = workdir / "ffn-dl-portable.zip"
    extracted = workdir / "extracted"
    flat_zip = workdir / "ffn-dl-flat.zip"

    try:
        _download(
            update_info["download_url"],
            zip_path,
            progress_cb=progress_cb,
            expected_size=update_info.get("size", 0),
        )
        _verify_digest(zip_path, update_info.get("digest"))

        # Unwrap the top-level folder so ZipExtractor can extract
        # straight into install_dir.
        extracted.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extracted)
        zip_path.unlink(missing_ok=True)

        inner = list(extracted.iterdir())
        src_for_repack = (
            inner[0] if len(inner) == 1 and inner[0].is_dir() else extracted
        )
        _repack_flat(src_for_repack, flat_zip)
        shutil.rmtree(extracted, ignore_errors=True)

        # Copy the extractor out of the install dir so it isn't locked
        # when it tries to overwrite its own binary inside install_dir.
        extractor_tmp = workdir / ZIP_EXTRACTOR_EXE
        shutil.copy2(extractor_src, extractor_tmp)

        _spawn_extractor(extractor_tmp, flat_zip, install_dir, current_exe)
    except Exception:
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
    plus letting the parent finish its ``sys.exit`` keeps the two
    processes' teardown / startup from stepping on each other, which
    otherwise shows up as "app restarted but network/search is broken"
    on the first post-update launch.

    On POSIX we use ``os.execv``, which replaces the current process
    image in place — same PID, no race, nothing to detach.
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

    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, args)
