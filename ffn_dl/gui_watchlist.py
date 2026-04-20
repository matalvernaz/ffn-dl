"""Watchlist management window.

Opens from the main window's Watchlist menu as a non-modal frame —
mirrors the ``SearchFrame`` pattern so users can leave the watchlist
open alongside downloads without blocking the main window. Everything
the CLI's ``--watchlist-add`` / ``--watchlist-add-search`` /
``--watchlist-remove`` / ``--watchlist-run`` flags do is reachable
here, with the notification dispatch and store persistence shared
directly with the CLI path (via :mod:`ffn_dl.watchlist`) so the two
entry points can't drift.

Accessibility notes per project conventions:

* Every interactive control carries a descriptive ``SetName`` so
  NVDA announces a useful label, even when the visible text is just
  an ampersand mnemonic (e.g. ``&Run Now``).
* Keyboard shortcuts (``Delete`` to remove, ``F5`` to refresh,
  ``Ctrl+R`` to run all enabled) are bound via an ``AcceleratorTable``
  so keyboard-only users don't have to tab to a button.
* The ListCtrl reports each column's text verbatim, so screen readers
  read the full row without extra formatting tricks — this list is
  display-only, so the ``[x] `` prefix trick used in the checkable
  dialogs isn't needed here.
"""

from __future__ import annotations

import logging
import threading

import wx

from . import prefs as _p
from .notifications import ALL_CHANNELS
from .watchlist import (
    SEARCH_SUPPORTED_SITES,
    VALID_WATCH_TYPES,
    WATCH_TYPE_AUTHOR,
    WATCH_TYPE_SEARCH,
    WATCH_TYPE_STORY,
    Watch,
    WatchlistStore,
    classify_target,
    run_once,
    site_key_for_url,
)


logger = logging.getLogger(__name__)


_SITE_LABELS = {
    "ffn": "FFN",
    "ao3": "AO3",
    "royalroad": "Royal Road",
    "literotica": "Literotica",
    "wattpad": "Wattpad",
    "ficwad": "FicWad",
    "mediaminer": "MediaMiner",
}


_TYPE_LABELS = {
    WATCH_TYPE_STORY: "Story",
    WATCH_TYPE_AUTHOR: "Author",
    WATCH_TYPE_SEARCH: "Search",
}


_COLUMNS = [
    ("Type", 80),
    ("Site", 90),
    ("Target", 320),
    ("Last checked", 170),
    ("Status", 220),
]


# IDs used for accelerator-driven menu commands. wxPython's
# AcceleratorTable binds an event id to a key combo; these are scoped
# to the Watchlist frame so they don't collide with anything else.
_ID_REMOVE = wx.NewIdRef()
_ID_RUN_ALL = wx.NewIdRef()
_ID_REFRESH = wx.NewIdRef()


class WatchlistFrame(wx.Frame):
    """Non-modal watchlist manager."""

    def __init__(self, main_frame):
        super().__init__(
            main_frame,
            title="Watchlist",
            size=(900, 560),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.main_frame = main_frame
        self.prefs = main_frame.prefs
        self._store = None
        self._watches: list[Watch] = []

        self._build_ui()
        self._install_accelerators()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._reload()
        self.Centre()

    # ── UI ──────────────────────────────────────────────────

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        hint = wx.StaticText(
            panel,
            label=(
                "Entries here are polled either on launch (autopoll in "
                "Preferences) or when you click Run Now. Notifications "
                "use the credentials configured in the Preferences dialog."
            ),
        )
        hint.Wrap(860)
        sizer.Add(hint, 0, wx.ALL, pad)

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Watchlist entries")
        for i, (label, width) in enumerate(_COLUMNS):
            self.list_ctrl.InsertColumn(i, label, width=width)
        self.list_ctrl.Bind(
            wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_run_selected(),
        )
        self.list_ctrl.Bind(
            wx.EVT_LIST_ITEM_SELECTED, self._on_selection_change,
        )
        self.list_ctrl.Bind(
            wx.EVT_LIST_ITEM_DESELECTED, self._on_selection_change,
        )
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        # Button rows. Split into two rows to keep each button visible
        # on narrower screens and to group "add" actions separately from
        # "run / remove" actions for screen-reader flow.
        add_row = wx.BoxSizer(wx.HORIZONTAL)
        self.add_story_btn = wx.Button(panel, label="Add &Story URL...")
        self.add_story_btn.SetName("Add story URL watch")
        self.add_story_btn.Bind(
            wx.EVT_BUTTON,
            lambda e: self._open_url_dialog(WATCH_TYPE_STORY),
        )
        add_row.Add(self.add_story_btn, 0, wx.RIGHT, 8)

        self.add_author_btn = wx.Button(panel, label="Add &Author URL...")
        self.add_author_btn.SetName("Add author URL watch")
        self.add_author_btn.Bind(
            wx.EVT_BUTTON,
            lambda e: self._open_url_dialog(WATCH_TYPE_AUTHOR),
        )
        add_row.Add(self.add_author_btn, 0, wx.RIGHT, 8)

        self.add_search_btn = wx.Button(panel, label="Add &Search...")
        self.add_search_btn.SetName("Add saved-search watch")
        self.add_search_btn.Bind(wx.EVT_BUTTON, self._on_add_search)
        add_row.Add(self.add_search_btn, 0)
        sizer.Add(add_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        action_row = wx.BoxSizer(wx.HORIZONTAL)
        self.remove_btn = wx.Button(panel, label="&Remove Selected")
        self.remove_btn.SetName("Remove selected watch")
        self.remove_btn.Bind(wx.EVT_BUTTON, self._on_remove)
        self.remove_btn.Disable()
        action_row.Add(self.remove_btn, 0, wx.RIGHT, 8)

        self.toggle_btn = wx.Button(panel, label="&Pause / Resume")
        self.toggle_btn.SetName("Pause or resume selected watch")
        self.toggle_btn.Bind(wx.EVT_BUTTON, self._on_toggle_enabled)
        self.toggle_btn.Disable()
        action_row.Add(self.toggle_btn, 0, wx.RIGHT, 16)

        self.run_all_btn = wx.Button(panel, label="Run &Now")
        self.run_all_btn.SetName("Poll all enabled watches now")
        self.run_all_btn.Bind(wx.EVT_BUTTON, lambda e: self._on_run_all())
        action_row.Add(self.run_all_btn, 0, wx.RIGHT, 8)

        self.run_selected_btn = wx.Button(panel, label="Run &Selected")
        self.run_selected_btn.SetName("Poll selected watch now")
        self.run_selected_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_run_selected(),
        )
        self.run_selected_btn.Disable()
        action_row.Add(self.run_selected_btn, 0, wx.RIGHT, 16)

        self.refresh_btn = wx.Button(panel, label="Re&fresh")
        self.refresh_btn.SetName("Refresh the watchlist from disk")
        self.refresh_btn.Bind(wx.EVT_BUTTON, lambda e: self._reload())
        action_row.Add(self.refresh_btn, 0, wx.RIGHT, 8)

        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        close_btn.SetName("Close watchlist")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        action_row.AddStretchSpacer(1)
        action_row.Add(close_btn, 0)

        sizer.Add(action_row, 0, wx.EXPAND | wx.ALL, pad)

        self.status_ctrl = wx.StaticText(panel, label="")
        self.status_ctrl.SetName("Watchlist status")
        sizer.Add(self.status_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad)

        panel.SetSizer(sizer)

    def _install_accelerators(self):
        self.Bind(wx.EVT_MENU, lambda e: self._on_remove(), id=_ID_REMOVE)
        self.Bind(wx.EVT_MENU, lambda e: self._on_run_all(), id=_ID_RUN_ALL)
        self.Bind(wx.EVT_MENU, lambda e: self._reload(), id=_ID_REFRESH)
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_DELETE, _ID_REMOVE),
            (wx.ACCEL_CTRL, ord("R"), _ID_RUN_ALL),
            (wx.ACCEL_NORMAL, wx.WXK_F5, _ID_REFRESH),
        ]))

    # ── State ──────────────────────────────────────────────

    def _reload(self):
        """Re-read the watchlist from disk and repaint the list."""
        try:
            self._store = WatchlistStore.load_default()
            self._watches = self._store.all()
        except Exception as exc:
            logger.exception("Failed to load watchlist")
            wx.MessageBox(
                f"Could not load the watchlist:\n\n{exc}",
                "Watchlist error",
                wx.OK | wx.ICON_ERROR, self,
            )
            self._watches = []
        self._repaint_list()
        self._update_status_summary()

    def _repaint_list(self):
        ctrl = self.list_ctrl
        ctrl.Freeze()
        try:
            ctrl.DeleteAllItems()
            for w in self._watches:
                row = ctrl.InsertItem(
                    ctrl.GetItemCount(), _TYPE_LABELS.get(w.type, w.type),
                )
                ctrl.SetItem(row, 1, _SITE_LABELS.get(w.site, w.site))
                ctrl.SetItem(row, 2, self._describe_target(w))
                ctrl.SetItem(row, 3, w.last_checked_at or "Never")
                ctrl.SetItem(row, 4, self._describe_status(w))
        finally:
            ctrl.Thaw()
        self._on_selection_change()

    @staticmethod
    def _describe_target(watch: Watch) -> str:
        if watch.type == WATCH_TYPE_SEARCH:
            label = watch.label or watch.query or watch.target
            return f"{label}" if label else "(saved search)"
        return watch.label or watch.target

    @staticmethod
    def _describe_status(watch: Watch) -> str:
        if not watch.enabled:
            return "Paused"
        if watch.last_error:
            return f"Error: {watch.last_error}"
        if not watch.last_checked_at:
            return "Pending first poll"
        return "OK"

    def _update_status_summary(self):
        total = len(self._watches)
        enabled = sum(1 for w in self._watches if w.enabled)
        self.status_ctrl.SetLabel(
            f"{total} watch(es), {enabled} active."
            if total else "Watchlist is empty — add one with the buttons above."
        )

    # ── Selection-driven UI state ──────────────────────────

    def _selected_index(self):
        idx = self.list_ctrl.GetFirstSelected()
        return idx if 0 <= idx < len(self._watches) else -1

    def _on_selection_change(self, event=None):
        has_selection = self._selected_index() >= 0
        self.remove_btn.Enable(has_selection)
        self.toggle_btn.Enable(has_selection)
        self.run_selected_btn.Enable(has_selection)
        if event is not None:
            event.Skip()

    # ── Add flows ──────────────────────────────────────────

    def _open_url_dialog(self, expected_type: str):
        dlg = AddURLWatchDialog(
            self, expected_type, channels=self._channels_choices(),
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            url = dlg.url_value()
            label = dlg.label_value()
            channels = dlg.channel_values()
            watch_type = classify_target(url)
            if watch_type is None or watch_type not in VALID_WATCH_TYPES:
                wx.MessageBox(
                    f"{url!r} is not a recognised story or author page on "
                    "any supported site.",
                    "Add failed", wx.OK | wx.ICON_ERROR, self,
                )
                return
            if watch_type != expected_type:
                mismatch = _TYPE_LABELS.get(watch_type, watch_type)
                wanted = _TYPE_LABELS.get(expected_type, expected_type)
                wx.MessageBox(
                    f"That URL looks like a {mismatch} page, not a "
                    f"{wanted} page. Use the Add {mismatch} URL button "
                    "instead — or confirm you have the right link.",
                    "URL type mismatch",
                    wx.OK | wx.ICON_WARNING, self,
                )
                return
            self._add_watch(Watch(
                type=watch_type,
                site=site_key_for_url(url),
                target=url,
                label=label,
                channels=channels,
            ))
        finally:
            dlg.Destroy()

    def _on_add_search(self, event):
        dlg = AddSearchWatchDialog(self, channels=self._channels_choices())
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            site = dlg.site_value()
            query = dlg.query_value()
            label = dlg.label_value()
            channels = dlg.channel_values()
            if site not in SEARCH_SUPPORTED_SITES:
                wx.MessageBox(
                    f"Search watches aren't supported on {site!r}.",
                    "Add failed", wx.OK | wx.ICON_ERROR, self,
                )
                return
            if not query:
                wx.MessageBox(
                    "Please enter a search query.",
                    "Add failed", wx.OK | wx.ICON_ERROR, self,
                )
                return
            self._add_watch(Watch(
                type=WATCH_TYPE_SEARCH,
                site=site,
                target=f"{site} search: {query}",
                label=label,
                channels=channels,
                query=query,
            ))
        finally:
            dlg.Destroy()

    def _channels_choices(self):
        """Returns the list of channel ids to offer in add dialogs.

        Defaulting to :data:`ALL_CHANNELS` keeps the UI simple — users
        disable a channel by leaving its credentials blank rather than
        maintaining a parallel "enabled channels" pref.
        """
        return list(ALL_CHANNELS)

    def _add_watch(self, watch: Watch):
        try:
            self._store.add(watch)
        except Exception as exc:
            logger.exception("Failed to add watch")
            wx.MessageBox(
                f"Could not add watch:\n\n{exc}",
                "Add failed", wx.OK | wx.ICON_ERROR, self,
            )
            return
        logger.info(
            "Added %s watch for %s", watch.type, watch.display_label(),
        )
        self._reload()

    # ── Remove / toggle ────────────────────────────────────

    def _on_remove(self, event=None):
        idx = self._selected_index()
        if idx < 0:
            return
        watch = self._watches[idx]
        dlg = wx.MessageDialog(
            self,
            f"Remove this watch?\n\n{watch.display_label()}",
            "Confirm removal",
            style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        if not self._store.remove(watch.id):
            wx.MessageBox(
                "That watch is no longer on disk — someone else may have "
                "removed it. Refreshing.",
                "Remove failed", wx.OK | wx.ICON_WARNING, self,
            )
        logger.info("Removed watch %s (%s)", watch.id, watch.display_label())
        self._reload()

    def _on_toggle_enabled(self, event=None):
        idx = self._selected_index()
        if idx < 0:
            return
        watch = self._watches[idx]
        watch.enabled = not watch.enabled
        try:
            self._store.update(watch)
        except Exception as exc:
            logger.exception("Failed to toggle watch enabled state")
            wx.MessageBox(
                f"Could not update watch:\n\n{exc}",
                "Update failed", wx.OK | wx.ICON_ERROR, self,
            )
            return
        logger.info(
            "%s watch %s (%s)",
            "Resumed" if watch.enabled else "Paused",
            watch.id[:8], watch.display_label(),
        )
        self._reload()

    # ── Run poll ───────────────────────────────────────────

    def _on_run_all(self):
        self._run_poll(watch_ids=None, label="all enabled watches")

    def _on_run_selected(self):
        idx = self._selected_index()
        if idx < 0:
            return
        watch = self._watches[idx]
        if not watch.enabled:
            wx.MessageBox(
                "That watch is paused. Resume it before running a poll.",
                "Run failed", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        self._run_poll(
            watch_ids={watch.id},
            label=watch.display_label(),
        )

    def _run_poll(self, *, watch_ids, label):
        """Fire run_once in a daemon thread so the GUI stays responsive.

        We disable the Run buttons while the thread is active so a user
        can't queue two concurrent polls on the same store — that would
        race the atomic-write guarantee and could lose an update to
        ``last_checked_at``.
        """
        self._set_poll_busy(True)
        self.status_ctrl.SetLabel(f"Polling {label}...")
        thread = threading.Thread(
            target=self._run_poll_worker,
            args=(watch_ids, label),
            name="ffn-dl-watchlist-manual-poll",
            daemon=True,
        )
        thread.start()

    def _run_poll_worker(self, watch_ids, label):
        try:
            results = run_once(
                self._store, self.prefs, watch_ids=watch_ids,
            )
        except Exception as exc:
            logger.exception("Manual watchlist poll failed")
            wx.CallAfter(self._on_poll_done, [], str(exc), label)
            return
        wx.CallAfter(self._on_poll_done, results, "", label)

    def _on_poll_done(self, results, fatal_error, label):
        self._set_poll_busy(False)
        self._reload()
        if fatal_error:
            self.status_ctrl.SetLabel(f"Poll failed: {fatal_error}")
            return
        total_new = sum(len(r.new_items) for r in results if r.ok)
        errors = sum(1 for r in results if not r.ok)
        if total_new:
            summary = (
                f"Poll complete ({label}): {total_new} new item(s) "
                f"across {len(results)} watch(es)."
            )
        elif errors:
            summary = (
                f"Poll complete ({label}): no new items, "
                f"{errors} error(s)."
            )
        else:
            summary = f"Poll complete ({label}): no changes."
        self.status_ctrl.SetLabel(summary)

    def _set_poll_busy(self, busy):
        # Freeze add/remove/run buttons while the worker thread is live.
        # The list itself stays responsive so users can still read what
        # they've got.
        for btn in (
            self.add_story_btn, self.add_author_btn, self.add_search_btn,
            self.remove_btn, self.toggle_btn, self.run_all_btn,
            self.run_selected_btn, self.refresh_btn,
        ):
            btn.Enable(not busy)
        if not busy:
            self._on_selection_change()

    # ── Close ──────────────────────────────────────────────

    def _on_close(self, event):
        try:
            self.main_frame._notify_watchlist_frame_closed()
        except Exception:
            pass
        event.Skip()


# ──────────────────────────────────────────────────────────────────
# Add dialogs
# ──────────────────────────────────────────────────────────────────


class _ChannelCheckGroup(wx.Panel):
    """Check-list of notification channels for the add dialogs.

    Wraps a plain CheckListBox so the dialogs can ``SetName`` a single
    semantic label and grab the picked values in one call.
    """

    def __init__(self, parent, channels):
        super().__init__(parent)
        self._channels = list(channels)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list_ctrl = wx.CheckListBox(self, choices=self._channels)
        self.list_ctrl.SetName(
            "Notification channels to enable on this watch"
        )
        # Start with every channel ticked — matches the CLI default,
        # which only treats unticked channels as "disable this one".
        self.list_ctrl.SetCheckedItems(range(len(self._channels)))
        sizer.Add(self.list_ctrl, 1, wx.EXPAND)
        self.SetSizer(sizer)

    def picked(self):
        return [
            self._channels[i] for i in self.list_ctrl.GetCheckedItems()
        ]


class AddURLWatchDialog(wx.Dialog):
    """Collects a URL + optional label + channel selection.

    One dialog handles story and author adds; the caller tells the
    dialog which type it expects so the title and hint text can be
    accurate. The URL is classified (and re-checked against the
    expected type) back in :class:`WatchlistFrame`, not here.
    """

    def __init__(self, parent, expected_type, *, channels):
        title = (
            "Add story URL watch"
            if expected_type == WATCH_TYPE_STORY
            else "Add author URL watch"
        )
        super().__init__(
            parent, title=title,
            size=(560, 360),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._expected_type = expected_type

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        hint_text = (
            "Paste the story URL from any supported site (FFN, AO3, "
            "FicWad, Royal Road, MediaMiner, Literotica, Wattpad)."
            if expected_type == WATCH_TYPE_STORY
            else "Paste the author/profile page URL from any supported "
            "site. New works appearing under that author will trigger "
            "a notification."
        )
        hint = wx.StaticText(panel, label=hint_text)
        hint.Wrap(520)
        sizer.Add(hint, 0, wx.ALL, pad)

        url_row = wx.BoxSizer(wx.HORIZONTAL)
        url_row.Add(
            wx.StaticText(panel, label="&URL:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.url_ctrl = wx.TextCtrl(panel)
        self.url_ctrl.SetName("URL")
        url_row.Add(self.url_ctrl, 1)
        sizer.Add(url_row, 0, wx.EXPAND | wx.ALL, pad)

        label_row = wx.BoxSizer(wx.HORIZONTAL)
        label_row.Add(
            wx.StaticText(panel, label="Display &label (optional):"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.label_ctrl = wx.TextCtrl(panel)
        self.label_ctrl.SetName("Display label (optional)")
        label_row.Add(self.label_ctrl, 1)
        sizer.Add(label_row, 0, wx.EXPAND | wx.ALL, pad)

        sizer.Add(
            wx.StaticText(panel, label="&Channels:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, pad,
        )
        self.channel_group = _ChannelCheckGroup(panel, channels)
        sizer.Add(self.channel_group, 1, wx.EXPAND | wx.ALL, pad)

        btn_row = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, "&Add")
        ok_btn.SetDefault()
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        btn_row.AddButton(ok_btn)
        btn_row.AddButton(cancel_btn)
        btn_row.Realize()
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, pad)

        panel.SetSizer(sizer)
        self.url_ctrl.SetFocus()

    def url_value(self):
        return self.url_ctrl.GetValue().strip()

    def label_value(self):
        return self.label_ctrl.GetValue().strip()

    def channel_values(self):
        return self.channel_group.picked()


class AddSearchWatchDialog(wx.Dialog):
    """Collects a site + query + optional label + channel selection."""

    def __init__(self, parent, *, channels):
        super().__init__(
            parent, title="Add saved-search watch",
            size=(560, 400),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        hint = wx.StaticText(
            panel,
            label=(
                "Save a site-specific search as a watch. New matches for "
                "the query show up as notifications. Supported sites: "
                + ", ".join(_SITE_LABELS.get(s, s) for s in SEARCH_SUPPORTED_SITES)
                + "."
            ),
        )
        hint.Wrap(520)
        sizer.Add(hint, 0, wx.ALL, pad)

        site_row = wx.BoxSizer(wx.HORIZONTAL)
        site_row.Add(
            wx.StaticText(panel, label="&Site:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self._site_keys = list(SEARCH_SUPPORTED_SITES)
        self.site_ctrl = wx.Choice(
            panel,
            choices=[_SITE_LABELS.get(s, s) for s in self._site_keys],
        )
        self.site_ctrl.SetName("Search site")
        self.site_ctrl.SetSelection(0)
        site_row.Add(self.site_ctrl, 0)
        sizer.Add(site_row, 0, wx.EXPAND | wx.ALL, pad)

        query_row = wx.BoxSizer(wx.HORIZONTAL)
        query_row.Add(
            wx.StaticText(panel, label="&Query:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.query_ctrl = wx.TextCtrl(panel)
        self.query_ctrl.SetName("Search query")
        query_row.Add(self.query_ctrl, 1)
        sizer.Add(query_row, 0, wx.EXPAND | wx.ALL, pad)

        label_row = wx.BoxSizer(wx.HORIZONTAL)
        label_row.Add(
            wx.StaticText(panel, label="Display &label (optional):"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.label_ctrl = wx.TextCtrl(panel)
        self.label_ctrl.SetName("Display label (optional)")
        label_row.Add(self.label_ctrl, 1)
        sizer.Add(label_row, 0, wx.EXPAND | wx.ALL, pad)

        sizer.Add(
            wx.StaticText(panel, label="&Channels:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, pad,
        )
        self.channel_group = _ChannelCheckGroup(panel, channels)
        sizer.Add(self.channel_group, 1, wx.EXPAND | wx.ALL, pad)

        btn_row = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, "&Add")
        ok_btn.SetDefault()
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        btn_row.AddButton(ok_btn)
        btn_row.AddButton(cancel_btn)
        btn_row.Realize()
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, pad)

        panel.SetSizer(sizer)
        self.query_ctrl.SetFocus()

    def site_value(self):
        idx = self.site_ctrl.GetSelection()
        if 0 <= idx < len(self._site_keys):
            return self._site_keys[idx]
        return ""

    def query_value(self):
        return self.query_ctrl.GetValue().strip()

    def label_value(self):
        return self.label_ctrl.GetValue().strip()

    def channel_values(self):
        return self.channel_group.picked()
