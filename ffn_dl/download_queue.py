"""Per-site download queue for concurrent cross-site downloads.

Every supported site gets its own FIFO queue and a lazily-spawned
worker thread. Jobs enqueued for the same site run serially — so a
single scraper's rate-limit floor (FFN's 6s, AO3's default pacing) is
honoured — while jobs on different sites run in parallel. That is,
checking for updates on a library of 800 FFN stories no longer blocks
a one-off AO3 download from the URL bar.

Every download entry point funnels through this module: manual
downloads, file updates, voice previews, the clipboard watcher, and
the library-update Phase 3 loop. That way the manual and update-all
code paths share one serialization policy per site, rather than
each running its own parallel worker on top of the other and
tripping the site's rate limit.

Worker threads are named ``dlq-<site_name>``. The GUI's log helpers
inspect :func:`threading.current_thread` to auto-prefix status lines
with ``[<SITE>] ``, so the single shared log pane stays readable when
two sites are emitting progress concurrently.
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from typing import Callable


logger = logging.getLogger(__name__)

# How long an idle site-worker thread waits on an empty queue before
# exiting. Low enough that we don't keep ~20 threads around after a
# library sweep finishes; high enough that two back-to-back Downloads
# for the same site don't constantly thrash through thread creation.
_WORKER_IDLE_TIMEOUT_S = 5.0

# Thread-name prefix used for every worker. The GUI's log helpers key
# on this to inject a ``[<SITE>] `` prefix into status lines emitted
# on a worker thread.
WORKER_THREAD_PREFIX = "dlq-"


def site_from_thread_name(name: str) -> str | None:
    """Return the site name encoded in a worker thread's name, or None.

    The GUI's ``_log`` calls use this to decide whether a message came
    from a site-queue worker and therefore deserves a ``[<SITE>] ``
    prefix. Non-worker threads (``MainThread``, ad-hoc probe pools)
    return ``None`` and the message is logged unprefixed.
    """
    if not name.startswith(WORKER_THREAD_PREFIX):
        return None
    return name[len(WORKER_THREAD_PREFIX):] or None


class _SiteQueue:
    """FIFO queue and single worker thread for one site.

    The worker is lazily created on the first enqueue and exits after
    ``_WORKER_IDLE_TIMEOUT_S`` seconds of nothing to do; the next
    enqueue spawns a fresh one. Keeping a lazy lifecycle rather than
    pinning one thread per supported site for the whole process means
    idle sites (the user hasn't touched Wattpad in this session) cost
    nothing beyond a dict entry.
    """

    def __init__(
        self,
        site_name: str,
        on_state_change: Callable[[str, int, int], None],
    ) -> None:
        self._site_name = site_name
        self._q: "queue.Queue[tuple[Future, Callable[[], object]]]" = queue.Queue()
        self._worker: threading.Thread | None = None
        self._active = 0
        self._lock = threading.Lock()
        self._on_state_change = on_state_change

    @property
    def active(self) -> int:
        return self._active

    @property
    def pending(self) -> int:
        return self._q.qsize()

    def enqueue(self, job_fn: Callable[[], object]) -> Future:
        fut: Future = Future()
        with self._lock:
            self._q.put((fut, job_fn))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._drain,
                    name=f"{WORKER_THREAD_PREFIX}{self._site_name}",
                    daemon=True,
                )
                self._worker.start()
        self._fire_state_change()
        return fut

    def _fire_state_change(self) -> None:
        try:
            self._on_state_change(
                self._site_name, self._active, self._q.qsize(),
            )
        except Exception:
            logger.debug("SiteQueue state listener raised", exc_info=True)

    def _drain(self) -> None:
        while True:
            try:
                fut, job_fn = self._q.get(timeout=_WORKER_IDLE_TIMEOUT_S)
            except queue.Empty:
                return
            # Future may have been cancelled while pending — set_running_or_
            # notify_cancel() returns False in that case and we skip the job.
            if not fut.set_running_or_notify_cancel():
                self._fire_state_change()
                continue
            with self._lock:
                self._active += 1
            self._fire_state_change()
            try:
                result = job_fn()
                fut.set_result(result)
            except BaseException as exc:
                # Never let a worker die quietly — log so users can
                # tell the difference between "job raised" and "queue
                # silently stopped processing".
                logger.exception(
                    "Site-queue job raised: site=%s", self._site_name,
                )
                fut.set_exception(exc)
            finally:
                with self._lock:
                    self._active -= 1
                self._fire_state_change()


class DownloadQueues:
    """Process-wide registry of per-site queues.

    All access goes through the classmethods. A single global registry
    is intentional: both the GUI and the CLI ``_run_update_queue``
    Phase 3 loop need to share one queue per site, otherwise a manual
    download and a library-update download could double up on the same
    site and trip its rate limit.
    """

    _queues: dict[str, _SiteQueue] = {}
    _lock = threading.Lock()
    _listeners: list[Callable[[str, int, int], None]] = []
    _listeners_lock = threading.Lock()

    @classmethod
    def add_listener(
        cls, listener: Callable[[str, int, int], None],
    ) -> None:
        """Register a ``(site_name, active, pending)`` callback.

        Fires on every enqueue, job-start, and job-finish. Callbacks
        run on the worker thread (or the caller's thread for enqueue)
        — listeners that touch a UI toolkit must marshal to their own
        event loop (``wx.CallAfter`` etc.).
        """
        with cls._listeners_lock:
            cls._listeners.append(listener)

    @classmethod
    def remove_listener(
        cls, listener: Callable[[str, int, int], None],
    ) -> None:
        with cls._listeners_lock:
            try:
                cls._listeners.remove(listener)
            except ValueError:
                pass

    @classmethod
    def _notify(cls, site_name: str, active: int, pending: int) -> None:
        with cls._listeners_lock:
            snapshot = list(cls._listeners)
        for lst in snapshot:
            try:
                lst(site_name, active, pending)
            except Exception:
                logger.debug(
                    "DownloadQueues listener raised", exc_info=True,
                )

    @classmethod
    def enqueue(
        cls, site_name: str, job_fn: Callable[[], object],
    ) -> Future:
        """Queue ``job_fn`` on ``site_name``'s serial worker."""
        with cls._lock:
            q = cls._queues.get(site_name)
            if q is None:
                q = _SiteQueue(site_name, cls._notify)
                cls._queues[site_name] = q
        return q.enqueue(job_fn)

    @classmethod
    def snapshot(cls) -> dict[str, tuple[int, int]]:
        """Return ``{site: (active, pending)}`` for every non-idle site.

        Entries with active == 0 and pending == 0 are omitted so
        callers can treat a truthy result as "something is running".
        """
        with cls._lock:
            qs = list(cls._queues.items())
        return {
            name: (q.active, q.pending)
            for name, q in qs
            if q.active > 0 or q.pending > 0
        }

    @classmethod
    def is_site_busy(cls, site_name: str) -> bool:
        with cls._lock:
            q = cls._queues.get(site_name)
        return q is not None and (q.active > 0 or q.pending > 0)

    @classmethod
    def pending_for(cls, site_name: str) -> int:
        with cls._lock:
            q = cls._queues.get(site_name)
        return q.pending if q is not None else 0
