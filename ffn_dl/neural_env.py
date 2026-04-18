"""Runtime dependency installation for the frozen Windows .exe.

Neural attribution backends (fastcoref, BookNLP) pull in torch,
transformers, and hundreds of MB of wheels — too big to bundle, and
PyInstaller's frozen bundle can't run its own `sys.executable -m pip`
anyway because ``sys.executable`` points at the .exe bootloader, not a
Python interpreter.

The fix is the same pattern ComfyUI, A1111, and InvokeAI use: download
a standalone embeddable Python next to the app on first use, bootstrap
pip into it, then ``pip install --target=<user dir>`` the heavy deps.
At app startup we add that user dir to ``sys.path`` via
``site.addsitedir`` so ``.pth`` files (torch needs one) are honored
and the backends become importable.

The embeddable Python we download MUST match the frozen .exe's
Python minor version or wheels built for a different ABI won't load.
``PYTHON_EMBED_VERSION`` is pinned accordingly.
"""
from __future__ import annotations

import io
import logging
import os
import site
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


# Pin to match the Python version the .exe is built with. The CI
# workflow uses actions/setup-python@v6 with python-version "3.12" —
# any 3.12.X embeddable is ABI-compatible with any 3.12.X frozen
# build. Update this constant if the build workflow's minor version
# ever changes.
PYTHON_EMBED_VERSION = "3.12.8"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_EMBED_VERSION}/"
    f"python-{PYTHON_EMBED_VERSION}-embed-amd64.zip"
)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"


def _root() -> Path:
    """Where we keep the embedded Python + installed deps.

    For portable Windows builds this is ``<exe_dir>\\neural\\`` so the
    multi-hundred-MB torch install moves with the unzipped folder. For
    pip-installed ffn-dl (or a dev checkout) we keep ``~/.ffn-dl/neural``.
    ``portable.portable_root()`` picks a writable fallback under
    %LOCALAPPDATA% when the exe is in a read-only location like
    Program Files.
    """
    from . import portable as _p
    return _p.neural_dir()


NEURAL_ROOT = _root()
PY_DIR = NEURAL_ROOT / "py"
DEPS_DIR = NEURAL_ROOT / "deps"
BOOTSTRAP_DONE = PY_DIR / ".ffn-dl-bootstrap-ok"  # sentinel — only written on full success


def is_supported() -> bool:
    """True when runtime install via embeddable Python makes sense.

    That's Windows + frozen builds specifically. A pip-installed
    ffn-dl already has a real Python interpreter it can reuse via
    ``sys.executable``, so it takes a different code path in
    ``attribution.install``.
    """
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def python_exe() -> Path:
    """Path to the embedded Python interpreter (may not yet exist)."""
    return PY_DIR / "python.exe"


def deps_activated() -> bool:
    """True if DEPS_DIR is already on sys.path (idempotent activate)."""
    target = str(DEPS_DIR.resolve()) if DEPS_DIR.exists() else str(DEPS_DIR)
    return any(Path(p).resolve() == DEPS_DIR.resolve() for p in sys.path if p)


def activate() -> None:
    """Add DEPS_DIR to sys.path so neural backends become importable.

    Called at package import time from ``ffn_dl/__init__.py``. Safe
    to call repeatedly and safe to call before the directory exists
    — it just no-ops. Uses ``site.addsitedir`` rather than a plain
    ``sys.path.insert`` so ``.pth`` files get processed (torch ships
    a ``.pth`` that registers its internal extension paths).
    """
    if not DEPS_DIR.exists():
        return
    # addsitedir is idempotent-ish — it won't add the same dir twice
    # in a single process, but it will re-process .pth files. That's
    # fine.
    try:
        site.addsitedir(str(DEPS_DIR))
    except Exception as exc:  # never block app startup on this
        logger.debug("neural_env.activate failed: %s", exc)


# ── embeddable Python bootstrap ────────────────────────────────────


def _download(url: str, dest: Path, log_callback=None) -> bool:
    """Stream a URL to ``dest`` with coarse progress reporting.

    Reports every ~5% so the GUI log doesn't drown in lines for big
    wheels. Returns True on success; cleans up a partial file on any
    failure so retries start fresh.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            next_report = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if log_callback and total and downloaded >= next_report:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1024 * 1024)
                        tmb = total / (1024 * 1024)
                        log_callback(f"  {pct:3d}% ({mb:.1f} / {tmb:.1f} MB)")
                        next_report = downloaded + max(total // 20, 1)
        tmp.replace(dest)
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"Download failed: {exc}")
        tmp.unlink(missing_ok=True)
        return False


def _enable_site_in_pth(py_dir: Path, log_callback=None) -> bool:
    """Uncomment the ``import site`` line in python3XX._pth so the
    embedded interpreter runs site.py on startup — pip's install
    paths and our DEPS_DIR both depend on that machinery.
    """
    candidates = list(py_dir.glob("python*._pth"))
    if not candidates:
        if log_callback:
            log_callback(f"No ._pth file found in {py_dir}")
        return False
    pth = candidates[0]
    text = pth.read_text(encoding="utf-8")
    # Common default file has a commented "#import site" near the end.
    if "import site" in text and "#import site" in text:
        text = text.replace("#import site", "import site")
    elif "import site" not in text:
        text = text.rstrip() + "\nimport site\n"
    pth.write_text(text, encoding="utf-8")
    return True


def ensure_embed_python(log_callback=None) -> bool:
    """Download + extract embedded Python and install pip into it.

    Idempotent — reads a ``.ffn-dl-bootstrap-ok`` sentinel so the
    30-second setup runs only once per machine. Returns True when
    ``python_exe()`` is ready to run ``-m pip``.
    """
    if BOOTSTRAP_DONE.exists() and python_exe().exists():
        return True

    PY_DIR.mkdir(parents=True, exist_ok=True)

    if not python_exe().exists():
        if log_callback:
            log_callback(
                f"Downloading Python {PYTHON_EMBED_VERSION} embeddable (~10 MB)..."
            )
        zip_path = PY_DIR / "embed.zip"
        if not _download(PYTHON_EMBED_URL, zip_path, log_callback=log_callback):
            return False
        if log_callback:
            log_callback("Extracting Python...")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(PY_DIR)
        except zipfile.BadZipFile as exc:
            if log_callback:
                log_callback(f"Zip extract failed: {exc}")
            zip_path.unlink(missing_ok=True)
            return False
        zip_path.unlink(missing_ok=True)

    if not _enable_site_in_pth(PY_DIR, log_callback=log_callback):
        return False

    # Bootstrap pip via get-pip.py. The embeddable distribution
    # intentionally ships without pip so we have to install it
    # ourselves; this is the official approach per python.org docs.
    pip_check = subprocess.run(
        [str(python_exe()), "-m", "pip", "--version"],
        capture_output=True, text=True,
    )
    if pip_check.returncode != 0:
        if log_callback:
            log_callback("Bootstrapping pip...")
        get_pip = PY_DIR / "get-pip.py"
        if not _download(GET_PIP_URL, get_pip, log_callback=log_callback):
            return False
        result = subprocess.run(
            [str(python_exe()), str(get_pip), "--no-warn-script-location"],
            capture_output=True, text=True,
        )
        get_pip.unlink(missing_ok=True)
        if result.returncode != 0:
            if log_callback:
                log_callback("get-pip.py failed:")
                for line in (result.stderr or result.stdout or "").splitlines()[-10:]:
                    log_callback(f"  {line}")
            return False

    BOOTSTRAP_DONE.write_text("ok", encoding="utf-8")
    if log_callback:
        log_callback("Python environment ready.")
    return True


# ── package install via embedded Python ────────────────────────────


def pip_install(packages, log_callback=None, extra_args=None) -> bool:
    """Install one or more PyPI packages into ``DEPS_DIR`` via the
    embedded Python. Streams pip's stdout/stderr to ``log_callback``.

    Callers that need CPU-only torch (every neural backend we
    support is CPU-friendly and the CUDA wheels are ~2.5 GB vs
    ~200 MB) pass ``extra_args=['--extra-index-url', 'https://...']``
    so PyPI's dep resolver picks the CPU wheel.
    """
    if not ensure_embed_python(log_callback=log_callback):
        return False

    DEPS_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(python_exe()), "-m", "pip", "install",
        "--target", str(DEPS_DIR),
        "--upgrade",
        "--no-cache-dir",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(packages)

    if log_callback:
        log_callback(f"\nRunning: pip install {' '.join(packages)}")
        log_callback("(This may take several minutes — torch alone is ~200 MB)\n")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        if log_callback:
            log_callback(f"Failed to spawn pip: {exc}")
        return False

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and log_callback:
            log_callback(line)
    rc = proc.wait()
    if rc != 0:
        if log_callback:
            log_callback(f"pip install exited with status {rc}")
        return False
    return True
