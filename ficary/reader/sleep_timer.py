"""Sleep timer for the reader: stop reading after a set delay.

Pure logic (no wx, no audio) so it's unit-testable. The reader wires
``on_expire`` to stop live TTS and fade the soundscape out.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

MIN_MINUTES = 5
MAX_MINUTES = 120


class SleepTimer:
    def __init__(self, on_expire: Callable[[], None]):
        self._on_expire = on_expire
        self._lock = threading.Lock()
        self._gen = 0  # bumps on start/cancel; a stale expiry must not fire
        self._timer: Optional[threading.Timer] = None
        self._deadline: Optional[float] = None  # time.monotonic() target

    def start(self, minutes: int) -> int:
        """Start (or restart) the timer. Clamps to [MIN, MAX]; returns the
        clamped minute count actually used."""
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(minutes)))
        secs = minutes * 60
        with self._lock:
            self._gen += 1
            if self._timer is not None:
                self._timer.cancel()
            self._deadline = time.monotonic() + secs
            timer = threading.Timer(secs, self._fire, args=(self._gen,))
            timer.daemon = True
            self._timer = timer
            timer.start()
        return minutes

    def cancel(self) -> None:
        with self._lock:
            self._gen += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._deadline = None

    def _fire(self, gen: Optional[int] = None) -> None:
        with self._lock:
            if gen is not None and gen != self._gen:
                # A restart/cancel raced this expiry: Timer.cancel() can't
                # stop a callback that already began, but the state belongs
                # to the newer timer now — don't clobber it or fire.
                return
            self._timer = None
            self._deadline = None
        self._on_expire()

    @property
    def active(self) -> bool:
        return self._deadline is not None

    def remaining_seconds(self) -> int:
        with self._lock:
            deadline = self._deadline
        if deadline is None:
            return 0
        return max(0, int(round(deadline - time.monotonic())))
