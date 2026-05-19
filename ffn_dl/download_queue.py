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
        # ``_pending`` mirrors the queue's logical depth under our own
        # lock. Tracking it explicitly closes the false-idle window
        # between ``Queue.get()`` returning an item and ``_active``
        # being incremented: during that gap ``self._q.qsize()`` reads
        # zero and ``_active`` still reads zero, so snapshot() /
        # _has_active_background_work() would briefly conclude the
        # site is idle even though a job is about to run.
        self._pending = 0
        self._lock = threading.Lock()
        self._on_state_change = on_state_change

    @property
    def active(self) -> int:
        return self._active

    @property
    def pending(self) -> int:
        # Reads ``_pending`` from outside the lock; for a "are there
        # queued items?" check this is racy by an item or two but
        # accurate enough for UI/state callers. Snapshot() takes the
        # lock for an atomic read.
        return self._pending

    def enqueue(self, job_fn: Callable[[], object]) -> Future:
        fut: Future = Future()
        with self._lock:
            self._q.put((fut, job_fn))
            self._pending += 1
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
                # Re-check the queue under the lock before exiting.
                # Without this, a producer that called enqueue between
                # the timeout and the return saw self._worker.is_alive()
                # as still True (thread hadn't actually exited yet),
                # didn't spawn a replacement, and the new job sat in
                # the queue forever.
                with self._lock:
                    if self._q.empty():
                        self._worker = None
                        return
                continue
            # Future may have been cancelled while pending —
            # set_running_or_notify_cancel() returns False in that
            # case. Either way we've already pulled the item off the
            # queue, so decrement ``_pending`` here and continue.
            if not fut.set_running_or_notify_cancel():
                with self._lock:
                    self._pending -= 1
                self._fire_state_change()
                continue
            # Atomic-against-snapshot: move the just-pulled item from
            # pending → active under one lock acquisition so a
            # concurrent snapshot() always sees (a+p) >= 1 while this
            # job is in flight.
            with self._lock:
                self._pending -= 1
                self._active += 1
            self._fire_state_change()
            try:
                result = job_fn()
                fut.set_result(result)
            except Exception as exc:
                # Catch ``Exception`` not ``BaseException``: a worker
                # thread that swallows ``KeyboardInterrupt`` or
                # ``SystemExit`` would keep pulling more work even
                # after the user asked the process to stop. Plain
                # ``Exception`` covers every scraper / I/O failure the
                # queue is meant to recover from while letting fatal
                # signals propagate and tear the thread down cleanly.
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
        # Take per-site (active, pending) atomically under each queue's
        # own lock so a concurrent enqueue/finish can't tear the snapshot
        # (e.g. report active=0,pending=0 for a site whose state changed
        # between the truthy filter and the value read).
        out: dict[str, tuple[int, int]] = {}
        for name, q in qs:
            with q._lock:
                a = q._active
                # Read the lock-protected _pending instead of
                # _q.qsize(): qsize() drops to 0 the instant _drain's
                # Queue.get() returns, but _pending stays high until
                # the same lock acquisition that bumps _active. That
                # makes (a, p) atomically reflect "is anything in
                # flight for this site?" even mid-job-pickup.
                p = q._pending
            if a > 0 or p > 0:
                out[name] = (a, p)
        return out

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

    @classmethod
    def cancel_site(cls, site_name: str) -> int:
        """Cancel every queued (not-yet-running) job for ``site_name``.

        Returns the number of jobs cancelled. The currently-running job
        (if any) keeps going — Python ``Future`` cancellation only
        applies to pending work, and the scraper's own download path
        doesn't accept a cooperative cancel token. Anything still queued
        behind the current job is drained and its ``Future`` flipped to
        cancelled, which the worker's ``set_running_or_notify_cancel``
        check honours when it pulls the next item.

        Use case: GUI user clicks "cancel queue" on a stuck site
        without nuking the rest of the application's queued work.
        """
        with cls._lock:
            q = cls._queues.get(site_name)
        if q is None:
            return 0
        cancelled = 0
        # Drain the queue in one pass under the queue's own lock so a
        # racing enqueue can't slip a job past us between the qsize
        # check and the cancel call. Every item we pull off was
        # ``_pending`` so we decrement the counter for each — whether
        # or not the Future's cancel succeeds.
        with q._lock:
            try:
                while True:
                    fut, _job_fn = q._q.get_nowait()
                    q._pending -= 1
                    if fut.cancel():
                        cancelled += 1
            except queue.Empty:
                pass
        q._fire_state_change()
        if cancelled:
            logger.info(
                "DownloadQueues.cancel_site: cancelled %d job(s) for %s",
                cancelled, site_name,
            )
        return cancelled
