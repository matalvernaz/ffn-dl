"""Bridges reader lifecycle events to the shared engine's ambient channel.

Subscribes to the audio engine's event bus and reacts by calling engine mixer
methods only (fade in on open, duck under narration, restore, fade out on
close) — no wx, so it's safe off the GUI thread.

Sources are built on a worker thread: adding a looping source decodes the
file through ffmpeg, which is seconds of subprocess time for long ambience —
blocking the GUI thread (where READER_OPENED is emitted from) froze the
reader window on open. A build generation token keeps a teardown that races
a still-running build from resurrecting its sources.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from ..audio.engine import CHANNEL_AMBIENT, AudioEngine
from ..audio.events import Event, ReaderEvent
from . import library
from .model import Soundscape

logger = logging.getLogger(__name__)

_DUCK_LEVEL = 0.25
_FADE_IN_MS = 1500
_FADE_OUT_MS = 1500
_DUCK_MS = 250
_RESTORE_MS = 400


class SoundscapeSession:
    """One per open reader. Holds the story's assigned soundscape (or None)."""

    def __init__(self, engine: AudioEngine, soundscape: Optional[Soundscape] = None):
        self._engine = engine
        self._soundscape = soundscape
        self._started = False
        self._reader_open = False
        self._voice_active = False
        self._lock = threading.Lock()
        self._build_gen = 0
        self._build_thread: Optional[threading.Thread] = None
        engine.subscribe(self._on_event)

    def set_soundscape(self, soundscape: Optional[Soundscape]) -> None:
        if self._started:
            self._teardown()
        self._soundscape = soundscape
        # Build whenever the reader is open — the old gate on "was already
        # running" made the FIRST assignment to a story (or any assignment
        # after a sleep-timer fade-out) a silent no-op until reopen.
        if self._reader_open:
            self._build_and_fade_in()

    def close(self) -> None:
        self._engine.unsubscribe(self._on_event)
        with self._lock:
            self._build_gen += 1
        # READER_CLOSED already started the graceful fade (which stops the
        # channel when it completes); hard-stop only if something is still
        # marked running — otherwise close() would cut the fade off after
        # one step and the designed fade-out was never audible.
        if self._started:
            self._teardown()

    def _on_event(self, event: Event) -> None:
        kind = event.kind
        if kind is ReaderEvent.READER_OPENED:
            self._reader_open = True
            self._build_and_fade_in()
        elif kind is ReaderEvent.READER_CLOSED:
            self._reader_open = False
            self._fade_out()
        elif kind in (ReaderEvent.TTS_STARTED, ReaderEvent.TTS_RESUMED):
            self._voice_active = True
            if self._started:
                self._engine.duck(CHANNEL_AMBIENT, _DUCK_LEVEL, _DUCK_MS)
        elif kind in (ReaderEvent.TTS_PAUSED, ReaderEvent.TTS_STOPPED):
            self._voice_active = False
            if self._started:
                self._engine.restore(CHANNEL_AMBIENT, _RESTORE_MS)

    def _build_and_fade_in(self) -> None:
        if self._started or not self._soundscape:
            return
        with self._lock:
            self._build_gen += 1
            gen = self._build_gen
        thread = threading.Thread(
            target=self._build_sync, args=(gen,), daemon=True,
            name="ficary-soundscape-build",
        )
        self._build_thread = thread
        thread.start()

    def _join_build(self, timeout: float = 2.0) -> None:
        """Wait for an in-flight source build. Test hook — production
        callers never need to block on the build."""
        thread = self._build_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def _build_sync(self, gen: int) -> None:
        sc = self._soundscape
        if sc is None:
            return
        self._engine.set_reverb_room_size(sc.reverb_room_size)
        # Start silent so freshly-added loops don't blip at full volume for
        # the instant before the fade takes over. (fade_in below resets the
        # restore target to the soundscape's master volume.)
        self._engine.set_gain(CHANNEL_AMBIENT, 0.0)
        added = False
        for snd in sc.sounds:
            with self._lock:
                if gen != self._build_gen:
                    self._engine.stop(CHANNEL_AMBIENT)
                    return
            path = library.resolve_source(snd.source)
            if path is None:
                logger.info("Soundscape sound not found: %s", snd.source)
                continue
            handle = self._engine.add_looping_source(
                path, gain=snd.volume, positional=snd.positional,
                azimuth=snd.azimuth, elevation=snd.elevation,
                distance=snd.distance, channel=CHANNEL_AMBIENT)
            added = added or handle is not None
        with self._lock:
            if gen != self._build_gen:
                self._engine.stop(CHANNEL_AMBIENT)
                return
            if not added:
                return
            self._started = True
        self._engine.fade_in(CHANNEL_AMBIENT, _FADE_IN_MS, to=sc.master_volume)
        if self._voice_active:
            # Swapped/assigned while narration is running — come up ducked,
            # not on top of the voice.
            self._engine.duck(CHANNEL_AMBIENT, _DUCK_LEVEL, _DUCK_MS)

    def fade_out(self) -> None:
        """Public fade-out of the ambient bed (used by the sleep timer)."""
        self._fade_out()

    def _fade_out(self) -> None:
        if self._started:
            with self._lock:
                self._build_gen += 1
            self._engine.fade_out(CHANNEL_AMBIENT, _FADE_OUT_MS, then_stop=True)
            self._started = False

    def _teardown(self) -> None:
        with self._lock:
            self._build_gen += 1
        self._engine.stop(CHANNEL_AMBIENT)
        self._started = False
