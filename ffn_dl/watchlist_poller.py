"""Background poll thread for the GUI's watchlist autopoll.

Split from ``gui.py`` so the main-frame module doesn't grow a second
threading subsystem on top of the download workers. The thread is a
daemon — it dies with the process — so :meth:`WatchlistPoller.stop`
is a best-effort hint rather than a blocking join; joining would
freeze GUI close for up to one poll interval while the worker waits
on its sleep.

Polling goes through :func:`ffn_dl.watchlist.run_once`, the same
entry point the ``--watchlist-run`` CLI uses. Results are logged via
the root logger so they land in both the GUI status pane (through
``_WxLogHandler``) and the rotating file log when the user has that
enabled for bug reports.
"""

import logging
import threading

from . import prefs as _p
from .watchlist import MIN_POLL_INTERVAL_S, WatchlistStore, run_once


logger = logging.getLogger(__name__)


class WatchlistPoller:
    """Single-thread scheduler for background watchlist polls.

    Lifecycle is driven from :class:`MainFrame`: constructed at
    startup, ``start()``-ed if the autopoll pref is on, ``stop()``-ed
    on app close, and ``reconfigure()``-d whenever the user changes
    watchlist prefs through the Preferences dialog.
    """

    def __init__(self, prefs, on_result=None):
        self._prefs = prefs
        # Optional callback invoked with the PollResult list on the
        # worker thread after each poll. Reserved for the Watchlist
        # UI tab (Task #3) to refresh its table without reloading
        # the whole store from disk.
        self._on_result = on_result
        self._stop = threading.Event()
        self._thread = None
        self._interval = self._read_interval()

    # ── Config ──────────────────────────────────────────────

    @staticmethod
    def _clamp_interval(raw):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = _p.DEFAULT_WATCH_POLL_INTERVAL_S
        return max(MIN_POLL_INTERVAL_S, value)

    def _read_interval(self):
        return self._clamp_interval(
            self._prefs.get(_p.KEY_WATCH_POLL_INTERVAL_S)
        )

    # ── Lifecycle ───────────────────────────────────────────

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return
        self._stop.clear()
        self._interval = self._read_interval()
        self._thread = threading.Thread(
            target=self._run,
            name="ffn-dl-watchlist-poller",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Watchlist autopoll started (%d second interval).",
            self._interval,
        )

    def stop(self):
        """Signal the poll thread to stop. Does not block.

        Joining would freeze the caller for up to ``_interval`` seconds
        because the thread sleeps in :meth:`threading.Event.wait`. The
        thread is a daemon, so the process can exit without waiting
        for it; ``stop()`` only matters for in-process reconfigure.
        """
        if self._thread is None:
            return
        self._stop.set()
        self._thread = None
        logger.info("Watchlist autopoll stopped.")

    def reconfigure(self):
        """Re-read prefs after the user changes them, and align the
        thread state accordingly. Safe to call from the main thread.
        """
        autopoll = self._prefs.get_bool(_p.KEY_WATCH_AUTOPOLL)
        self._interval = self._read_interval()
        if autopoll and not self.is_running():
            self.start()
        elif not autopoll and self.is_running():
            self.stop()

    # ── Thread body ─────────────────────────────────────────

    def _run(self):
        while True:
            # Event.wait returns True iff stop was set within the
            # timeout window — that's our clean shutdown path.
            if self._stop.wait(timeout=self._interval):
                return
            try:
                self._do_poll()
            except Exception:
                # run_once traps its own scraper/network errors, but a
                # store-load failure (disk, permissions) could still
                # surface here. Log and keep the thread alive — if we
                # die here the user has no autopoll and no error.
                logger.exception("Watchlist poll iteration failed.")

    def _do_poll(self):
        store = WatchlistStore.load_default()
        watches = store.all()
        if not watches:
            logger.debug("Watchlist is empty; skipping scheduled poll.")
            return

        logger.info(
            "Watchlist poll: checking %d watch(es)...", len(watches),
        )
        results = run_once(store, self._prefs)
        total_new = sum(len(r.new_items) for r in results if r.ok)
        errors = sum(1 for r in results if not r.ok)

        if total_new:
            logger.info(
                "Watchlist poll done: %d new item(s) across %d watch(es)"
                "%s.",
                total_new, len(results),
                f", {errors} error(s)" if errors else "",
            )
        elif errors:
            logger.warning(
                "Watchlist poll done: no new items, %d error(s) of %d.",
                errors, len(results),
            )
        else:
            logger.debug(
                "Watchlist poll done: no changes across %d watch(es).",
                len(results),
            )

        if self._on_result is not None:
            try:
                self._on_result(results)
            except Exception:
                logger.exception("Watchlist on_result callback failed.")
