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


class LLMUnavailable(RuntimeError):
    """The configured LLM endpoint refused the connection, timed out,
    or wasn't resolvable.

    Distinct from a ``RuntimeError`` raised on a malformed reply or a
    rejected HTTP status: those are per-call problems, but an
    unavailable endpoint is the same failure for every subsequent
    chapter in a download. Callers that loop over chapters can catch
    this once and stop trying — see the chapter loops in
    ``ffn_dl.exporters``."""


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
    "llm": {
        "pip_name": None,  # uses urllib + a remote/local HTTP API
        "import_name": None,
        "display": "LLM (Ollama / OpenAI / Anthropic)",
        "size_hint": "API",
        "description": (
            "Sends each chapter to a Large Language Model and asks it "
            "to label the speaker of every quoted line, grounded by the "
            "story's character list. Choose between a local Ollama "
            "endpoint (no key, runs offline) or a remote provider "
            "(OpenAI / Anthropic / OpenAI-compatible) that needs an "
            "API key. Latest research puts well-prompted LLMs above "
            "BookNLP-big on quotation attribution accuracy."
        ),
        "sizes": None,  # provider/model live in dedicated config
        "default_size": None,
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
    return ["builtin", "fastcoref", "booknlp", "llm"]


def is_installed(backend: str) -> bool:
    """True if the backend can be imported right now.

    "builtin" and "llm" are always installed (the LLM adapter only
    needs urllib + json from the stdlib). For the others, we try a
    cheap ``importlib.util.find_spec`` — no actual import, so this is
    safe to call repeatedly from a UI.
    """
    if backend in ("builtin", "llm"):
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
        # First-ever install creates DEPS_DIR after startup's activate()
        # already no-oped. Re-activate so DEPS_DIR lands on sys.path —
        # otherwise the post-install _ensure_spacy_model check can't see
        # the model it just downloaded.
        neural_env.activate()
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


def has_failed(backend: str, model_size: str | None = None) -> bool:
    """True if this backend already fell back to builtin in this run.

    The caller (tts.py audiobook pipeline) consults this after each
    `refine_speakers` call so it can avoid persisting unrefined builtin
    segments under the requested-backend's cache key — which would
    otherwise look like a successful BookNLP/fastcoref result on the
    next render and skip the real refinement entirely.
    """
    return (backend, normalize_size(backend, model_size)) in _failed_runs


def refine_speakers(
    segments, full_text: str,
    backend: str = "builtin",
    model_size: str | None = None,
    character_list: Iterable[str] | None = None,
    llm_config: dict | None = None,
):
    """Apply the chosen backend's refinement to `segments` (in order).

    `model_size` picks a size variant for backends that expose them
    (currently only BookNLP: "small" or "big"). Ignored for backends
    without size variants.

    `character_list` is the story's metadata-derived cast list (e.g.
    AO3 character tags or FFN's third bare-segment). It serves as a
    closed-world prior — names matching the list are trusted, names
    that don't aren't necessarily wrong. Backends use it differently:
    LLM bakes it into the prompt; heuristic backends pass it to
    ``post_refine`` so junk-speaker / self-intro passes treat them as
    confirmed speakers even on a single occurrence.

    `llm_config` is required when ``backend == "llm"`` and carries the
    provider/model/key/endpoint. Ignored for other backends.

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
        if backend == "llm":
            if not llm_config:
                raise RuntimeError(
                    "llm backend selected but no llm_config provided"
                )
            return _refine_with_llm(
                segments, full_text,
                character_list=character_list,
                **llm_config,
            )
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


# ── Post-attribution refinement ────────────────────────────────────
#
# These passes run on the combined per-chapter segment lists after the
# chosen backend (builtin or neural) has attributed what it can. They
# target two pattern classes that both backends struggle with:
#
# 1. *Self-introductions* — "I'm Ron, by the way, Ron Weasley." BookNLP
#    coref can only link quotes to entities it has already seen; the
#    first line of a new character introducing themselves therefore
#    tends to get stuck on the previous speaker by carryforward.
#
# 2. *Junk speakers* from BookNLP's PROP entity detection — capitalised
#    spell names / species / places ("Cruciatus", "Veela", "Wizard",
#    "Barrier", "Scroll", "Unknown"…) occasionally win a quote
#    attribution in fantasy prose. Anything single-word that only
#    appears once in the entire book and matches our common-noun
#    blocklist gets demoted back to narrator.


# Common capitalised fanfic-prose nouns BookNLP sometimes mis-tags as
# speakers. Kept lowercase; check is case-insensitive. Deliberately
# narrow — names like "Dragon", "Phoenix", "Raven" CAN be real character
# names (Reyna, Fleur, Percy Jackson fic, DCU fic), so the demotion only
# fires when the speaker also appears exactly once in the whole book.
_FANFIC_JUNK_NAMES = frozenset({
    # Species / classifications
    "wizard", "witch", "muggle", "squib", "goblin", "dwarf", "elf",
    "veela", "werewolf", "vampire", "centaur", "giant", "basilisk",
    "thestral", "nundu", "hippogriff", "niffler", "fwooper", "puffskein",
    "metamorphmagus", "animagus", "parselmouth", "legilimens", "occlumens",
    "pureblood", "halfblood", "mudblood", "blood-traitor",
    "firstyear", "firstyears", "seventhyear",
    # Roles / titles that fic capitalises
    "hunter", "seeker", "beater", "keeper", "chaser", "captain",
    "prefect", "prefects", "headboy", "headgirl",
    "spellcrafter", "spellmaker", "warder", "duelist", "duelists",
    "auror", "unspeakable", "dementor", "deatheater",
    "champion", "champions", "professor", "professors",
    "first-year", "first-years", "seventh-year", "seventh-years",
    # BookNLP sentinels / generic narrative nouns
    "unknown", "stranger", "another", "reading", "writing",
    "password", "for", "forge", "another",
    # Objects often capitalised in fantasy prose
    "barrier", "scroll", "fireball", "fireballs", "portkey", "pensieve",
    "patronus", "horcrux", "wand", "diary", "beans", "cushioning",
    "harpoon", "lightning", "expulso", "cruciatus", "disillusionment",
    "attraction", "principle", "aspect", "metamorphmagus",
    # Places / institutions
    "alley", "ministry", "beauxbatons", "durmstrang", "hogwarts",
    "hogsmeade", "azkaban", "gringotts", "house",
    # Short connectives the proper-noun regex sometimes grabs
    "heir", "duelists",
})


_SELF_INTRO_PATTERNS = [
    # "I'm Ron, by the way, Ron Weasley."  /  "I'm Hermione Granger"
    re.compile(
        r"\bI[\'\u2019]m\s+(?P<name>[A-Z][a-zA-Z\']*[a-z]"
        r"(?:\s+[A-Z][a-zA-Z\']*[a-z])?)\b"
    ),
    # "I am Ron" / "I am Ron Weasley"
    re.compile(
        r"\bI\s+am\s+(?P<name>[A-Z][a-zA-Z\']*[a-z]"
        r"(?:\s+[A-Z][a-zA-Z\']*[a-z])?)\b"
    ),
    # "My name is X" / "My name's X Y"
    re.compile(
        r"\bMy\s+name[\'\u2019]?s?\s+(?:is\s+)?(?P<name>"
        r"[A-Z][a-zA-Z\']*[a-z](?:\s+[A-Z][a-zA-Z\']*[a-z])?)\b"
    ),
    # "The name is Ron" / "The name's Bond, James Bond"
    re.compile(
        r"\bThe\s+name[\'\u2019]?s?\s+(?:is\s+)?(?P<name>"
        r"[A-Z][a-zA-Z\']*[a-z](?:\s+[A-Z][a-zA-Z\']*[a-z])?)\b"
    ),
    # "Call me X" / "You can call me X"
    re.compile(
        r"\b[Cc]all\s+me\s+(?P<name>[A-Z][a-zA-Z\']*[a-z]"
        r"(?:\s+[A-Z][a-zA-Z\']*[a-z])?)\b"
    ),
    # ", by the way, Ron Weasley." — dangling self-intro appended after
    # the main utterance. Requires at least a first name + surname to
    # reduce the risk of over-firing on possessive "my way" phrases.
    re.compile(
        r",\s*by\s+the\s+way[,.\s]+(?:my\s+name[\'\u2019]?s?\s+(?:is\s+)?)?"
        r"(?P<name>[A-Z][a-zA-Z\']*[a-z]\s+[A-Z][a-zA-Z\']*[a-z])\b"
    ),
]


def _extract_self_intro_name(text: str, known_speakers: Iterable[str]):
    """If a dialogue line contains an explicit self-introduction,
    return the name the speaker is claiming. Validation against the
    book-wide `known_speakers` set prevents misfires on adjective tails
    after "I'm" ("I'm Cold", "I'm Sorry") — the name only counts when
    it reappears elsewhere in the book as a confirmed speaker.
    """
    known = set(known_speakers)
    for pat in _SELF_INTRO_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group("name").strip()
        # Validate: must be a name we've seen elsewhere, OR be a two-
        # token "First Last" pair (which is almost never a false
        # positive even without corroboration).
        if raw in known:
            return raw
        tokens = raw.split()
        if len(tokens) == 2 and tokens[0] in known:
            return raw
        if len(tokens) == 2:
            # First-and-last is strong enough to trust on its own.
            return raw
        # Single-token candidate that isn't in known_speakers — too
        # risky to act on ("I'm fine", "I am Sorry" as a name, …).
        continue
    return None


def _apply_self_introductions(all_segments, known_speakers):
    """Re-attribute a segment whose text self-identifies the speaker.

    Fires when the current speaker is None OR is the same as the
    last attributed speaker (carryforward suspect) OR there is no
    prior attribution in the chapter yet. Leaves distinct explicit
    attributions from the backend alone so "I am Sirius, though."
    assigned to Draco isn't rewritten to Sirius.

    A token-overlap guard prevents the common short-to-long rewrite
    ("Ron" → "Ron Weasley") from being treated as a misattribution.
    """
    changed = 0
    for segs in all_segments:
        last_attributed = None
        for seg in segs:
            if not seg.text:
                continue
            name = _extract_self_intro_name(seg.text, known_speakers)
            if name and seg.speaker != name:
                overlap = False
                if seg.speaker:
                    cur_tokens = set(seg.speaker.split())
                    new_tokens = set(name.split())
                    overlap = bool(cur_tokens & new_tokens)
                if not overlap:
                    trust_override = (
                        seg.speaker is None
                        or last_attributed is None
                        or seg.speaker == last_attributed
                    )
                    if trust_override:
                        seg.speaker = name
                        changed += 1
            if seg.speaker:
                last_attributed = seg.speaker
    if changed:
        logger.info(
            "Post-attribution: %d segment%s re-attributed via self-introduction",
            changed, "" if changed == 1 else "s",
        )
    return all_segments


def _collect_global_speaker_counts(all_segments) -> dict[str, int]:
    counts: dict[str, int] = {}
    for segs in all_segments:
        for seg in segs:
            if seg.speaker:
                counts[seg.speaker] = counts.get(seg.speaker, 0) + 1
    return counts


def _filter_junk_speakers(all_segments, speaker_counts, character_tokens=None):
    """Demote obvious BookNLP PROP mis-classifications back to narrator.

    A speaker is demoted only when all of:
    - its total count across the whole book is 1,
    - the name is a single capitalised word,
    - the name is in `_FANFIC_JUNK_NAMES` (case-insensitive),
    - the lowercased token does not appear in ``character_tokens``
      (the metadata-derived cast list).

    The single-occurrence gate keeps legitimate rarely-speaking
    characters whose first name collides with the junk list safe —
    if they speak twice or more, their voice mapping stays. The
    cast-list check spares any tagged character whose name happens
    to clash with a junk word ("Captain" in a Marvel fic).
    """
    cast = {t.lower() for t in (character_tokens or ())}
    demoted = 0
    for segs in all_segments:
        for seg in segs:
            sp = seg.speaker
            if not sp:
                continue
            tokens = sp.split()
            if len(tokens) != 1:
                continue
            low = tokens[0].lower().rstrip(".,;:!?'\u2019")
            if low not in _FANFIC_JUNK_NAMES:
                continue
            if speaker_counts.get(sp, 0) > 1:
                continue
            if low in cast:
                continue
            seg.speaker = None
            demoted += 1
    if demoted:
        logger.info(
            "Post-attribution: %d segment%s demoted from junk speakers",
            demoted, "" if demoted == 1 else "s",
        )
    return all_segments


def _character_tokens(character_list):
    """Flatten a character list into the set of names that count as
    confirmed speakers - the full name, the de-suffixed form (FFN
    "Harry P." -> "Harry"), and each capitalised token of the cleaned
    name.

    A backend may emit "Hermione" or "Hermione Granger" depending on
    its canonicalisation. AO3 tags arrive as full names, FFN as
    "First L." with a trailing surname-initial.
    """
    out: set[str] = set()
    if not character_list:
        return out
    for raw in character_list:
        name = (raw or "").strip()
        if not name:
            continue
        out.add(name)
        cleaned = re.sub(r"\s+[A-Z]\.?$", "", name).strip()
        if cleaned:
            out.add(cleaned)
            for token in cleaned.split():
                if len(token) >= 3 and token[0].isupper():
                    out.add(token)
    return out


def post_refine(all_segments, character_list=None):
    """Run both post-attribution passes in order.

    Applied after the chosen backend has finished refining every
    chapter (or after loading the attribution cache). The passes are
    backend-agnostic and handle patterns both parsers struggle with.

    ``character_list`` is the metadata-derived cast (AO3 tags / FFN
    bare segment). Cast members count as confirmed speakers for
    self-intro validation even on a single occurrence, and are
    immune to junk-speaker demotion regardless of count.
    """
    counts = _collect_global_speaker_counts(all_segments)
    cast_tokens = _character_tokens(character_list)
    # A speaker counts as "known" if they appear more than once - a
    # single random match shouldn't seed self-intro validation. Cast-
    # list names are trusted on the first occurrence too.
    known = {name for name, c in counts.items() if c >= 2}
    known.update(cast_tokens)
    _apply_self_introductions(all_segments, known)
    # Refresh counts before the junk filter - self-intro may have moved
    # occurrences between speakers.
    counts = _collect_global_speaker_counts(all_segments)
    _filter_junk_speakers(all_segments, counts, cast_tokens)
    return all_segments


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
        # spaCy's download subcommand shells out to ``pip install
        # <wheel>`` with no --target. Without one, pip falls back to
        # the embedded Python's own Lib/site-packages — which is NOT on
        # the frozen app's sys.path; only DEPS_DIR is (added via
        # site.addsitedir from neural_env.activate). Trailing args on
        # ``spacy download`` are forwarded to pip, so pinning --target
        # here is what lands the model where the main .exe can import
        # it. Without this the download "succeeds" but the model stays
        # invisible to the running app, and every render falls back to
        # builtin attribution.
        args = args + ["--target", str(neural_env.DEPS_DIR)]
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

    # When there's no caller-supplied log_callback (runtime path from
    # _refine_with_booknlp), fall through to the logger so the download
    # progress and any failure reason land in ffn-dl.log instead of
    # vanishing.
    cb = log_callback or (lambda line: logger.info(line))

    cb(f"spaCy model {model_name!r} not found; downloading...")

    ok = _spacy_download(model_name, log_callback=cb)
    # In frozen builds the model lands in DEPS_DIR, which may not yet
    # be on sys.path if this is the first neural install of the session.
    # Re-activate so the new package is importable from the main process.
    if _is_frozen():
        try:
            from . import neural_env
            neural_env.activate()
        except ImportError:
            pass
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
_booknlp_windows_patched = False
_booknlp_state_dict_patched = False
_booknlp_text_encoding_patched = False


# BookNLP's own downloader (urllib.request.urlretrieve inside
# english_booknlp.py) writes straight to the target path with no
# timeout, no resume, no size check. A partial file — e.g. a
# half-downloaded 446 MB coref model — looks "present" to its
# ``if not Path(...).is_file()`` guard and later crashes torch.load
# with an unexpected-EOF zip error. We size-verify every file and
# fetch missing/short ones ourselves before letting BookNLP init,
# so its guard skips the broken downloader entirely.
#
# Sizes below are the authoritative Content-Length values from
# people.ischool.berkeley.edu (Last-Modified 2021-11; files have not
# been re-issued since). Update if BookNLP ever ships new weights.
_BOOKNLP_URL_BASE = (
    "https://people.ischool.berkeley.edu/~dbamman/booknlp_models/"
)
_BOOKNLP_MODELS: dict[str, list[tuple[str, int]]] = {
    "small": [
        ("entities_google_bert_uncased_L-4_H-256_A-4-v1.0.model", 61_979_735),
        ("coref_google_bert_uncased_L-2_H-256_A-4-v1.0.model", 40_831_851),
        ("speaker_google_bert_uncased_L-8_H-256_A-4-v1.0.1.model", 57_586_985),
    ],
    "big": [
        ("entities_google_bert_uncased_L-6_H-768_A-12-v1.0.model", 311_346_637),
        ("coref_google_bert_uncased_L-12_H-768_A-12-v1.0.model", 446_250_373),
        ("speaker_google_bert_uncased_L-12_H-768_A-12-v1.0.1.model", 438_641_129),
    ],
}


def _booknlp_model_dir():
    from pathlib import Path
    return Path.home() / "booknlp_models"


def _download_booknlp_file(url, dest, expected_size):
    """Resumable download with size verification. Writes to
    ``<dest>.part`` and renames atomically on a size match. The server
    supports ``Range`` (accept-ranges: bytes), so a network blip
    resumes from the last byte on disk instead of restarting. Retries
    three times before giving up."""
    import urllib.error
    import urllib.request
    from pathlib import Path

    part = Path(str(dest) + ".part")
    chunk = 1 << 20  # 1 MiB

    for attempt in range(3):
        start = part.stat().st_size if part.exists() else 0
        if start > expected_size:
            # Over-long partial — must have been from a different file
            # or a corrupted append. Start fresh.
            part.unlink()
            start = 0

        if start == expected_size:
            part.rename(dest)
            logger.info("BookNLP: %s already complete", dest.name)
            return

        logger.info(
            "BookNLP: downloading %s (%d/%d bytes, attempt %d/3)",
            dest.name, start, expected_size, attempt + 1,
        )

        try:
            req = urllib.request.Request(url)
            if start:
                req.add_header("Range", f"bytes={start}-")

            with urllib.request.urlopen(req, timeout=60) as resp:
                # Server ignored Range (status 200 instead of 206) —
                # discard and restart from zero.
                if start and resp.status == 200:
                    start = 0
                    part.unlink(missing_ok=True)

                mode = "ab" if start else "wb"
                bytes_so_far = start
                next_log = start + 50_000_000
                with open(part, mode) as f:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
                        bytes_so_far += len(buf)
                        if bytes_so_far >= next_log:
                            logger.info(
                                "BookNLP: %s %.0f%% (%d/%d)",
                                dest.name,
                                100 * bytes_so_far / expected_size,
                                bytes_so_far, expected_size,
                            )
                            next_log += 50_000_000

            actual = part.stat().st_size
            if actual == expected_size:
                part.rename(dest)
                logger.info("BookNLP: downloaded %s", dest.name)
                return

            logger.warning(
                "BookNLP: %s size %d != expected %d; retrying",
                dest.name, actual, expected_size,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "BookNLP: %s download interrupted (%s); will retry",
                dest.name, exc,
            )

    raise RuntimeError(
        f"Failed to download BookNLP model {dest.name} after 3 attempts."
    )


def _ensure_booknlp_models(model_size: str) -> None:
    """Size-verify BookNLP's per-size model files and (re)download any
    that are missing or truncated. Runs before ``BookNLP(...)`` because
    its built-in downloader treats any existing file as complete — a
    partial file slips past the guard and hangs/crashes torch.load."""
    if model_size not in _BOOKNLP_MODELS:
        return

    model_dir = _booknlp_model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)

    for fname, expected_size in _BOOKNLP_MODELS[model_size]:
        target = model_dir / fname
        if target.exists() and target.stat().st_size == expected_size:
            continue
        if target.exists():
            actual = target.stat().st_size
            logger.warning(
                "BookNLP: %s is %d/%d bytes; deleting and re-downloading",
                fname, actual, expected_size,
            )
            target.unlink()
        _download_booknlp_file(
            _BOOKNLP_URL_BASE + fname, target, expected_size,
        )


def _basename_any_sep(s: str) -> str:
    """Strip any directory prefix using either ``/`` or ``\\`` as a
    separator, regardless of the host OS. ``os.path.basename`` on POSIX
    Python doesn't recognise ``\\`` as a separator, which makes the
    Windows-path shim a silent no-op on Linux/macOS test runs."""
    return s.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _patch_booknlp_windows_paths() -> None:
    """Work around an upstream BookNLP bug that breaks on Windows.

    Three tagger classes (entity_tagger.LitBankEntityTagger,
    litbank_coref.LitBankCoref, bert_qa.QuotationAttribution) derive
    the HuggingFace base-model name from the on-disk model file via
    ``model_file.split("/")[-1]``. On POSIX that strips the directory
    and leaves just the model filename. On Windows paths use ``\\``,
    so the split returns the whole path unchanged and transformers'
    ``from_pretrained`` sees e.g.
    ``C:\\ffdl\\booknlp_models\\entities_google/bert_uncased_...``,
    which HuggingFace Hub's repo-id validator rejects because it
    contains ``:`` and ``\\``.

    We patch each affected module's ``re`` binding with a shim that
    intercepts only the ``re.sub("google_bert", ...)`` call used for
    this derivation and strips the directory before the real
    substitution runs. Every other regex flows through unchanged.

    Runs on every platform: on POSIX the basename strip is a no-op for
    POSIX paths, but it ensures any Windows-style path passed in
    (e.g. via a config) is handled too.
    """
    global _booknlp_windows_patched
    if _booknlp_windows_patched:
        return

    import re as _re
    from booknlp.english import entity_tagger, litbank_coref, bert_qa

    class _ReShim:
        def __init__(self, real):
            self._real = real

        def sub(self, pattern, repl, string, *a, **k):
            if pattern == "google_bert" and isinstance(string, str):
                string = _basename_any_sep(string)
            return self._real.sub(pattern, repl, string, *a, **k)

        def __getattr__(self, name):
            return getattr(self._real, name)

    shim = _ReShim(_re)
    entity_tagger.re = shim
    litbank_coref.re = shim
    bert_qa.re = shim
    _booknlp_windows_patched = True


def _patch_booknlp_state_dict() -> None:
    """Strip deprecated ``*.embeddings.position_ids`` keys from BookNLP's
    saved state_dicts before they reach ``load_state_dict``.

    The shipped BookNLP weights were saved against an older
    ``transformers`` where ``BertEmbeddings`` registered
    ``position_ids`` as a buffer. Transformers 4.31+ removed that
    buffer, so ``model.load_state_dict(torch.load(...))`` raises
    ``Unexpected key(s) in state_dict: "bert.embeddings.position_ids"``
    and the backend silently falls back to builtin.

    We wrap ``torch.load`` inside the three BookNLP modules that call
    ``load_state_dict`` (``entity_tagger``, ``litbank_coref``,
    ``bert_qa``) so the offending keys are dropped before the model
    sees them. Other ``torch.load`` calls flow through unchanged.
    """
    global _booknlp_state_dict_patched
    if _booknlp_state_dict_patched:
        return

    from booknlp.english import entity_tagger, litbank_coref, bert_qa

    def _strip(state):
        if isinstance(state, dict):
            for key in [k for k in state
                        if isinstance(k, str)
                        and k.endswith(".embeddings.position_ids")]:
                state.pop(key, None)
        return state

    class _TorchShim:
        def __init__(self, real):
            self._real = real

        def load(self, *a, **k):
            return _strip(self._real.load(*a, **k))

        def __getattr__(self, name):
            return getattr(self._real, name)

    for mod in (entity_tagger, litbank_coref, bert_qa):
        mod.torch = _TorchShim(mod.torch)

    _booknlp_state_dict_patched = True


def _patch_booknlp_text_encoding() -> None:
    """Force every text-mode ``open()`` inside BookNLP to use UTF-8.

    Several BookNLP modules read input or auxiliary files with bare
    ``open(filename)`` — no ``encoding=`` argument. Python on Windows
    falls back to ``locale.getpreferredencoding()`` (cp1252), which
    chokes on UTF-8 fanfic text that contains smart quotes (U+201D
    encodes to ``E2 80 9D``; ``0x9D`` is undefined in cp1252):

        'charmap' codec can't decode byte 0x9d in position 1701: ...

    The most visible call site is ``english_booknlp.process()`` which
    opens the input book.txt; we also patch every other module that
    does an unencoded text read so reruns and re-imports don't
    regress. Binary mode opens are passed through unchanged.
    """
    global _booknlp_text_encoding_patched
    if _booknlp_text_encoding_patched:
        return

    import builtins

    from booknlp.english import (
        english_booknlp,
        entity_tagger,
        gender_inference_model_1,
        name_coref,
        bert_coref_quote_pronouns,
    )
    from booknlp.common import b3, sequence_layered_reader

    real_open = builtins.open

    def _utf8_open(file, mode="r", *a, **k):
        if "b" not in mode and "encoding" not in k:
            k["encoding"] = "utf-8"
        return real_open(file, mode, *a, **k)

    for mod in (
        english_booknlp,
        entity_tagger,
        gender_inference_model_1,
        name_coref,
        bert_coref_quote_pronouns,
        b3,
        sequence_layered_reader,
    ):
        mod.open = _utf8_open

    _booknlp_text_encoding_patched = True


def _get_booknlp_model(model_size: str):
    if model_size in _booknlp_cache:
        return _booknlp_cache[model_size]
    _ensure_booknlp_models(model_size)
    _patch_booknlp_windows_paths()
    _patch_booknlp_state_dict()
    _patch_booknlp_text_encoding()
    from booknlp.booknlp import BookNLP
    logger.info("BookNLP: constructing model (size=%s)", model_size)
    model = BookNLP(
        "en",
        {
            "pipeline": "entity,quote,coref",
            "model": model_size,
        },
    )
    logger.info("BookNLP: model construction complete")
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
    import shutil
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
    try:
        infile = tmp / "book.txt"
        infile.write_text(full_text, encoding="utf-8")
        logger.info("BookNLP: processing %d chars (output dir %s)", len(full_text), tmp)
        model.process(str(infile), str(tmp), "book")
        logger.info("BookNLP: process() returned")

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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── LLM adapter ────────────────────────────────────────────────────


# Maximum chapter chars we send to a single LLM request before chunking.
# Tuned for 8B local models (Llama 3.1 8B has 128K context but quality
# degrades fast past 8-16K input on consumer GPUs / CPU). Cloud models
# tolerate much more, but staying conservative keeps latency and token
# cost predictable. Overlap on chunk boundaries lets a quote that lands
# at the seam keep some surrounding context.
_LLM_CHUNK_CHARS = 6000
_LLM_CHUNK_OVERLAP_CHARS = 500
_LLM_REQUEST_TIMEOUT_S = 180
# Cap how many quoted segments we ask the model to label in one request.
# Even with plenty of context, asking for 200 labels at once reliably
# truncates the response; 40 is a comfortable ceiling that matches a
# typical chapter's dialogue density.
_LLM_QUOTES_PER_REQUEST = 40


def _looks_quoted(text: str) -> bool:
    """True for segments that read as direct dialogue. Used to decide
    which segments to send to the LLM — narration we leave alone."""
    if not text:
        return False
    quote_chars = ('"', '“', '”', '‘', '’')
    return any(ch in text for ch in quote_chars)


def _llm_provider_supported(provider: str) -> bool:
    return provider in {"ollama", "openai", "anthropic", "openai-compatible"}


def _llm_default_endpoint(provider: str) -> str:
    if provider == "ollama":
        return "http://localhost:11434"
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "anthropic":
        return "https://api.anthropic.com/v1"
    return ""


def _llm_normalize_endpoint(provider: str, endpoint: str | None) -> str:
    base = (endpoint or "").strip().rstrip("/")
    if not base:
        base = _llm_default_endpoint(provider)
    return base


def _llm_call(
    *, provider: str, model: str, api_key: str | None,
    endpoint: str, system_prompt: str, user_prompt: str,
) -> str:
    """One round-trip to the configured LLM. Returns the raw text reply
    (the caller is responsible for JSON-parsing it). Raises on transport
    errors, non-2xx responses, or unsupported providers."""
    import json as _json
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}

    if provider == "ollama":
        url = f"{endpoint}/api/chat"
        payload = {
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    elif provider == "anthropic":
        url = f"{endpoint}/messages"
        if not api_key:
            raise RuntimeError("Anthropic backend requires an API key")
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    else:  # openai or openai-compatible
        url = f"{endpoint}/chat/completions"
        if provider == "openai" and not api_key:
            raise RuntimeError("OpenAI backend requires an API key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }

    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_LLM_REQUEST_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Surface server-provided error text — most providers ship the
        # underlying reason in the body and the bare status line is useless.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"LLM HTTP {exc.code} from {provider}: {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Connection refused, DNS failure, timeout — the endpoint isn't
        # reachable at all. Distinct from HTTPError above (which means
        # the server replied) because per-chapter loops want to give
        # up after one of these instead of retrying 100+ times.
        raise LLMUnavailable(
            f"LLM endpoint {url} unreachable: {exc}"
        ) from exc

    parsed = _json.loads(body)
    if provider == "ollama":
        return (parsed.get("message") or {}).get("content", "")
    if provider == "anthropic":
        for block in parsed.get("content") or []:
            if block.get("type") == "text":
                return block.get("text", "")
        return ""
    # openai / openai-compatible
    choices = parsed.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message") or {}).get("content", "")


_LLM_PROBE_TIMEOUT_S = 5.0
"""Connect/read timeout for the "is this endpoint up?" probe used by
the LLM settings dialog. Short enough that a wrong/dead URL doesn't
make the user wait the way a real classifier call would."""


class LLMProbeResult:
    """Outcome of :func:`probe_llm_endpoint`.

    ``ok`` is True when the endpoint replied 2xx to its inventory
    surface (Ollama ``/api/tags``, OpenAI/compatible ``/models``,
    Anthropic ``/models``). Cloud providers also need the API key for
    that call to succeed, so ``ok=False`` with ``status==401`` is the
    "key is wrong" signal — distinct from ``status is None`` (endpoint
    unreachable / DNS / timeout).
    """

    __slots__ = ("ok", "detail", "status", "models")

    def __init__(
        self,
        *,
        ok: bool,
        detail: str,
        status: int | None = None,
        models: list[str] | None = None,
    ):
        self.ok = ok
        self.detail = detail
        self.status = status
        self.models = models


def probe_llm_endpoint(
    *,
    provider: str,
    endpoint: str | None,
    api_key: str | None = None,
    timeout: float = _LLM_PROBE_TIMEOUT_S,
) -> LLMProbeResult:
    """Ping the configured LLM endpoint's inventory surface and report
    whether it's actually reachable, authenticated, and serving models.

    Used by the GUI's "Test connection" button so a user with their
    Ollama daemon offline (or an API key typo) finds out immediately
    instead of after kicking off a 100-chapter download. Pure helper —
    no GUI deps, no logging side effects, safe to call from a worker
    thread.
    """
    import json as _json
    import urllib.error
    import urllib.request

    base = _llm_normalize_endpoint(provider, endpoint)
    logger.info("LLM probe: provider=%s endpoint=%s", provider, base or "(blank)")
    if not base:
        return LLMProbeResult(
            ok=False,
            detail=(
                "Endpoint is empty. Set Endpoint to your provider's "
                "base URL (e.g. https://api.groq.com/openai/v1)."
            ),
        )

    headers: dict[str, str] = {}
    if provider == "ollama":
        url = f"{base}/api/tags"
        models_key = "models"
    elif provider == "anthropic":
        url = f"{base}/models"
        if not api_key:
            return LLMProbeResult(
                ok=False,
                detail="Anthropic requires an API key for the probe.",
            )
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        models_key = "data"
    else:  # openai / openai-compatible
        url = f"{base}/models"
        if provider == "openai" and not api_key:
            return LLMProbeResult(
                ok=False,
                detail="OpenAI requires an API key for the probe.",
            )
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        models_key = "data"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        logger.warning(
            "LLM probe: HTTP %s from %s (%s)", exc.code, provider, exc.reason,
        )
        if exc.code in (401, 403):
            return LLMProbeResult(
                ok=False,
                status=exc.code,
                detail=(
                    f"Authentication rejected by {provider} "
                    f"(HTTP {exc.code}) — check the API key."
                ),
            )
        return LLMProbeResult(
            ok=False,
            status=exc.code,
            detail=f"Server replied HTTP {exc.code}: {exc.reason}",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning(
            "LLM probe: %s endpoint unreachable: %s", provider, exc,
        )
        if provider == "ollama":
            hint = (
                " — is the Ollama daemon running? Use the Install "
                "Ollama button below if it isn't installed."
            )
        else:
            hint = ""
        return LLMProbeResult(
            ok=False,
            detail=f"Endpoint unreachable: {exc}{hint}",
        )

    try:
        parsed = _json.loads(body)
    except ValueError:
        return LLMProbeResult(
            ok=True,
            status=status,
            detail=(
                f"Endpoint responded HTTP {status} but the body wasn't "
                f"JSON ({len(body)} bytes). Probably reachable."
            ),
        )

    raw_models = parsed.get(models_key) if isinstance(parsed, dict) else None
    model_names: list[str] = []
    if isinstance(raw_models, list):
        for m in raw_models:
            if isinstance(m, dict):
                # Ollama: {"name": "llama3.1:8b", ...}
                # OpenAI/Anthropic: {"id": "gpt-4o-mini", ...}
                name = m.get("name") or m.get("id")
                if isinstance(name, str):
                    model_names.append(name)
            elif isinstance(m, str):
                model_names.append(m)

    if model_names:
        detail = (
            f"Connected to {provider}. {len(model_names)} model(s) "
            f"available: {', '.join(model_names[:5])}"
            + ("…" if len(model_names) > 5 else "")
        )
    else:
        detail = (
            f"Connected to {provider}, but no models are installed. "
            + (
                "Run `ollama pull llama3.1:8b` (or any other model) "
                "from a terminal to download one."
                if provider == "ollama"
                else "Check the provider dashboard for available models."
            )
        )
    logger.info(
        "LLM probe: ok provider=%s status=%s models=%d",
        provider, status, len(model_names),
    )
    return LLMProbeResult(
        ok=True,
        status=status,
        detail=detail,
        models=model_names,
    )


def compute_model_choices(
    *,
    curated: list[str],
    extra: list[str],
    current: str,
) -> list[str]:
    """Merge curated model suggestions, probe-discovered models, and
    the user's currently-typed value into a single de-duplicated,
    case-insensitively sorted list for the LLM settings dialog's
    Model combo box.

    Pure on lists/strings so the dropdown's content shaping can be
    tested without spinning up wx. The ``current`` value is preserved
    even when blank (no-op) so a user mid-type doesn't lose their
    entry just because a background probe returned. Case-insensitive
    sorting keeps ``Llama3.1`` and ``llama3.1`` adjacent in the
    dropdown rather than at opposite ends.
    """
    seen: dict[str, None] = {}
    for name in curated:
        if name:
            seen[name] = None
    for name in extra:
        if name:
            seen[name] = None
    current = (current or "").strip()
    if current:
        seen[current] = None
    return sorted(seen.keys(), key=str.lower)


def _llm_parse_speaker_map(reply: str) -> dict[str, dict]:
    """Pull a ``{"1": {"speaker": "Name", "emotion": "..."}}`` mapping
    out of the LLM reply.

    Two response shapes are accepted so the parser stays compatible
    with older prompts that only asked for the speaker:

    - ``{"1": "Harry"}`` -> ``{"1": {"speaker": "Harry"}}``
    - ``{"1": {"speaker": "Harry", "emotion": "shouting"}}`` (verbatim)

    LLMs sometimes wrap the JSON in prose or markdown fences; we strip
    a leading ```json fence and isolate the first balanced ``{...}``
    block before parsing. Returns an empty dict on any failure so the
    caller can fall through to the next chunk without raising.
    """
    import json as _json

    if not reply:
        return {}
    text = reply.strip()
    # Strip a markdown fence if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    # Isolate the first JSON object — sometimes models prefix "Sure! ".
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace <= first_brace:
        return {}
    blob = text[first_brace : last_brace + 1]
    try:
        parsed = _json.loads(blob)
    except (ValueError, _json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in parsed.items():
        key = str(k).strip()
        if isinstance(v, str) and v.strip():
            out[key] = {"speaker": v.strip()}
        elif isinstance(v, dict):
            speaker = v.get("speaker")
            if isinstance(speaker, str) and speaker.strip():
                entry = {"speaker": speaker.strip()}
                emotion = v.get("emotion")
                if isinstance(emotion, str) and emotion.strip():
                    entry["emotion"] = emotion.strip()
                out[key] = entry
    return out


# Map LLM-emitted free-form emotion labels to the keys our prosody
# table actually understands. Anything not in the table is dropped
# so a hallucinated tag (``"contemplative"``) doesn't slip into the
# Segment and confuse downstream prosody lookup.
_LLM_EMOTION_ALIASES = {
    "whisper": "whisper",
    "whispered": "whisper",
    "whispering": "whisper",
    "muttered": "whisper",
    "muttering": "whisper",
    "shout": "shout",
    "shouted": "shout",
    "shouting": "shout",
    "yell": "shout",
    "yelled": "shout",
    "yelling": "shout",
    "scream": "shout",
    "screamed": "shout",
    "screaming": "shout",
    "excited": "excited",
    "excitement": "excited",
    "exclaim": "excited",
    "exclaimed": "excited",
    "exclaiming": "excited",
    "cheerful": "cheerful",
    "happy": "cheerful",
    "joyful": "cheerful",
    "laughing": "cheerful",
    "amused": "cheerful",
    "sad": "sad",
    "sorrowful": "sad",
    "crying": "sad",
    "sobbed": "sad",
    "sobbing": "sad",
    "tearful": "sad",
    "angry": "angry",
    "anger": "angry",
    "furious": "angry",
    "snarled": "angry",
    "growled": "angry",
    "neutral": "",
    "normal": "",
    "calm": "",
    "default": "",
}


def _llm_normalise_emotion(raw: str | None) -> str | None:
    """Map a free-form LLM emotion label to a prosody-table key, or
    None for a label we don't recognise / care about. Empty / neutral
    labels collapse to None so we don't override an already-detected
    emotion with a no-op."""
    if not raw or not isinstance(raw, str):
        return None
    low = raw.strip().lower()
    if not low:
        return None
    mapped = _LLM_EMOTION_ALIASES.get(low)
    if mapped is None:
        return None
    return mapped or None


def _llm_canonicalise_name(
    name: str, character_list: list[str], cast_tokens: set[str],
) -> str | None:
    """Map an LLM-emitted speaker label back to a canonical character.

    Models sometimes return surface forms ("Hermy", "the boy", "Harry's
    mother") that don't match any cast entry exactly. We accept:
    - "Narrator" / "Unknown" / empty -> None (narration)
    - exact match on the full character_list (case-insensitive)
    - exact match on a cast token
    - a single-word reply whose lowercased form is a known cast token

    Anything else is preserved verbatim — better to surface a real
    out-of-cast name (an OC the metadata didn't tag) than to drop it.
    """
    if not name:
        return None
    low = name.strip().lower()
    if low in {"narrator", "narration", "unknown", "none", "n/a"}:
        return None
    cl_lower = {c.lower(): c for c in character_list}
    if low in cl_lower:
        return cl_lower[low]
    cast_lower = {t.lower(): t for t in cast_tokens}
    if low in cast_lower:
        return cast_lower[low]
    return name.strip()


def _refine_with_llm(
    segments, full_text: str, *,
    provider: str,
    model: str,
    api_key: str | None = None,
    endpoint: str | None = None,
    character_list: Iterable[str] | None = None,
):
    """Send chapter context + numbered quotes to an LLM and overwrite
    segment speakers with its labels.

    Strategy:
    1. Pick out segments that look quoted (skip narration).
    2. Slide a window over ``full_text``; for each window, batch the
       quotes whose midpoint falls inside it and ask the LLM to label
       them. Each quote is labelled exactly once (the first window
       that contains it wins).
    3. Map the LLM's reply through ``_llm_canonicalise_name`` and write
       the result onto each segment.

    On any provider error we raise — the dispatcher catches and falls
    back to builtin for the rest of the render."""
    if not _llm_provider_supported(provider):
        raise RuntimeError(f"Unsupported LLM provider: {provider!r}")
    if not model:
        raise RuntimeError("LLM backend requires a model name")
    endpoint_url = _llm_normalize_endpoint(provider, endpoint)
    if not endpoint_url:
        raise RuntimeError(f"No endpoint configured for provider {provider!r}")

    char_list = [c for c in (character_list or []) if c]
    cast_tokens = _character_tokens(char_list)

    # Find each quoted segment's start position in full_text (best-effort
    # substring search, same approach as fastcoref / booknlp adapters).
    quoted_idx: list[tuple[int, int]] = []  # (segment_index, char_offset)
    cursor = 0
    for i, seg in enumerate(segments):
        if not seg.text or not _looks_quoted(seg.text):
            continue
        pos = full_text.find(seg.text, cursor)
        if pos < 0:
            stripped = seg.text.strip('"“”')
            pos = full_text.find(stripped, cursor)
        if pos < 0:
            continue
        cursor = pos + len(seg.text)
        quoted_idx.append((i, pos))

    if not quoted_idx:
        return segments

    char_list_str = ", ".join(char_list) if char_list else "(none provided)"
    system_prompt = (
        "You are an expert at identifying who said each line of dialogue "
        "in fanfiction and what emotional register they used. You will "
        "be given a passage and a numbered list of quoted lines from "
        "that passage. For each line, identify:\n"
        "- 'speaker': use exactly one of the listed character names "
        "  when the speaker is one of them, 'Narrator' for unspoken "
        "  thoughts / narration, or 'Unknown' only when you genuinely "
        "  cannot tell.\n"
        "- 'emotion': pick from {whisper, shout, excited, cheerful, "
        "  sad, angry, neutral} — use the one that fits the line's "
        "  delivery, defaulting to 'neutral' for plain dialogue.\n"
        "Respond with ONLY a single JSON object whose keys are the "
        "quote numbers (as strings) and values are objects with "
        "'speaker' and 'emotion'. Example: "
        '{"1": {"speaker": "Harry Potter", "emotion": "shout"}, '
        '"2": {"speaker": "Narrator", "emotion": "neutral"}}.'
    )

    chunk_size = _LLM_CHUNK_CHARS
    overlap = _LLM_CHUNK_OVERLAP_CHARS
    total = len(full_text)
    pos = 0
    handled: set[int] = set()  # segment indices already labelled

    while pos < total and len(handled) < len(quoted_idx):
        end = min(total, pos + chunk_size)
        # Quotes whose midpoint falls in this window and aren't done yet.
        batch: list[tuple[int, int]] = []
        for seg_i, qpos in quoted_idx:
            if seg_i in handled:
                continue
            mid = qpos + len(segments[seg_i].text) // 2
            if pos <= mid < end:
                batch.append((seg_i, qpos))
            if len(batch) >= _LLM_QUOTES_PER_REQUEST:
                break

        if batch:
            window_text = full_text[pos:end]
            numbered = []
            for n, (seg_i, _qpos) in enumerate(batch, 1):
                numbered.append(f"{n}. {segments[seg_i].text}")
            user_prompt = (
                f"Character list: {char_list_str}\n\n"
                f"Passage:\n{window_text}\n\n"
                f"Quotes to attribute:\n" + "\n".join(numbered) + "\n\n"
                "Return JSON only."
            )
            reply = _llm_call(
                provider=provider, model=model, api_key=api_key,
                endpoint=endpoint_url,
                system_prompt=system_prompt, user_prompt=user_prompt,
            )
            mapping = _llm_parse_speaker_map(reply)
            for n, (seg_i, _qpos) in enumerate(batch, 1):
                entry = mapping.get(str(n)) or mapping.get(n)
                if entry is None:
                    continue
                raw_name = entry.get("speaker") if isinstance(entry, dict) else entry
                if not raw_name:
                    continue
                canonical = _llm_canonicalise_name(
                    raw_name, char_list, cast_tokens,
                )
                segments[seg_i].speaker = canonical
                # Emotion is optional — only override the segment's
                # existing emotion (which the regex parser may have
                # already set) when the LLM emitted a tag we know.
                if isinstance(entry, dict):
                    emotion = _llm_normalise_emotion(entry.get("emotion"))
                    if emotion:
                        segments[seg_i].emotion = emotion
                handled.add(seg_i)

        # Advance the window. Stop sliding once we've covered the
        # text — the overlap subtraction guarantees forward progress
        # even when chunk_size <= overlap.
        if end >= total:
            break
        next_pos = end - overlap
        if next_pos <= pos:
            next_pos = end
        pos = next_pos

    if handled:
        logger.info(
            "LLM attribution: labelled %d/%d quoted segment%s (%s/%s)",
            len(handled), len(quoted_idx),
            "" if len(quoted_idx) == 1 else "s",
            provider, model,
        )

    return segments


def llm_cache_token(provider: str, model: str) -> str:
    """Filesystem-safe cache discriminator for the LLM backend.

    The attribution cache keys on (backend, model_size, chapter_hash);
    for ``backend == "llm"`` we encode (provider, model) into the
    model_size slot so an Ollama-llama3 result doesn't overwrite a
    GPT-4o result for the same chapter.
    """
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", model or "")
    return f"{provider}-{safe_model}".strip("-_") or "default"


# ── Author's-note classifier (LLM) ────────────────────────────────


_AN_SYSTEM_PROMPT = (
    "You are reading fanfiction and identifying author's notes vs "
    "story content. Author's notes are out-of-story commentary "
    "addressing the reader directly: greetings ('Howdy', 'Hi "
    "everyone'), self-introductions ('My name is...'), thanks to "
    "betas/readers, requests for reviews/favourites/kudos, Patreon "
    "plugs, ownership disclaimers ('I don't own X', 'I own nothing'), "
    "update schedules, links to discord/tumblr, replies to comments, "
    "or chatty asides about the author's life. If a paragraph breaks "
    "the fourth wall to address the reader as the AUTHOR (not as a "
    "first-person narrator character), it's an author's note — even "
    "if it's chatty or sounds like prose. In-world dialogue and "
    "first-person narration by a story character are NOT author's "
    "notes.\n\n"
    "Output schema (REQUIRED, do not deviate):\n"
    "Return a single flat JSON object. Each key is a paragraph "
    "number as a string ('1', '2', ...). Each value is a boolean: "
    "true = author's note, false = story content. Include every "
    "paragraph number you were given. Do NOT wrap the answer in any "
    "outer object, do NOT add fields for chapter title or word count, "
    "and do NOT echo paragraph text. Example for four paragraphs:\n"
    '{"1": true, "2": false, "3": false, "4": true}'
)


def classify_authors_notes_via_llm(
    paragraphs: list[str], *, llm_config: dict,
    system_prompt_override: str | None = None,
) -> set[int]:
    """Ask the LLM to flag which paragraphs are author's notes.

    Returns a set of zero-based indices. ``llm_config`` matches the
    dict ``generate_audiobook`` accepts. Failure modes (transport
    error, parse failure, missing config) return an empty set so the
    regex pre-pass is the only A/N filter applied — i.e. the LLM
    backstop is purely additive.

    ``system_prompt_override`` lets callers swap the default
    audiobook-flavoured prompt for a stricter one — used by the
    HTML-pipeline verification round, which re-asks the model with
    "high confidence only" framing when the first pass flagged so
    much of a chapter that we suspect a hallucination. ``None``
    falls back to ``_AN_SYSTEM_PROMPT``.
    """
    if not paragraphs or not llm_config:
        return set()

    provider = llm_config.get("provider", "")
    model = llm_config.get("model", "")
    if not provider or not model:
        return set()
    endpoint = _llm_normalize_endpoint(provider, llm_config.get("endpoint"))

    numbered = []
    # Truncate each paragraph to a reasonable length so a 5K-word
    # narration block doesn't dominate the prompt and crowd out the
    # actual decisions. The first 600 chars almost always contain
    # the signal that distinguishes A/N from prose.
    for i, p in enumerate(paragraphs, 1):
        sample = p.strip()
        if len(sample) > 600:
            sample = sample[:600] + "…"
        numbered.append(f"{i}. {sample}")
    user_prompt = (
        "Paragraphs to classify (true = author's note, false = story "
        "content):\n\n" + "\n\n".join(numbered) + "\n\nReturn JSON only."
    )

    # DEBUG-level transcript of the classifier input. Lets a user
    # who's seeing "no A/N paragraphs found" on a fic with obvious
    # author's notes diagnose without having to share the fic
    # itself: grep the log for `LLM A/N input` and you can see the
    # first ~80 chars of every paragraph the model was shown, in
    # order. The truncation keeps the log manageable on long fics.
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "LLM A/N input: %d paragraph(s) for %s/%s",
            len(paragraphs), provider, model,
        )
        for i, p in enumerate(paragraphs, 1):
            preview = p.strip().replace("\n", " ")[:120]
            logger.debug("LLM A/N input  [%d] (%d chars): %s",
                         i, len(p), preview)

    try:
        reply = _llm_call(
            provider=provider, model=model,
            api_key=llm_config.get("api_key"),
            endpoint=endpoint,
            system_prompt=system_prompt_override or _AN_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
    except LLMUnavailable:
        # Endpoint is down — propagate so the chapter-loop caller can
        # disable LLM for the rest of the run instead of retrying every
        # chapter and spamming the same warning.
        raise
    except Exception as exc:  # noqa: BLE001 — additive, never fail
        logger.warning("LLM author's-note classifier failed: %s", exc)
        return set()

    # DEBUG-level transcript of the raw model reply. If the user is
    # debugging "model returned empty" vs "model returned all-false"
    # vs "model returned weird JSON", this is the differentiator.
    # Capped at 1500 chars because some providers like to wrap their
    # JSON in a chatty preamble.
    if logger.isEnabledFor(logging.DEBUG):
        snippet = (reply or "").replace("\n", " ")[:1500]
        logger.debug("LLM A/N raw reply: %s", snippet)

    import json as _json

    if not reply:
        return set()
    text = reply.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace == -1 or last_brace <= first_brace:
        return set()
    blob = text[first_brace : last_brace + 1]
    try:
        parsed = _json.loads(blob)
    except (ValueError, _json.JSONDecodeError):
        return set()

    flagged = _parse_an_response(parsed, paragraphs)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "LLM A/N parsed: %d flag(s) from %d paragraph(s)",
            len(flagged), len(paragraphs),
        )
    return flagged


# Keys models tend to use for the "this paragraph is N" field when
# they wrap A/N entries in objects instead of returning the documented
# ``{"1": true, ...}`` map. Lowercased once at module load so the
# scan in :func:`_parse_an_response` is a cheap membership test.
_AN_INDEX_KEYS = frozenset({
    "number", "index", "idx", "paragraph", "paragraph_number",
    "para", "para_number", "i", "n",
})


# Top-level keys models like to nest the A/N list under instead of
# returning a flat map. Walked recursively, so any depth works as long
# as one of these keys gates the actual list.
_AN_LIST_KEYS = frozenset({
    "author_notes", "authors_notes", "author_note", "notes", "note",
    "flagged", "flags", "an", "ans", "a_n", "a_ns", "results",
    "items", "entries", "data",
})


def _parse_an_response(parsed, paragraphs: list[str]) -> set[int]:
    """Extract flagged paragraph indices from the LLM's parsed JSON.

    The documented response shape is ``{"1": true, "2": false, ...}``,
    but smaller / more "instruction-tuned" models routinely answer with
    creative schemas instead — ``{"author_notes": [{"text": "..."}]}``,
    ``{"notes": [{"number": 5}, ...]}``, ``{"response": {...}}``, and
    so on. Without flexibility here a correctly-classified chapter
    silently parses as "no A/N found", which is the bug Matt hit on
    a 42-chapter fic with obvious A/Ns.

    Five strategies, each tried in order; the first to yield any
    matches wins. None of them is destructive — pure read of the
    parsed structure. Order matters: the documented map is the
    cheapest and least ambiguous, so try it first. Text matching is
    last because it's the broadest.
    """
    # 1. Documented format: ``{"1": true, "2": false}``.
    flagged: set[int] = set()
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            try:
                idx = int(str(k).strip()) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(paragraphs) and bool(v):
                flagged.add(idx)
    if flagged:
        return flagged

    # 2. Walk for nested ``{"number": N}`` / ``{"index": N}`` etc.
    for idx in _walk_index_fields(parsed, len(paragraphs)):
        flagged.add(idx)
    if flagged:
        return flagged

    # 3. List of bare integers anywhere in the tree (``{"flagged":
    #    [1, 5, 7]}``). Treat them as 1-based indices because the
    #    prompt presents paragraphs that way.
    for idx in _walk_int_lists(parsed, len(paragraphs)):
        flagged.add(idx)
    if flagged:
        return flagged

    # 4. List of bare strings anywhere in the tree, where each string
    #    parses to an int (``["1", "5", "7"]``).
    for s in _walk_strings(parsed):
        try:
            idx = int(s.strip()) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(paragraphs):
            flagged.add(idx)
    if flagged:
        return flagged

    # 5. Last-resort: text matching. Some models (qwen2.5:7b on this
    #    prompt, gpt-4o-mini under certain conditions) return the
    #    full paragraph TEXT they consider an A/N rather than its
    #    number. Match each returned string against the prefix of
    #    every input paragraph and flag the matches. Tolerant of
    #    whitespace and the truncation ellipsis we add to long
    #    paragraphs in the prompt.
    para_prefixes = [
        (i, _normalise_para(p)) for i, p in enumerate(paragraphs)
    ]
    for s in _walk_strings(parsed):
        candidate = _normalise_para(s)
        if len(candidate) < 30:
            # Too short to be a confident match — risks false
            # positives from category labels like "introduction".
            continue
        for idx, prefix in para_prefixes:
            if not prefix:
                continue
            # Match by either direction's prefix because either
            # value can be the longer one (the LLM may quote a
            # truncated version of a long paragraph).
            shorter = candidate if len(candidate) < len(prefix) else prefix
            longer = prefix if len(candidate) < len(prefix) else candidate
            if longer.startswith(shorter[:60]):
                flagged.add(idx)
                break
    return flagged


def _walk_index_fields(obj, n_paragraphs: int):
    """Yield zero-based indices found in any
    ``{"number"/"index"/"paragraph": N}`` field anywhere in ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _AN_INDEX_KEYS:
                try:
                    idx = int(v) - 1
                except (TypeError, ValueError):
                    pass
                else:
                    if 0 <= idx < n_paragraphs:
                        yield idx
            yield from _walk_index_fields(v, n_paragraphs)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_index_fields(v, n_paragraphs)


def _walk_int_lists(obj, n_paragraphs: int):
    """Yield zero-based indices found in any list of bare integers
    anywhere in ``obj``. Integers are interpreted as 1-based indices
    because that's how the prompt numbers paragraphs."""
    if isinstance(obj, list):
        if all(isinstance(v, int) for v in obj):
            for v in obj:
                idx = v - 1
                if 0 <= idx < n_paragraphs:
                    yield idx
            return
        for v in obj:
            yield from _walk_int_lists(v, n_paragraphs)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_int_lists(v, n_paragraphs)


def expand_an_block(flagged: set[int], n_paragraphs: int) -> set[int]:
    """Expand the LLM-flagged set across the natural A/N boundaries
    (chapter head and tail).

    LLM classifiers reliably catch *some* paragraphs in a contiguous
    A/N block but miss others — the model picks individual lines, it
    doesn't reason about "this is the next sentence of the same
    author commentary I just labelled". Authors in practice put A/Ns
    in tight contiguous blocks at chapter start and chapter end, so
    once the LLM has confirmed any A/N is present in those regions,
    the surrounding paragraphs in the same region are nearly always
    also A/N.

    Safety gates (per Matt's destructive-heuristic policy):

    * Two corroborating signals required — an LLM flag *and* a
      boundary position. Mid-chapter flags don't expand.
    * Hard cap: never produce a flagged set covering more than 50%
      of the chapter. If expansion would breach that, return the
      original set unchanged.
    * No effect on chapters with fewer than 8 paragraphs (too short
      for boundary heuristics to be meaningful).

    Returns a new set; the input set is not mutated.
    """
    if not flagged or n_paragraphs < 8:
        return set(flagged)

    expanded = set(flagged)

    # Trailing block — anchor the expansion if any flag landed in
    # the bottom 20%, then sweep from the earliest flag in the
    # bottom 30% to the chapter end. Two thresholds (20% / 30%)
    # because authors sometimes start the outro with a "your reviews
    # are great" line that the LLM catches alongside the main
    # rambling block — the earliest flag inside the wider window
    # is the better anchor.
    bottom_anchor = int(n_paragraphs * 0.8)
    bottom_window = int(n_paragraphs * 0.7)
    if any(i >= bottom_anchor for i in flagged):
        earliest = min(i for i in flagged if i >= bottom_window)
        for i in range(earliest, n_paragraphs):
            expanded.add(i)

    # Leading block — same logic, mirrored. Authors front-load
    # disclaimers and "I own nothing" lines in the top few
    # paragraphs; if the LLM flagged any, sweep to whichever flag
    # sits highest in the head window.
    top_anchor = max(1, int(n_paragraphs * 0.05))
    top_window = max(2, int(n_paragraphs * 0.15))
    if any(i < top_anchor for i in flagged):
        latest = max(i for i in flagged if i < top_window)
        for i in range(0, latest + 1):
            expanded.add(i)

    # Hard cap — refuse to drop more than half the chapter, no
    # matter how persuasive the heuristic seems. A user who chose
    # "just strip the obvious A/Ns" is much better off with a few
    # surviving notes than with a half-empty chapter.
    if len(expanded) > n_paragraphs // 2:
        return set(flagged)

    return expanded


def _walk_strings(obj):
    """Yield every string value anywhere inside ``obj``."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _normalise_para(s: str) -> str:
    """Collapse whitespace and strip the prompt's truncation ellipsis
    so prefix-matching doesn't get tripped by trivial differences."""
    s = (s or "").strip()
    if s.endswith("…"):
        s = s[:-1].rstrip()
    return " ".join(s.split())
