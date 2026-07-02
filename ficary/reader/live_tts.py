"""Live text-to-speech for the reader (app-voice mode).

Walks a chapter's chunks, synthesizing each with the existing
``tts_providers.synthesize`` dispatcher (edge/piper) to a temp file, playing
it through the shared audio engine's ``voice`` channel, and following along
with a highlight callback. One chunk is prefetched while the current one
plays. Emits reader lifecycle events so the soundscape ducks/restores.

Concurrency model: every ``start()``/``stop()`` bumps a generation token.
The worker loop, the prefetch threads, and every blocking wait carry the
generation they were started under; a stale generation discards its results
instead of mutating current state, so a worker that outlives a 0.5 s join
(e.g. stuck in a slow network synth) can never play audio, repaint a
highlight, or poison the synth cache of the run that replaced it. All waits
are short timeout loops that re-check the stop flag — nothing in here parks
unbounded.

The sequencing is testable with a fake engine (whose ``play_file`` invokes
``on_done``) and a fake synth — no audio device or network needed.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from ..audio.engine import CHANNEL_VOICE, AudioEngine
from ..audio.events import Event, ReaderEvent
from .chunker import Chunk, chunk_text

logger = logging.getLogger(__name__)

_WAIT_SLICE_S = 0.2   # granularity of every stop-aware wait
_JOIN_TIMEOUT_S = 0.5  # stop() gives the worker this long; beyond it the
                       # generation token makes the straggler harmless
_SYNTH_ATTEMPTS = 2    # transient edge-tts errors get one retry


class _Slot:
    """In-flight/completed synth state for one (generation, chunk) pair."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.path: Optional[Path] = None


class LiveTTSController:
    def __init__(self, engine: AudioEngine, *, voice: str, rate: str = "0",
                 on_highlight: Optional[Callable[[Chunk], None]] = None,
                 on_complete: Optional[Callable[[int], None]] = None,
                 story_key: Optional[str] = None,
                 synth: Optional[Callable[..., None]] = None,
                 tmp_dir: Optional[Path] = None):
        self._engine = engine
        self._voice = voice
        self._rate = _to_edge_rate(rate)
        self._on_highlight = on_highlight
        self._on_complete = on_complete
        self._story_key = story_key
        self._synth = synth or _default_synth
        self._owns_tmp = tmp_dir is None
        self._tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="ficary-reader-tts-"))
        self._chunks: list[Chunk] = []
        self._lock = threading.Lock()
        self._gen = 0
        self._slots: dict[tuple[int, int], _Slot] = {}
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._chapter_number: Optional[int] = None

    def is_active(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    # ── transport ─────────────────────────────────────────────────
    def start(self, text: str, chapter_number: Optional[int] = None) -> None:
        self.stop()
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._slots = {k: v for k, v in self._slots.items() if k[0] == gen}
        self._chunks = chunk_text(text)
        self._chapter_number = chapter_number
        self._stop.clear()
        self._resume.set()
        if not self._chunks:
            return
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._worker = threading.Thread(
            target=self._run, args=(gen, list(self._chunks)), daemon=True)
        self._worker.start()

    def pause(self) -> None:
        if not self.is_active():
            return
        self._resume.clear()
        self._engine.pause(CHANNEL_VOICE)
        self._emit(ReaderEvent.TTS_PAUSED)

    def resume(self) -> None:
        if not self.is_active():
            return
        self._engine.resume(CHANNEL_VOICE)
        self._resume.set()
        self._emit(ReaderEvent.TTS_RESUMED)

    def stop(self) -> None:
        was_active = self.is_active()
        with self._lock:
            self._gen += 1  # invalidate the running worker + its prefetches
        self._stop.set()
        self._resume.set()
        self._done.set()
        self._engine.stop(CHANNEL_VOICE)
        if self._worker and self._worker.is_alive() and self._worker is not threading.current_thread():
            self._worker.join(timeout=_JOIN_TIMEOUT_S)
        self._worker = None
        if was_active:
            self._emit(ReaderEvent.TTS_STOPPED, payload={"reason": "stopped"})
        if self._owns_tmp and not self.is_active():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # ── worker loop ───────────────────────────────────────────────
    def _current(self, gen: int) -> bool:
        with self._lock:
            return gen == self._gen and not self._stop.is_set()

    def _wait_stop_aware(self, event: threading.Event, gen: int) -> bool:
        """Wait on ``event``; True when it fired while this generation is
        still current, False when superseded/stopped."""
        while not event.wait(_WAIT_SLICE_S):
            if not self._current(gen):
                return False
        return self._current(gen)

    def _run(self, gen: int, chunks: list[Chunk]) -> None:
        self._emit(ReaderEvent.TTS_STARTED)
        failed = 0
        completed = False
        try:
            for i, chunk in enumerate(chunks):
                if not self._current(gen):
                    return
                if not self._wait_stop_aware(self._resume, gen):
                    return
                path = self._ensure_synth(gen, i)
                if not self._current(gen):
                    return
                self._prefetch(gen, i + 1)
                if path is None:
                    failed += 1
                    continue
                # Pause may have arrived during the synth — honour it
                # before starting the next clip, not one chunk late.
                if not self._wait_stop_aware(self._resume, gen):
                    return
                if self._on_highlight:
                    self._on_highlight(chunk)
                self._emit(ReaderEvent.TTS_CHUNK, chunk.index)
                self._done.clear()
                self._engine.play_file(path, CHANNEL_VOICE, on_done=self._done.set)
                if not self._wait_stop_aware(self._done, gen):
                    return
            completed = True
        finally:
            if completed and self._current(gen):
                self._emit(ReaderEvent.TTS_STOPPED,
                           payload={"reason": "completed", "failed_chunks": failed})
                if self._on_complete:
                    self._on_complete(failed)

    def _ensure_synth(self, gen: int, index: int) -> Optional[Path]:
        if not (0 <= index < len(self._chunks)):
            return None
        key = (gen, index)
        with self._lock:
            if gen != self._gen:
                return None
            slot = self._slots.get(key)
            if slot is not None:
                owner = False
            else:
                slot = _Slot()
                self._slots[key] = slot
                owner = True
        if not owner:
            # Another thread (prefetch or worker) owns this synth — wait for
            # it rather than racing a second synthesis to the same file.
            if not self._wait_stop_aware(slot.event, gen):
                return None
            return slot.path
        out = self._tmp_dir / f"g{gen:04d}_chunk_{index:05d}.mp3"
        text = self._chunks[index].text
        path: Optional[Path] = None
        for attempt in range(1, _SYNTH_ATTEMPTS + 1):
            if not self._current(gen):
                break
            try:
                self._synth(self._voice, text, out, rate=self._rate)
                path = out
                break
            except Exception:
                logger.exception(
                    "TTS synth failed for chunk %d (attempt %d/%d)",
                    index, attempt, _SYNTH_ATTEMPTS,
                )
        slot.path = path
        slot.event.set()
        return path if self._current(gen) else None

    def _prefetch(self, gen: int, index: int) -> None:
        if not (0 <= index < len(self._chunks)):
            return
        with self._lock:
            if gen != self._gen or (gen, index) in self._slots:
                return
        threading.Thread(
            target=self._ensure_synth, args=(gen, index), daemon=True).start()

    def _emit(self, kind: ReaderEvent, chunk_index: Optional[int] = None,
              payload: Optional[dict] = None) -> None:
        merged = dict(payload or {})
        if chunk_index is not None:
            merged["chunk_index"] = chunk_index
        self._engine.emit(Event(kind=kind, story_key=self._story_key,
                                chapter_number=self._chapter_number, payload=merged))


def _to_edge_rate(rate: Optional[str]) -> Optional[str]:
    """Normalize the reader's speech-rate pref to the edge-style ``"+N%"``
    string ``tts_providers.synthesize`` expects. A bare integer (``"10"``,
    ``"-5"``) gains the sign and percent; ``"0"``/empty means provider
    default (None) — passing a malformed rate makes edge-tts raise on every
    single chunk."""
    if not rate:
        return None
    text = str(rate).strip()
    if not text or text == "0":
        return None
    if text.endswith("%"):
        return text
    try:
        value = int(text)
    except ValueError:
        logger.warning("Unrecognized speech rate %r; using provider default", rate)
        return None
    if value == 0:
        return None
    return f"{value:+d}%"


def _default_synth(voice: str, text: str, output_path: Path, *,
                   rate: Optional[str] = None) -> None:
    from ..tts_providers import synthesize
    synthesize(voice, text, output_path, rate=rate)
