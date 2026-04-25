"""Microsoft Edge Neural Voices via ``edge-tts``.

Wraps ``edge_tts.Communicate`` and ``edge_tts.list_voices`` so the rest
of the pipeline can treat it as a generic provider. The voice catalog
is fetched once per process from edge-tts (which talks to the same
remote endpoint Edge's "Read aloud" uses) and cached — list_voices is
not free (it's an HTTP call) and stays stable for hours at a time.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import VoiceInfo, voice_id

logger = logging.getLogger(__name__)


_CATALOG_CACHE: list[VoiceInfo] | None = None


class EdgeProvider:
    """Edge-tts wrapper exposing the standard provider interface."""

    name = "edge"

    def is_installed(self) -> bool:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False
        return True

    def list_voices(self) -> list[VoiceInfo]:
        global _CATALOG_CACHE
        if _CATALOG_CACHE is not None:
            return _CATALOG_CACHE
        if not self.is_installed():
            return []
        try:
            raw = asyncio.run(_list_voices_async())
        except RuntimeError as exc:
            # asyncio.run can fail inside an existing loop (notebooks,
            # the GUI's wx event loop calling list_voices synchronously
            # from a callback). Fall back to a fresh loop.
            if "asyncio.run() cannot be called" in str(exc):
                loop = asyncio.new_event_loop()
                try:
                    raw = loop.run_until_complete(_list_voices_async())
                finally:
                    loop.close()
            else:
                raise
        voices: list[VoiceInfo] = []
        for entry in raw:
            short = entry.get("ShortName", "")
            if not short:
                continue
            locale = entry.get("Locale", "") or short.rsplit("-", 1)[0]
            gender = entry.get("Gender", "Neutral") or "Neutral"
            friendly = entry.get("FriendlyName") or short
            voices.append(
                VoiceInfo(
                    id=voice_id(self.name, short),
                    provider=self.name,
                    short_name=short,
                    locale=locale,
                    gender=gender,
                    display=friendly,
                    description=entry.get("Status", ""),
                )
            )
        _CATALOG_CACHE = voices
        return voices

    def synthesize(
        self, *, text: str, voice: str, output_path: Path,
        rate: str | None = None,
        volume: str | None = None,
        pitch: str | None = None,
    ) -> None:
        if not self.is_installed():
            raise RuntimeError(
                "edge-tts is required for audiobook generation. "
                "Install with: pip install 'ffn-dl[audio]'"
            )
        kwargs = {"voice": voice}
        if rate:
            kwargs["rate"] = rate
        if volume:
            kwargs["volume"] = volume
        if pitch:
            kwargs["pitch"] = pitch
        try:
            asyncio.run(_synthesize_async(text, kwargs, str(output_path)))
        except RuntimeError as exc:
            if "asyncio.run() cannot be called" in str(exc):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        _synthesize_async(text, kwargs, str(output_path))
                    )
                finally:
                    loop.close()
            else:
                raise


async def _list_voices_async():
    import edge_tts
    return await edge_tts.list_voices()


async def _synthesize_async(text: str, kwargs: dict, output_path: str):
    import edge_tts
    comm = edge_tts.Communicate(text, **kwargs)
    await comm.save(output_path)
