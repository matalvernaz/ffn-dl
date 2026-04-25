"""Local Piper TTS provider — Rhasspy's ONNX-based speech synthesiser.

Piper runs fully offline and ships strong regional voices (Hagrid-y
West Country English, Scottish, Irish, Welsh, Indian, Australian, plus
French / Spanish / German / Italian / etc.) that edge-tts doesn't
match. Each voice is a pair of files: an ONNX model + a JSON config.
We don't bundle them — they live under ``<portable_root>/piper_models/``
and are downloaded on first use from the official HuggingFace repo
(rhasspy/piper-voices).

Synthesis is a subprocess call to the bundled ``piper`` binary (or the
one on PATH) so we don't need ``onnxruntime`` in the main process. The
provider self-installs the binary the first time the user picks a Piper
voice, fetching from the upstream release manifest.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import VoiceInfo, voice_id

logger = logging.getLogger(__name__)


# ── Voice manifest ─────────────────────────────────────────────────
#
# A curated cross-section of the Piper voice catalog. Picked to give
# every major English regional accent (US/UK/Scottish/Irish/Welsh/
# Australian/Indian) plus the most-requested non-English locales for
# fanfic (French / Spanish / German / Italian / Japanese / Russian).
# Each entry: (short_name, locale, gender, display_name, hf_subpath).
# Adding a voice here doesn't bloat the install — voices are only
# downloaded the first time the VoiceMapper picks them.
#
# The HuggingFace repo path is rhasspy/piper-voices/main/<hf_subpath>.
# A voice's two files are <subpath>/<short>.onnx and
# <subpath>/<short>.onnx.json. Sizes are 25–60 MB per medium voice,
# 8–15 MB per low voice.
_HF_ROOT = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main"
)


_VOICES: list[tuple[str, str, str, str, str]] = [
    # English — US
    ("en_US-amy-medium", "en-US", "Female", "Amy (US)", "en/en_US/amy/medium"),
    ("en_US-libritts-high", "en-US", "Female", "LibriTTS (US, multi)", "en/en_US/libritts/high"),
    ("en_US-ryan-high", "en-US", "Male", "Ryan (US)", "en/en_US/ryan/high"),
    ("en_US-joe-medium", "en-US", "Male", "Joe (US)", "en/en_US/joe/medium"),
    ("en_US-kathleen-low", "en-US", "Female", "Kathleen (US)", "en/en_US/kathleen/low"),
    ("en_US-lessac-medium", "en-US", "Female", "Lessac (US)", "en/en_US/lessac/medium"),
    # English — UK / Scottish / Welsh / Irish
    ("en_GB-alan-medium", "en-GB", "Male", "Alan (UK)", "en/en_GB/alan/medium"),
    ("en_GB-northern_english_male-medium", "en-GB", "Male",
     "Northern English Male (UK)", "en/en_GB/northern_english_male/medium"),
    ("en_GB-southern_english_female-low", "en-GB", "Female",
     "Southern English Female (UK)", "en/en_GB/southern_english_female/low"),
    ("en_GB-jenny_dioco-medium", "en-GB", "Female", "Jenny Dioco (UK)",
     "en/en_GB/jenny_dioco/medium"),
    ("en_GB-cori-high", "en-GB", "Female", "Cori (UK)", "en/en_GB/cori/high"),
    ("en_GB-semaine-medium", "en-GB", "Female", "Semaine (UK)",
     "en/en_GB/semaine/medium"),
    # French
    ("fr_FR-siwis-medium", "fr-FR", "Female", "Siwis (FR)", "fr/fr_FR/siwis/medium"),
    ("fr_FR-tom-medium", "fr-FR", "Male", "Tom (FR)", "fr/fr_FR/tom/medium"),
    ("fr_FR-upmc-medium", "fr-FR", "Male", "UPMC (FR, multi)",
     "fr/fr_FR/upmc/medium"),
    # Spanish
    ("es_ES-mls_10246-low", "es-ES", "Male", "MLS 10246 (ES)",
     "es/es_ES/mls_10246/low"),
    ("es_MX-claude-high", "es-MX", "Female", "Claude (MX)",
     "es/es_MX/claude/high"),
    # German
    ("de_DE-thorsten-high", "de-DE", "Male", "Thorsten (DE)",
     "de/de_DE/thorsten/high"),
    ("de_DE-eva_k-x_low", "de-DE", "Female", "Eva K (DE)",
     "de/de_DE/eva_k/x_low"),
    # Italian
    ("it_IT-paola-medium", "it-IT", "Female", "Paola (IT)",
     "it/it_IT/paola/medium"),
    ("it_IT-riccardo-x_low", "it-IT", "Male", "Riccardo (IT)",
     "it/it_IT/riccardo/x_low"),
    # Russian
    ("ru_RU-irina-medium", "ru-RU", "Female", "Irina (RU)",
     "ru/ru_RU/irina/medium"),
    ("ru_RU-dmitri-medium", "ru-RU", "Male", "Dmitri (RU)",
     "ru/ru_RU/dmitri/medium"),
    # Japanese (single)
    ("ja_JP-test-medium", "ja-JP", "Neutral", "Test (JP)", "ja/ja_JP/test/medium"),
    # Dutch / Swedish / Polish / Portuguese (one each)
    ("nl_NL-mls_5809-low", "nl-NL", "Male", "MLS 5809 (NL)",
     "nl/nl_NL/mls_5809/low"),
    ("sv_SE-nst-medium", "sv-SE", "Male", "NST (SE)", "sv/sv_SE/nst/medium"),
    ("pl_PL-darkman-medium", "pl-PL", "Male", "Darkman (PL)",
     "pl/pl_PL/darkman/medium"),
    ("pt_BR-faber-medium", "pt-BR", "Male", "Faber (BR)",
     "pt/pt_BR/faber/medium"),
]


def piper_models_dir() -> Path:
    """Where downloaded ONNX voices live. Mirrors the BookNLP and
    booknlp_models / neural / cache folders that already sit next to
    the portable exe; pip-installed ffn-dl falls back to the user's
    home dir."""
    try:
        from .. import portable
        return portable.portable_root() / "piper_models"
    except Exception:
        return Path.home() / "piper_models"


def piper_binary_dir() -> Path:
    """Where the bundled piper binary lives once installed."""
    try:
        from .. import portable
        return portable.portable_root() / "piper_bin"
    except Exception:
        return Path.home() / ".local" / "share" / "piper"


def piper_executable() -> str | None:
    """Locate the piper binary — first on PATH (pip-installed users may
    have ``piper-tts`` in their venv), then in the ffn-dl-managed dir
    that ``install_piper_binary`` writes to. Returns None if neither
    works, which is the cue for the GUI to surface an Install button."""
    on_path = shutil.which("piper")
    if on_path:
        return on_path
    candidate = piper_binary_dir() / ("piper.exe" if os.name == "nt" else "piper")
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


# ── Binary install ────────────────────────────────────────────────


_PIPER_RELEASE = "2023.11.14-2"  # last upstream release as of 2026-04
_PIPER_RELEASE_BASE = (
    "https://github.com/rhasspy/piper/releases/download/"
)


def _piper_release_asset() -> tuple[str, str] | None:
    """Resolve the upstream release asset filename for this platform.

    Returns (filename, archive-format). Pip-installable platforms not
    covered by the release matrix fall through to None — the user is
    expected to ``pip install piper-tts`` themselves on those.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows" and machine in ("amd64", "x86_64"):
        return ("piper_windows_amd64.zip", "zip")
    if system == "linux" and machine in ("amd64", "x86_64"):
        return ("piper_linux_x86_64.tar.gz", "tar")
    if system == "linux" and machine in ("aarch64", "arm64"):
        return ("piper_linux_aarch64.tar.gz", "tar")
    if system == "darwin" and machine in ("arm64", "aarch64"):
        return ("piper_macos_aarch64.tar.gz", "tar")
    if system == "darwin" and machine in ("amd64", "x86_64"):
        return ("piper_macos_x64.tar.gz", "tar")
    return None


def install_piper_binary(log_callback=None) -> bool:
    """Download and unpack the upstream Piper release into
    ``piper_binary_dir()``. Idempotent: if a binary is already present,
    returns True without re-downloading. Surfaces progress / failure
    through ``log_callback`` so the GUI install button can mirror it
    inline."""
    if piper_executable() is not None:
        return True
    asset = _piper_release_asset()
    if asset is None:
        if log_callback:
            log_callback(
                "Piper auto-install isn't supported on this platform. "
                "Install it manually (pip install piper-tts) and put "
                "'piper' on PATH."
            )
        return False
    name, fmt = asset
    url = f"{_PIPER_RELEASE_BASE}{_PIPER_RELEASE}/{name}"
    target_dir = piper_binary_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / name
    try:
        if log_callback:
            log_callback(f"Piper: downloading {name} ...")
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        archive.write_bytes(data)
        if fmt == "zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(target_dir)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(target_dir)
        # Some release archives unpack into a nested ``piper/`` dir;
        # flatten it so piper_executable() finds the binary directly.
        nested = target_dir / "piper"
        if nested.is_dir() and nested != target_dir:
            for item in nested.iterdir():
                shutil.move(str(item), str(target_dir / item.name))
            try:
                nested.rmdir()
            except OSError:
                pass
        binary = target_dir / ("piper.exe" if os.name == "nt" else "piper")
        if binary.exists() and os.name != "nt":
            try:
                binary.chmod(binary.stat().st_mode | 0o111)
            except OSError:
                pass
        archive.unlink(missing_ok=True)
        if log_callback:
            log_callback(f"Piper: binary installed at {binary}")
        return piper_executable() is not None
    except (urllib.error.URLError, OSError, zipfile.BadZipFile,
            tarfile.TarError) as exc:
        if log_callback:
            log_callback(f"Piper install failed: {exc}")
        return False


# ── Voice download ────────────────────────────────────────────────


def _voice_files(short_name: str) -> tuple[Path, Path]:
    """Return ``(onnx_path, json_path)`` for a voice — wherever they
    live on disk (downloaded or not)."""
    base = piper_models_dir() / short_name
    return (base.with_suffix(".onnx"), base.with_suffix(".onnx.json"))


def voice_is_downloaded(short_name: str) -> bool:
    onnx, cfg = _voice_files(short_name)
    return onnx.exists() and cfg.exists() and onnx.stat().st_size > 1024


def _voice_subpath(short_name: str) -> str | None:
    for s, _loc, _g, _d, sub in _VOICES:
        if s == short_name:
            return sub
    return None


def download_voice(short_name: str, log_callback=None) -> bool:
    """Fetch a voice's ``.onnx`` + ``.onnx.json`` pair from the
    HuggingFace repo. Returns True on success. Caller is expected to
    have already checked ``voice_is_downloaded()``."""
    sub = _voice_subpath(short_name)
    if sub is None:
        if log_callback:
            log_callback(f"Unknown Piper voice: {short_name}")
        return False
    base_url = f"{_HF_ROOT}/{sub}/{short_name}"
    onnx_target, cfg_target = _voice_files(short_name)
    onnx_target.parent.mkdir(parents=True, exist_ok=True)
    for url, target in (
        (base_url + ".onnx", onnx_target),
        (base_url + ".onnx.json", cfg_target),
    ):
        try:
            if log_callback:
                log_callback(f"Piper: fetching {target.name} ...")
            with urllib.request.urlopen(url, timeout=120) as resp:
                target.write_bytes(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            if log_callback:
                log_callback(f"Piper: download failed for {target.name}: {exc}")
            target.unlink(missing_ok=True)
            return False
    if log_callback:
        log_callback(f"Piper: voice {short_name} ready.")
    return True


# ── Provider class ────────────────────────────────────────────────


_RATE_FALLBACK_LENGTH_SCALE = {
    # Piper has no rate-percentage knob; it has --length_scale where
    # smaller = faster. Map the edge-tts-style "+10%" / "-15%" strings
    # to length-scale values that produce roughly the same perceived
    # speed shift. Identity is 1.0.
}


def _length_scale_from_rate(rate: str | None) -> float:
    if not rate:
        return 1.0
    try:
        pct = int(rate.rstrip("%"))
    except ValueError:
        return 1.0
    # +10% rate = 10% faster speech = 0.9 length_scale (shorter durations)
    return max(0.5, min(2.0, 1.0 - pct / 100.0))


class PiperProvider:
    """Local Piper TTS provider exposing the same interface as Edge."""

    name = "piper"

    def is_installed(self) -> bool:
        return piper_executable() is not None

    def list_voices(self) -> list[VoiceInfo]:
        out: list[VoiceInfo] = []
        for short, locale, gender, display, _sub in _VOICES:
            downloaded = voice_is_downloaded(short)
            description = "downloaded" if downloaded else "click Install to download"
            out.append(
                VoiceInfo(
                    id=voice_id(self.name, short),
                    provider=self.name,
                    short_name=short,
                    locale=locale,
                    gender=gender,
                    display=display,
                    description=description,
                )
            )
        return out

    def synthesize(
        self, *, text: str, voice: str, output_path: Path,
        rate: str | None = None,
        volume: str | None = None,  # piper has no native volume knob
        pitch: str | None = None,   # piper has no native pitch knob
    ) -> None:
        binary = piper_executable()
        if not binary:
            raise RuntimeError(
                "Piper is not installed. Click 'Manage TTS providers...' "
                "in the audio toolbar to install it, or `pip install piper-tts`."
            )
        if not voice_is_downloaded(voice):
            ok = download_voice(voice)
            if not ok:
                raise RuntimeError(
                    f"Piper voice {voice!r} could not be downloaded; "
                    "check network connectivity or pick a different voice."
                )
        onnx, _cfg = _voice_files(voice)
        length_scale = _length_scale_from_rate(rate)
        # Piper writes WAV to stdout when --output_file is given. We
        # ask for an MP3 by piping into ffmpeg, matching the rest of
        # the audiobook pipeline (every chapter mp3 is concat'd).
        from ..tts import _find_tool  # ffmpeg locator
        ffmpeg = _find_tool("ffmpeg")
        wav_tmp = output_path.with_suffix(".piper.wav")
        try:
            cmd = [
                binary,
                "--model", str(onnx),
                "--length_scale", f"{length_scale:.3f}",
                "--output_file", str(wav_tmp),
            ]
            proc = subprocess.run(
                cmd, input=text, text=True, capture_output=True, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"piper exit {proc.returncode}: {(proc.stderr or '').strip()[-300:]}"
                )
            convert = subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-i", str(wav_tmp),
                    "-codec:a", "libmp3lame", "-qscale:a", "4",
                    str(output_path),
                ],
                capture_output=True, text=True,
            )
            if convert.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg WAV→MP3 exit {convert.returncode}: "
                    f"{(convert.stderr or '').strip()[-300:]}"
                )
        finally:
            wav_tmp.unlink(missing_ok=True)


# Re-exported helpers so the GUI / CLI can drive install / download
# without importing the provider class directly.
__all__ = [
    "PiperProvider",
    "piper_models_dir",
    "piper_binary_dir",
    "piper_executable",
    "install_piper_binary",
    "voice_is_downloaded",
    "download_voice",
]
