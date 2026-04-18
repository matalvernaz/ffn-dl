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
import re
import subprocess
import sys
from typing import Iterable, List

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
    """Return the ``pip install`` argv for a backend, or None if there
    is nothing to install (builtin) or the backend is unknown."""
    info = BACKENDS.get(backend)
    if not info or not info["pip_name"]:
        return None
    return [sys.executable, "-m", "pip", "install", "--upgrade", info["pip_name"]]


def install(backend: str, log_callback=None) -> bool:
    """Install a backend via ``pip`` in a subprocess, streaming output.

    log_callback(line) is invoked for each line of stdout/stderr so a
    GUI can surface progress. Returns True on success.
    """
    cmd = install_command(backend)
    if not cmd:
        return backend == "builtin"

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
    if rc != 0 and log_callback:
        log_callback(f"pip install exited with status {rc}")
    return rc == 0


# ── Dispatcher ──────────────────────────────────────────────────────


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
    if not is_installed(backend):
        logger.warning(
            "Attribution backend %r is not installed; using builtin parser",
            backend,
        )
        return segments
    size = normalize_size(backend, model_size)
    try:
        if backend == "fastcoref":
            return _refine_with_fastcoref(segments, full_text)
        if backend == "booknlp":
            return _refine_with_booknlp(segments, full_text, model_size=size)
    except Exception as exc:  # the whole point is to never blow up the render
        logger.warning(
            "Attribution backend %r failed (%s); falling back to builtin",
            backend, exc,
        )
        return segments

    logger.warning("Unknown attribution backend %r; using builtin", backend)
    return segments


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

    from booknlp.booknlp import BookNLP

    if model_size not in ("small", "big"):
        model_size = "small"

    tmp = Path(tempfile.mkdtemp(prefix="ffn-booknlp-"))
    infile = tmp / "book.txt"
    infile.write_text(full_text, encoding="utf-8")

    model = BookNLP(
        "en",
        {
            "pipeline": "entity,quote,coref",
            "model": model_size,
        },
    )
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
