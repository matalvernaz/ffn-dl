"""Cross-cutting logging helpers.

Correlation IDs
===============

A library-wide update run produces thousands of log lines from every
scraper, the cache, the exporter, and the library refresh code —
interleaved if anything runs concurrently (AO3's bookmark pagination,
the parallel probe pool in ``--update-all``). Debugging "why did these
seven stories fail last night?" used to mean grepping a timestamp
range and eyeballing which lines went with which story.

This module adds a per-download correlation ID so every log line
emitted while working on a single story carries the same short tag.
A caller wrapping the download in :func:`correlation_context` gets
output like::

    [dl-a83f4c21] Fetching FFN story 12345 metadata...
    [dl-a83f4c21] Downloading FFN 12345: 'Some Title' by X (12 chapters)
    [dl-a83f4c21] Rate limited (HTTP 429), waiting 37s (attempt 1/5)

It's a pure-addition feature: scrapers keep using
``logging.getLogger(__name__)`` unchanged, and when no context is
active the tag simply doesn't appear. The filter is installed on the
``ffn_dl`` package logger at import time, so any child logger picks
it up automatically. That's important — I'm not going to touch every
``logger.info`` call in the scraper modules just to wire this up.

Implementation notes:

* Uses ``contextvars`` so concurrent threads each see their own
  correlation ID. The parallel chapter fetcher in ``BaseScraper``
  inherits the parent's context under Python's default ``copy_context``
  behaviour, so chapter logs end up tagged with the same story ID as
  the metadata fetch that kicked them off — which is what we want.
* Short IDs (8 hex chars) are long enough for a library update pass
  of a few hundred stories to avoid birthday collisions and short
  enough not to wreck the log layout.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from typing import Generator, Optional

_current_cid: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ffn_dl_correlation_id", default=None,
)

_FILTER_INSTALLED = False


def new_correlation_id() -> str:
    """Return a fresh 8-hex-character correlation id.

    Short enough to keep log lines scannable, long enough (~2^32 values)
    that a realistic library-update run won't hit a collision."""
    return uuid.uuid4().hex[:8]


def current_correlation_id() -> Optional[str]:
    """Return the correlation id active on the current context, or
    ``None`` when no :func:`correlation_context` is open. Exposed so
    callers can propagate the id to non-logging surfaces — progress
    callbacks, GUI status panes, crash reports."""
    return _current_cid.get()


@contextlib.contextmanager
def correlation_context(
    cid: Optional[str] = None,
) -> Generator[str, None, None]:
    """Run a block with a correlation id active on the current context.

    Every ``logger.info`` / ``logger.warning`` / etc. from any
    ``ffn_dl.*`` module inside the block has the id prepended, so a
    library update run can filter "all lines for story X" from an
    interleaved log by grepping the id. Returns the id so the caller
    can embed it in progress updates or error surfaces.

    When ``cid`` is omitted, a fresh id is generated — pass an explicit
    one if the caller already knows it (e.g. when resuming a download
    and wanting its logs to line up with the original session).
    """
    if cid is None:
        cid = new_correlation_id()
    token = _current_cid.set(cid)
    try:
        yield cid
    finally:
        _current_cid.reset(token)


def _correlation_record_factory(_default_factory=None):
    """Wrap the current :func:`logging.getLogRecordFactory` so every
    record for an ``ffn_dl.*`` logger gets the active correlation id
    prepended.

    A ``LogRecordFactory`` runs for every ``logging.log*`` call
    regardless of which logger emits it, which is exactly what we
    need — attaching a :class:`logging.Filter` to the ``ffn_dl``
    logger would *not* run for records emitted by child loggers
    (filters only run on the originating logger unless also
    attached to a handler, and ffn_dl ships no handlers of its own).

    We guard on ``record.name`` so third-party libraries' log lines
    aren't tagged with our correlation id.
    """
    base = _default_factory or logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        cid = _current_cid.get()
        if (
            cid
            and isinstance(record.name, str)
            and record.name.startswith("ffn_dl")
            and isinstance(record.msg, str)
            and not record.msg.startswith("[dl-")
        ):
            record.msg = f"[dl-{cid}] {record.msg}"
        return record

    return factory


def install_correlation_filter() -> None:
    """Install the correlation LogRecordFactory.

    Idempotent — safe to call multiple times. Called automatically
    from ``ffn_dl/__init__.py`` so the filter is active from the
    first import; no caller needs to remember to switch it on.
    """
    global _FILTER_INSTALLED
    if _FILTER_INSTALLED:
        return
    logging.setLogRecordFactory(_correlation_record_factory())
    _FILTER_INSTALLED = True
