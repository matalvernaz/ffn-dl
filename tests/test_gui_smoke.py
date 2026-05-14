"""Headless GUI smoke test.

Boots ``MainFrame`` plus the satellite frames (Search, Watchlist,
Library) under a wx ``App`` and tears them down. Catches the class
of regression that AST-level checks miss: a Bind() that referenced
a renamed handler, a menu item with an empty label, an event
handler that crashes during initial-state setup, a satellite frame
constructor whose signature drifted away from its caller.

Skips silently when:

* wxPython isn't installed (CI without GTK headers, or a CLI-only
  install). The test is a pure correctness signal — environments
  that can't render shouldn't fail it.
* ``DISPLAY`` isn't set (no X server, no xvfb-run wrapper). On
  Linux CI this means ``xvfb-run pytest`` or installing
  ``pytest-xvfb``; locally a workstation already has a display.
"""

from __future__ import annotations

import os
import sys

import pytest

wx = pytest.importorskip("wx")

if not os.environ.get("DISPLAY"):
    pytest.skip(
        "GUI smoke test needs a display server (run under xvfb-run "
        "or set DISPLAY=:0).",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def wx_app():
    """A single ``wx.App`` for the whole session.

    wxPython doesn't tolerate constructing a second ``wx.App`` in
    the same process — the GTK signal table from the first one
    survives the Destroy() and segfaults the next App's init. Tests
    that need a frame share this one and rely on per-test
    Destroy() of the frame itself to keep handler tables clean.
    """
    app = wx.App(False)
    yield app
    # Don't ``app.Destroy()`` here — pytest-finalize ordering can
    # call this after a frame fixture's teardown has already torn
    # the GTK loop down, segfaulting the interpreter shutdown.


def test_main_frame_constructs(wx_app):
    """MainFrame's __init__ wires every menu, toolbar, and bind in
    one shot — a regression that breaks any of those raises here."""
    from ffn_dl import gui

    frame = gui.MainFrame()
    try:
        title = frame.GetTitle()
        assert "ffn-dl" in title.lower()

        menubar = frame.GetMenuBar()
        assert menubar is not None
        assert menubar.GetMenuCount() >= 1

        # Every non-separator item must have a non-empty label —
        # an empty label means SetItemLabel was passed "" by mistake
        # (a renamed constant collapsing to None) and will read as
        # blank in NVDA.
        for i in range(menubar.GetMenuCount()):
            menu = menubar.GetMenu(i)
            for item in menu.GetMenuItems():
                if item.GetKind() == wx.ITEM_SEPARATOR:
                    continue
                label = item.GetItemLabelText() or item.GetItemLabel()
                assert label, (
                    f"empty label on menu '{menubar.GetMenuLabelText(i)}' "
                    f"item id={item.GetId()}"
                )
    finally:
        frame.Destroy()


def test_show_update_dialog_helper_callable(wx_app):
    """``_show_update_dialog`` is the four-button update prompt
    introduced in 2.3.2. Confirms the helper survives import
    without actually showing the modal."""
    from ffn_dl import gui

    assert callable(gui._show_update_dialog)


def test_search_frame_roundtrip(wx_app):
    """SearchFrame's constructor signature drifted away from its
    caller in past refactors — round-trip catches the next time."""
    from ffn_dl.gui import MainFrame
    from ffn_dl.gui_search import SearchFrame, _ffn_search_spec

    frame = MainFrame()
    try:
        sf = SearchFrame(frame, "ffn", _ffn_search_spec())
        sf.Destroy()
    finally:
        frame.Destroy()


def test_watchlist_frame_roundtrip(wx_app):
    """WatchlistFrame opens and closes without firing the poller."""
    from ffn_dl.gui import MainFrame
    from ffn_dl.gui_watchlist import WatchlistFrame

    frame = MainFrame()
    try:
        wf = WatchlistFrame(frame)
        wf.Destroy()
    finally:
        frame.Destroy()


def test_library_frame_roundtrip(wx_app):
    """LibraryFrame's prefs argument is required — a refactor that
    flips to lazy ``self.prefs`` in MainFrame would break this."""
    from ffn_dl.gui import MainFrame
    from ffn_dl.library.gui import LibraryFrame

    frame = MainFrame()
    try:
        lf = LibraryFrame(frame, frame.prefs)
        lf.Destroy()
    finally:
        frame.Destroy()


def test_announce_label_updates_label_and_accessible_name(wx_app):
    """``_announce_label`` mirrors the visible label into the MSAA
    accessible name. Without that mirror, NVDA on Windows reads the
    *initial* name forever — the user never hears the status flip
    from ``(not installed)`` to ``(installing...)`` to ``(installed)``
    that the sighted UI shows immediately.

    We verify the public observable (``GetName()`` matches the new
    label after the call) rather than the MSAA event itself, which
    is platform-internal."""
    from ffn_dl.gui import _announce_label

    frame = wx.Frame(None)
    try:
        text = wx.StaticText(frame, label="(initial)")
        text.SetName("(initial)")
        _announce_label(text, "(installing...)")
        assert text.GetLabel() == "(installing...)"
        assert text.GetName() == "(installing...)"
        _announce_label(text, "(installed)")
        assert text.GetLabel() == "(installed)"
        assert text.GetName() == "(installed)"
    finally:
        frame.Destroy()


def test_idle_event_pumps_clean(wx_app):
    """Some MainFrame init paths schedule wx.CallAfter to populate
    the recent-files list, log pane, etc. Pumping idle once flushes
    those handlers in a controlled context so we catch any crash
    here instead of in production."""
    from ffn_dl.gui import MainFrame

    frame = MainFrame()
    try:
        wx.SafeYield()
        frame.ProcessEvent(wx.IdleEvent())
    finally:
        frame.Destroy()


def test_add_from_url_list_dialog_constructs(wx_app):
    """The 2.4.0 bulk-import dialog has six interactive widgets
    (URL field, max-results spin, Extract button, list, three
    select-* buttons, OK/Cancel). Construction without a parent
    catches the class of regression where a Bind() targets a
    renamed handler — which on the GUI side never crashes the
    test suite, only production."""
    from ffn_dl.gui import MainFrame
    from ffn_dl.gui_dialogs import AddFromUrlListDialog

    frame = MainFrame()
    try:
        dlg = AddFromUrlListDialog(frame)
        try:
            assert dlg.GetTitle() == "Add from URL list"
            # OK is disabled until extraction populates the list
            assert not dlg.ok_btn.IsEnabled()
            # SetName landed on every widget the user tabs to —
            # NVDA reads the name when focus arrives, so a missing
            # name shows up as a blank announcement.
            assert dlg.url_ctrl.GetName()
            assert dlg.max_ctrl.GetName()
            assert dlg.list_ctrl.GetName()
            # picked_works on an unpopulated dialog returns []
            assert dlg.picked_works() == []
        finally:
            dlg.Destroy()
    finally:
        frame.Destroy()
