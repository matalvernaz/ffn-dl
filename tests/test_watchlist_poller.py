"""Tests for the GUI-only watchlist poll thread.

The thread itself (timing, logging, actual network polls) is out of
scope — spinning up real threads in pytest is flaky and doesn't
catch more than the unit-level checks here would. These tests pin
down the two pieces of the poller that have actual logic: clamping
the poll interval to the watchlist minimum, and the reconfigure
transitions (autopoll on/off) reading current pref values.
"""
from __future__ import annotations

import pytest

from ffn_dl import prefs as _p
from ffn_dl.watchlist import MIN_POLL_INTERVAL_S
from ffn_dl.watchlist_poller import WatchlistPoller


class _FakePrefs:
    """In-memory stand-in for :class:`ffn_dl.prefs.Prefs`."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key, default=None):
        raw = self._values.get(key, "")
        if raw == "" or raw is None:
            return default if default is not None else _p.DEFAULTS.get(key)
        return raw

    def get_bool(self, key, default=None):
        if default is None:
            default = _p.DEFAULTS.get(key, False)
        return bool(self._values.get(key, default))

    def set(self, key, value):
        self._values[key] = value

    def set_bool(self, key, value):
        self._values[key] = bool(value)


def test_interval_clamped_to_minimum():
    prefs = _FakePrefs({_p.KEY_WATCH_POLL_INTERVAL_S: "30"})
    poller = WatchlistPoller(prefs)
    assert poller._interval == MIN_POLL_INTERVAL_S


def test_interval_accepts_value_above_minimum():
    prefs = _FakePrefs({_p.KEY_WATCH_POLL_INTERVAL_S: str(60 * 60)})
    poller = WatchlistPoller(prefs)
    assert poller._interval == 60 * 60


def test_interval_falls_back_to_default_on_garbage():
    prefs = _FakePrefs({_p.KEY_WATCH_POLL_INTERVAL_S: "not-a-number"})
    poller = WatchlistPoller(prefs)
    assert poller._interval == _p.DEFAULT_WATCH_POLL_INTERVAL_S


def test_reconfigure_starts_thread_when_autopoll_enabled(monkeypatch):
    prefs = _FakePrefs({_p.KEY_WATCH_AUTOPOLL: True})
    poller = WatchlistPoller(prefs)

    started = []

    def fake_start():
        started.append(True)

    monkeypatch.setattr(poller, "start", fake_start)
    poller.reconfigure()
    assert started == [True]


def test_reconfigure_stops_thread_when_autopoll_disabled(monkeypatch):
    prefs = _FakePrefs({_p.KEY_WATCH_AUTOPOLL: False})
    poller = WatchlistPoller(prefs)

    # Simulate a live thread so reconfigure sees is_running() = True
    class _LiveThread:
        def is_alive(self):
            return True

    poller._thread = _LiveThread()

    stopped = []

    def fake_stop():
        stopped.append(True)

    monkeypatch.setattr(poller, "stop", fake_stop)
    poller.reconfigure()
    assert stopped == [True]


def test_reconfigure_noop_when_already_in_target_state(monkeypatch):
    """Enabled + already running → no start; disabled + not running → no stop."""
    prefs = _FakePrefs({_p.KEY_WATCH_AUTOPOLL: True})
    poller = WatchlistPoller(prefs)

    class _LiveThread:
        def is_alive(self):
            return True

    poller._thread = _LiveThread()

    calls = []
    monkeypatch.setattr(poller, "start", lambda: calls.append("start"))
    monkeypatch.setattr(poller, "stop", lambda: calls.append("stop"))
    poller.reconfigure()
    assert calls == []

    prefs.set_bool(_p.KEY_WATCH_AUTOPOLL, False)
    poller._thread = None
    poller.reconfigure()
    assert calls == []
