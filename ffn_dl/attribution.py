"""Pluggable speaker-attribution backends for audiobook generation.

The built-in pipeline in `tts.py` already parses dialogue and assigns
speakers via regex + heuristics. For tougher cases (pronoun-heavy
prose, unconventional attribution, dense multi-speaker scenes) users
can opt into a neural refinement pass that runs after `parse_segments`.

Backends ship as optional extras — the core ffn-dl install never
requires them. Each backend exposes:

    - is_installed() → bool  (fast, import-free check)
    - refine(segments, full_text) → list[Segment]  (returns new list;
      may mutate segments in place)

`refine_speakers(segments, full_text, backend)` is the main dispatcher;
unknown or uninstalled backends degrade silently to the builtin no-op.
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import sys
from typing import Iterable, List


def _is_frozen() -> bool:
    """True when running inside a PyInstaller bundle. In that mode
    ``sys.executable`` is the .exe bootloader rather than a Python
    interpreter, so ``sys.executable -m pip`` would route the pip
    flags into ffn-dl's own argparse. The frozen codepath instead
    uses ``neural_env`` to install into a sibling embedded Python."""
    return bool(getattr(sys, "frozen", False))


# Extra pip args per backend — keep torch on CPU wheels so we don't
# pull the ~2.5 GB CUDA build when all we need is inference.
_EXTRA_ARGS = {
    "fastcoref": [
        "--extra-index-url", "https://download.pytorch.org/whl/cpu",
    ],
    "booknlp": [
        "--extra-index-url", "https://download.pytorch.org/whl/cpu",
    ],
}

logger = logging.getLogger(__name__)


# ── Registry ────────────────────────────────────────────────────────

# Each entry: distribution-name used with pip. The import name may
# differ; we record both where they do ("booknlp" installs as "booknlp"
# and imports as "booknlp", fastcoref same).
BACKENDS = {
    "builtin": {
        "pip_name": None,  # built-in; nothing to install
        "import_name": None,
        "display": "Built-in regex (fast, no download)",
        "size_hint": "0 MB",
        "description": (
            "The default parser. No extra models or downloads. "
            "Works well for clearly-attributed dialogue."
        ),
        "sizes": None,       # no size variants for this backend
        "default_size": None,
    },
    "fastcoref": {
        "pip_name": "fastcoref",
        "import_name": "fastcoref",
        "display": "fastcoref (coref refinement, ~90 MB)",
        "size_hint": "~90 MB",
        "description": (
            "Runs fast neural coreference over the full text and "
            "remaps pronoun-attributed lines ('he said') to the "
            "correct named character from the coref chain."
        ),
        "sizes": None,
        "default_size": None,
    },
    "booknlp": {
        "pip_name": "booknlp",
        "import_name": "booknlp",
        "display": "BookNLP (full attribution)",
        "size_hint": "~150 MB small / ~1 GB big",
        "description": (
            "Replaces our attribution with BookNLP's quote + coref "
            "models (Bamman et al.). Most accurate on long novels. "
            "Models are downloaded on first use — see Model size."
        ),
        "sizes": {
            "small": {
                "display": "Small (faster, ~150 MB)",
                "size_hint": "~150 MB",
                "description": (
                    "Distilled models — several minutes per novel "
                    "on CPU, solid accuracy for most stories."
                ),
            },
            "big": {
                "display": "Big (most accurate, ~1 GB)",
                "size_hint": "~1 GB",
                "description": (
                    "Full-size BERT-base models — slower (~15 min "
                    "per 100k-token novel on CPU) but highest "
                    "speaker-attribution accuracy."
                ),
            },
        },
        "default_size": "small",
    },
}


def sizes_for(backend: str) -> dict | None:
    """Return the sizes dict for a backend, or None if it has no size
    variants. UI uses this to decide whether to show a size dropdown."""
    info = BACKENDS.get(backend) or {}
    return info.get("sizes") or None


def default_size(backend: str) -> str | None:
    info = BACKENDS.get(backend) or {}
    return info.get("default_size")


def normalize_size(backend: str, size: str | None) -> str | None:
    """Clamp `size` to one this backend supports. Returns None when the
    backend has no size variants. Falls back to the backend's default
    when `size` is unknown or missing."""
    sizes = sizes_for(backend)
    if not sizes:
        return None
    if size and size in sizes:
        return size
    return default_size(backend)


def available() -> List[str]:
    """Ordered list of backend names suitable for a UI dropdown."""
    return ["builtin", "fastcoref", "booknlp"]


def is_installed(backend: str) -> bool:
    """True if the backend can be imported right now.

    "builtin" is always installed. For the others, we try a cheap
    ``importlib.util.find_spec`` — no actual import, so this is safe
    to call repeatedly from a UI.
    """
    if backend == "builtin":
        return True
    info = BACKENDS.get(backend)
    if not info or not info["import_name"]:
        return False
    try:
        return importlib.util.find_spec(info["import_name"]) is not None
    except (ImportError, ValueError):
        return False


def install_command(backend: str) -> List[str] | None:
    """Return the ``pip install`` argv for a backend when not frozen.

    Returns None for the builtin backend, unknown backends, or when
    running as a frozen .exe — the frozen path doesn't shell out to
    pip directly, it goes through ``neural_env.pip_install`` which
    uses a separate embedded Python interpreter.
    """
    info = BACKENDS.get(backend)
    if not info or not info["pip_name"]:
        return None
    if _is_frozen():
        return None
    return [sys.executable, "-m", "pip", "install", "--upgrade", info["pip_name"]]


def install_unsupported_reason(backend: str) -> str | None:
    """Return a human-readable reason why ``install(backend)`` would
    refuse to run, or None if installation is supported.

    Installation IS supported in the frozen .exe (via neural_env).
    The only unsupported case is frozen non-Windows builds, which we
    don't actually ship — included so future platforms fail loudly
    instead of silently doing nothing.
    """
    info = BACKENDS.get(backend) or {}
    if not info.get("pip_name"):
        return None  # builtin — no install needed
    if _is_frozen():
        try:
            from . import neural_env
        except ImportError:
            return (
                "The embedded Python helper (neural_env) isn't available "
                "in this build — neural backends can't be installed."
            )
        if not neural_env.is_supported():
            return (
                "Neural backend installation from the standalone build "
                "is only supported on Windows. Install ffn-dl from PyPI "
                "on other platforms."
            )
    return None


def install(backend: str, log_callback=None) -> bool:
    """Install a backend, streaming pip's output to ``log_callback``.

    In a pip-installed ffn-dl this just runs
    ``sys.executable -m pip install <backend>``. In the frozen .exe it
    routes through ``neural_env``, which lazily downloads an
    embeddable Python on first use and pip-installs into a user dir
    that ``ffn_dl/__init__.py`` adds to ``sys.path`` at startup.

    Returns True on success. Never raises — failures surface through
    ``log_callback`` so the GUI can report them inline.
    """
    if backend == "builtin":
        return True

    info = BACKENDS.get(backend)
    if not info or not info["pip_name"]:
        return False

    reason = install_unsupported_reason(backend)
    if reason:
        if log_callback:
            for line in reason.splitlines():
                log_callback(line)
        return False

    if _is_frozen():
        try:
            from . import neural_env
        except ImportError as exc:
            if log_callback:
                log_callback(f"neural_env unavailable: {exc}")
            return False
        if not neural_env.pip_install(
            [info["pip_name"]],
            log_callback=log_callback,
            extra_args=_EXTRA_ARGS.get(backend),
        ):
            return False
    else:
        # Non-frozen path — use sys.executable's pip directly.
        cmd = install_command(backend)
        if not cmd:
            return False

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            if log_callback:
                log_callback(f"Failed to launch pip: {exc}")
            return False

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if log_callback and line:
                log_callback(line)
        rc = proc.wait()
        if rc != 0:
            if log_callback:
                log_callback(f"pip install exited with status {rc}")
            return False

    # BookNLP needs spaCy's en_core_web_sm at runtime; pip won't pull
    # it transitively. Fetch it now so first use doesn't stall or fail.
    if backend == "booknlp" and not _ensure_spacy_model(
        "en_core_web_sm", log_callback=log_callback,
    ):
        if log_callback:
            log_callback(
                "Warning: spaCy model en_core_web_sm could not be "
                "downloaded — BookNLP will fall back to builtin at run time."
            )
        # Don't fail the whole install — first-use also retries the
        # download, and a retry of this button will try again too.
    return True


# ── Dispatcher ──────────────────────────────────────────────────────

# Track backends that have already failed once this run so we don't
# repeat the same warning for every chapter of a multi-chapter book.
# Keyed by (backend, size) so a later call with different params can
# still attempt refinement. Cleared only on process exit.
_failed_runs: set[tuple[str, str | None]] = set()


def refine_speakers(
    segments, full_text: str,
    backend: str = "builtin",
    model_size: str | None = None,
):
    """Apply the chosen backend's refinement to `segments` (in order).

    `model_size` picks a size variant for backends that expose them
    (currently only BookNLP: "small" or "big"). Ignored for backends
    without size variants.

    Returns the possibly-updated segment list. On any error the
    builtin no-op is used and a warning is logged — audiobook
    generation must never fail because a neural dep is missing.
    """
    if backend in (None, "", "builtin"):
        return segments
    size = normalize_size(backend, model_size)
    key = (backend, size)
    if key in _failed_runs:
        return segments  # already reported; stay silent for remaining chapters
    if not is_installed(backend):
        logger.warning(
            "Attribution backend %r is not installed; using builtin parser",
            backend,
        )
        _failed_runs.add(key)
        return segments
    try:
        if backend == "fastcoref":
            return _refine_with_fastcoref(segments, full_text)
        if backend == "booknlp":
            return _refine_with_booknlp(segments, full_text, model_size=size)
    except Exception as exc:  # the whole point is to never blow up the render
        logger.warning(
            "Attribution backend %r failed (%s); falling back to builtin "
            "for the rest of this render",
            backend, exc,
        )
        _failed_runs.add(key)
        return segments

    logger.warning("Unknown attribution backend %r; using builtin", backend)
    _failed_runs.add(key)
    return segments


# ── spaCy model bootstrap ──────────────────────────────────────────


# BookNLP imports spaCy and loads ``en_core_web_sm`` on every
# ``process()`` call. Pip doesn't pull spaCy models automatically, so a
# fresh ``pip install booknlp`` leaves this missing. We check on first
# use and attempt a one-shot ``spacy download`` to self-heal existing
# installs — new installs also get it proactively from ``install()``.
_spacy_model_checked: set[str] = set()


def _spacy_model_available(model_name: str) -> bool:
    try:
        return importlib.util.find_spec(model_name) is not None
    except (ImportError, ValueError):
        return False


def _spacy_download(model_name: str, log_callback=None) -> bool:
    """Run ``spacy download <model>`` against the right interpreter.

    Frozen builds go through ``neural_env.run_python`` so the command
    lands in the embedded Python where spaCy is installed. Everything
    else uses ``sys.executable`` directly.
    """
    args = ["-m", "spacy", "download", model_name]
    if _is_frozen():
        try:
            from . import neural_env
        except ImportError as exc:
            if log_callback:
                log_callback(f"neural_env unavailable: {exc}")
            return False
        return neural_env.run_python(args, log_callback=log_callback)

    cmd = [sys.executable, *args]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        if log_callback:
            log_callback(f"Failed to launch spacy: {exc}")
        return False

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and log_callback:
            log_callback(line)
    return proc.wait() == 0


def _ensure_spacy_model(model_name: str, log_callback=None) -> bool:
    """Make sure ``model_name`` is importable; attempt a download once
    per process if it isn't. Returns True if the model is available
    afterwards. Repeated calls within a process short-circuit."""
    if _spacy_model_available(model_name):
        return True
    if model_name in _spacy_model_checked:
        return False
    _spacy_model_checked.add(model_name)

    msg = f"spaCy model {model_name!r} not found; downloading..."
    if log_callback:
        log_callback(msg)
    else:
        logger.info(msg)

    ok = _spacy_download(model_name, log_callback=log_callback)
    # Invalidate importlib's finder cache so the freshly-installed
    # package is discoverable without restarting the process.
    importlib.invalidate_caches()
    return ok and _spacy_model_available(model_name)


# ── fastcoref adapter ──────────────────────────────────────────────


_PRONOUN_TOKENS = {
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "they", "them", "their", "theirs", "themselves",
    "it", "its",
}


def _refine_with_fastcoref(segments, full_text):
    """Use fastcoref's coref clusters to remap pronoun speakers to the
    correct named antecedent.

    We only touch segments whose current speaker is None or looks like
    a pronoun. For each such segment, we find the character offset of
    the nearest preceding pronoun (from the speaker attribution in the
    original text window), look up its coref cluster, and pick the
    longest non-pronoun mention in that cluster as the speaker name.
    """
    from fastcoref import FCoref

    model = FCoref(device="cpu")
    preds = model.predict(texts=[full_text])
    if not preds:
        return segments
    clusters = preds[0].get_clusters()  # list[list[(start_char, end_char)]]
    if not clusters:
        return segments

    # Build a helper: for each char position in the text, which
    # cluster (if any) contains it? Only store cluster indices for
    # character positions that fall inside a mention.
    pos_to_cluster = {}
    for idx, cluster in enumerate(clusters):
        for start, end in cluster:
            for c in range(start, end):
                pos_to_cluster[c] = idx

    def _cluster_canonical(cluster_idx):
        # The longest non-pronoun mention is the canonical name.
        best = None
        for start, end in clusters[cluster_idx]:
            span = full_text[start:end].strip()
            if not span:
                continue
            low = span.lower()
            if low in _PRONOUN_TOKENS:
                continue
            if best is None or len(span) > len(best):
                best = span
        return best

    # Walk segments and refine. We need character offsets for each
    # segment; reconstruct by re-scanning full_text for each segment's
    # text in order (O(n) total, good enough for chapters).
    cursor = 0
    for seg in segments:
        if not seg.text:
            continue
        idx = full_text.find(seg.text, cursor)
        if idx < 0:
            idx = full_text.find(seg.text.strip('"\u201c\u201d'), cursor)
        if idx < 0:
            continue
        cursor = idx + len(seg.text)

        current = (seg.speaker or "").lower()
        needs_refine = (
            seg.speaker is None
            or current in _PRONOUN_TOKENS
            or re.fullmatch(r"he|she|they|it", current or "") is not None
        )
        if not needs_refine:
            continue

        # Find the pronoun that attributed this segment — look at the
        # next 60 chars past the dialogue for "he said" / "said he"
        # patterns, and pick up the pronoun's position.
        tail = full_text[cursor : cursor + 80]
        pronoun_match = re.search(r"\b(he|she|they|it)\b", tail, flags=re.IGNORECASE)
        if not pronoun_match:
            continue
        abs_pos = cursor + pronoun_match.start()
        cluster_idx = pos_to_cluster.get(abs_pos)
        if cluster_idx is None:
            continue
        canonical = _cluster_canonical(cluster_idx)
        if canonical:
            seg.speaker = canonical

    return segments


# ── BookNLP adapter ────────────────────────────────────────────────


# BookNLP model construction loads ~150 MB / ~1 GB of weights and
# several spaCy / PyTorch components. Cache per model_size so a
# multi-chapter render doesn't reload everything on every chapter.
_booknlp_cache: dict[str, object] = {}


def _get_booknlp_model(model_size: str):
    if model_size in _booknlp_cache:
        return _booknlp_cache[model_size]
    from booknlp.booknlp import BookNLP
    model = BookNLP(
        "en",
        {
            "pipeline": "entity,quote,coref",
            "model": model_size,
        },
    )
    _booknlp_cache[model_size] = model
    return model


def _refine_with_booknlp(segments, full_text, model_size="small"):
    """Run BookNLP over the full text, parse its quote + entity output,
    and overwrite segment speakers with BookNLP's canonical character
    names.

    BookNLP returns quotes keyed by token offsets; we remap to character
    offsets through its tokens TSV and then align to our segments by
    substring position.

    model_size is "small" (~150 MB, default) or "big" (~1 GB, higher
    accuracy). BookNLP downloads model weights lazily on first use.
    """
    import csv
    import tempfile
    from pathlib import Path

    if model_size not in ("small", "big"):
        model_size = "small"

    # BookNLP loads spaCy's en_core_web_sm inside .process(). pip won't
    # install it transitively, so older BookNLP installs can be missing
    # it; fetch on first use as a self-heal.
    if not _ensure_spacy_model("en_core_web_sm"):
        raise RuntimeError(
            "spaCy model en_core_web_sm is not available and the "
            "automatic download failed — reinstall BookNLP or run "
            "`python -m spacy download en_core_web_sm` manually."
        )

    model = _get_booknlp_model(model_size)

    tmp = Path(tempfile.mkdtemp(prefix="ffn-booknlp-"))
    infile = tmp / "book.txt"
    infile.write_text(full_text, encoding="utf-8")
    model.process(str(infile), str(tmp), "book")

    # Token offsets → character offsets
    tokens_file = tmp / "book.tokens"
    tok_char = {}  # token_id → start_char
    if tokens_file.exists():
        with open(tokens_file, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                try:
                    tok_char[int(row["token_ID_within_document"])] = int(
                        row.get("byte_onset") or row.get("start_token") or 0
                    )
                except (KeyError, ValueError):
                    continue

    # Entity names per coref ID — pick longest PROP mention per group.
    entities_file = tmp / "book.entities"
    canonical = {}  # coref_id → canonical name string
    if entities_file.exists():
        with open(entities_file, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                cat = row.get("cat", "")
                if cat != "PROP":
                    continue
                try:
                    cid = int(row["COREF"])
                except (KeyError, ValueError):
                    continue
                text = (row.get("text") or "").strip()
                if not text:
                    continue
                prev = canonical.get(cid)
                if prev is None or len(text) > len(prev):
                    canonical[cid] = text

    # Quotes: (start_token, end_token, mention_start, mention_end, text, mention_phrase, char_id)
    quotes_file = tmp / "book.quotes"
    quote_spans = []  # list of (start_char, end_char, speaker_name)
    if quotes_file.exists():
        with open(quotes_file, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                try:
                    start_tok = int(row["quote_start"])
                    end_tok = int(row["quote_end"])
                    cid = int(row.get("char_id") or row.get("mention_speaker_id") or -1)
                except (KeyError, ValueError):
                    continue
                if cid < 0:
                    continue
                name = canonical.get(cid)
                if not name:
                    continue
                start = tok_char.get(start_tok)
                end = tok_char.get(end_tok)
                if start is None or end is None:
                    continue
                quote_spans.append((start, end, name))

    # Align to our segments by substring search; preserve order.
    cursor = 0
    quote_spans.sort()
    qi = 0
    for seg in segments:
        if not seg.text:
            continue
        idx = full_text.find(seg.text, cursor)
        if idx < 0:
            idx = full_text.find(seg.text.strip('"\u201c\u201d'), cursor)
        if idx < 0:
            continue
        cursor = idx + len(seg.text)
        # Advance qi to the first span overlapping this segment
        while qi < len(quote_spans) and quote_spans[qi][1] < idx:
            qi += 1
        if qi < len(quote_spans):
            qstart, qend, name = quote_spans[qi]
            if qstart <= idx < qend or idx <= qstart < cursor:
                seg.speaker = name

    return segments
