"""Per-story accent + character profile JSON files.

Two on-disk files live next to each audiobook output (alongside the
existing voice / pronunciation maps):

- ``.ffn-accents-<story_id>.json``: ``{"Harry Potter": "en-GB", ...}``
  Maps speaker → BCP-47 locale code. Filters the candidate voice pool
  for each character; the audiobook generator refuses voices whose
  locale doesn't match. Special value ``"any"`` (or omission) means
  no filter.
- ``.ffn-profile-<story_id>.json``: per-character profile dict
  ``{"Harry Potter": {"gender": "male", "age": "young adult",
  "accent": "en-GB", "tone": "earnest"}}``. Computed by the LLM (when
  the LLM attribution backend is enabled) and used as a richer prior
  for VoiceMapper than the gender heuristic alone.

Both files are user-editable. Edits survive re-renders because the
generator only seeds them when they're empty.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_accents(path: Path) -> dict[str, str]:
    """Read a ``.ffn-accents-*.json`` file. Returns ``{}`` on missing,
    unreadable, or wrong-shape files — accent filtering is a positive
    override, so a corrupt file should fall through to "no filter"
    rather than blocking the render."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Accent map unreadable (%s); ignoring", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def save_accents(path: Path, accents: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    skeleton = {
        "_comment": (
            "Per-character accent overrides for the audiobook generator. "
            "Values are BCP-47 locale codes (en-GB, en-IE, en-AU, en-IN, "
            "fr-FR, ...) — VoiceMapper restricts each character's voice "
            "pool to that locale. Use 'any' (or omit the entry) to skip "
            "the filter for a given character."
        ),
    }
    skeleton.update(accents)
    path.write_text(json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")


def load_profiles(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Character profile map unreadable (%s); ignoring", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = v
    return out


def save_profiles(path: Path, profiles: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    skeleton = {
        "_comment": (
            "Per-character profiles seeded by the LLM attribution "
            "backend. Each entry: gender (male/female/neutral), age "
            "(child/teen/young adult/adult/elder), accent (BCP-47), "
            "tone (free-form descriptor). The audiobook generator "
            "uses these to pick voices that match the character "
            "rather than just gender. Hand-edit freely."
        ),
    }
    skeleton.update(profiles)
    path.write_text(json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")


def filter_user_entries(d: dict) -> dict:
    """Drop the ``_comment`` skeleton key when iterating — keeps the
    file self-documenting on disk without polluting the runtime view."""
    return {k: v for k, v in d.items() if not k.startswith("_")}
