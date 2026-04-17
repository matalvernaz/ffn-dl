"""Accessible wxPython GUI for ffn-dl.

Uses native Win32 controls via wxPython so NVDA, JAWS, and other
screen readers can read every widget natively.
"""

import json
import re
import sys
import threading
import wx
import webbrowser
from collections import deque
from pathlib import Path


_LOG_FLUSH_INTERVAL_MS = 100
_LOG_MAX_LINES = 5000
_LOG_TRIM_TO_LINES = 4000


_FFN_URL_RE = re.compile(
    r"https?://(?:www\.)?("
    r"fanfiction\.net/s/\d+"
    r"|ficwad\.com/story/\d+"
    r"|(?:archiveofourown\.org|ao3\.org)/works/\d+"
    r"|royalroad\.com/fiction/\d+"
    r"|mediaminer\.org/fanfic/(?:view_st\.php/\d+|s/[^?#\s]+?/\d+)"
    r"|literotica\.com/s/[a-z0-9-]+"
    r")"
)

_SEARCH_COLUMNS = [
    ("Title", 260),
    ("Author", 120),
    ("Fandom", 160),
    ("Words", 70),
    ("Ch", 40),
    ("Rating", 80),
    ("Status", 90),
]


def _ffn_search_spec():
    from .search import (
        FFN_CROSSOVER, FFN_GENRE, FFN_LANGUAGE, FFN_MATCH,
        FFN_RATING, FFN_SORT, FFN_STATUS, FFN_WORDS, search_ffn,
    )
    return {
        "label": "Search FFN",
        "search_fn": search_ffn,
        "filters": [
            ("&Rating:", "rating", list(FFN_RATING)),
            ("&Language:", "language", list(FFN_LANGUAGE)),
            ("S&tatus:", "status", list(FFN_STATUS)),
            ("&Genre:", "genre", list(FFN_GENRE)),
            ("Genre &2:", "genre2", list(FFN_GENRE)),
            ("&Words:", "min_words", list(FFN_WORDS)),
            ("&Crossover:", "crossover", list(FFN_CROSSOVER)),
            ("&Match in:", "match", list(FFN_MATCH)),
            ("Sor&t by:", "sort", list(FFN_SORT)),
        ],
    }


def _ao3_search_spec():
    from .search import (
        AO3_CATEGORY, AO3_COMPLETE, AO3_CROSSOVER, AO3_LANGUAGES,
        AO3_RATING, AO3_SORT, search_ao3,
    )
    return {
        "label": "Search AO3",
        "search_fn": search_ao3,
        "filters": [
            ("&Rating:", "rating", list(AO3_RATING)),
            ("Cate&gory:", "category", list(AO3_CATEGORY)),
            ("S&tatus:", "complete", list(AO3_COMPLETE)),
            ("&Crossover:", "crossover", list(AO3_CROSSOVER)),
            ("Lan&guage:", "language", list(AO3_LANGUAGES)),
            ("Sor&t by:", "sort", list(AO3_SORT)),
        ],
        "text_filters": [
            ("&Fandom:", "fandom"),
            ("&Character:", "character"),
            ("&Relationship:", "relationship"),
            ("Free&form tag:", "freeform"),
            ("&Word count:", "word_count"),
        ],
        "checkboxes": [
            ("&Single-chapter only", "single_chapter"),
        ],
    }


def _royalroad_search_spec():
    from .search import (
        RR_GENRES, RR_LISTS, RR_ORDER_BY, RR_STATUS, RR_TAGS, RR_TYPE,
        RR_WARNINGS, search_royalroad,
    )
    return {
        "label": "Search Royal Road",
        "search_fn": search_royalroad,
        "filters": [
            ("&Browse:", "list", list(RR_LISTS)),
            ("S&tatus:", "status", list(RR_STATUS)),
            ("&Type:", "type", list(RR_TYPE)),
            ("Sor&t by:", "order_by", list(RR_ORDER_BY)),
        ],
        "multi_pickers": [
            ("&Genres:", "genres", "Pick Royal Road genres", list(RR_GENRES)),
            ("Ta&gs:", "tags_picked", "Pick Royal Road tags", list(RR_TAGS)),
            (
                "War&nings:", "warnings",
                "Pick content warnings to require", list(RR_WARNINGS),
            ),
        ],
        "text_filters": [
            ("Min &words:", "min_words"),
            ("Ma&x words:", "max_words"),
            ("Min &pages:", "min_pages"),
            ("Min &rating:", "min_rating"),
        ],
    }


def _literotica_search_spec():
    from .search import LIT_CATEGORIES, search_literotica
    return {
        "label": "Search Literotica",
        "search_fn": search_literotica,
        "filters": [
            ("Categor&y:", "category", list(LIT_CATEGORIES)),
        ],
        "text_filters": [
            ("&Page:", "page"),
        ],
    }


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="ffn-dl - Fanfiction Downloader",
            size=(820, 720),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        from .prefs import Prefs
        self.prefs = Prefs()
        self._downloading = False
        self._watching = False
        self._watch_seen = set()
        self._last_clip = ""
        self._tabs = {}  # site_key → {query_ctrl, results_ctrl, summary_ctrl, search_dl_btn, search_btn, filter_ctrls, text_ctrls, checkbox_ctrls, search_fn, results}
        self._log_queue = deque()
        self._log_lock = threading.Lock()
        self._build_ui()
        self._load_prefs()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()
        self._start_update_check()

    def _build_ui(self):
        root = wx.Panel(self)
        root_sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # ── Notebook with tabs ───────────────────────────────
        self.notebook = wx.Notebook(root)
        self.notebook.SetName("Mode tabs")

        self._build_download_tab(self.notebook)
        self._build_search_tab(self.notebook, "ffn", _ffn_search_spec())
        self._build_search_tab(self.notebook, "ao3", _ao3_search_spec())
        self._build_search_tab(self.notebook, "royalroad", _royalroad_search_spec())
        self._build_search_tab(self.notebook, "literotica", _literotica_search_spec())

        root_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, pad)

        # ── Shared options (format / filename / output folder) ─
        opts = wx.BoxSizer(wx.HORIZONTAL)

        opts.Add(wx.StaticText(root, label="&Format:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.format_ctrl = wx.Choice(root, choices=["epub", "html", "txt", "audio"])
        self.format_ctrl.SetSelection(0)
        self.format_ctrl.SetName("Format")
        opts.Add(self.format_ctrl, 0, wx.RIGHT, 16)

        opts.Add(wx.StaticText(root, label="File&name template:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.name_ctrl = wx.TextCtrl(root, value="{title} - {author}", size=(200, -1))
        self.name_ctrl.SetName("Filename template")
        opts.Add(self.name_ctrl, 1)

        root_sizer.Add(opts, 0, wx.EXPAND | wx.ALL, pad)

        # Extra export options row
        opts2 = wx.BoxSizer(wx.HORIZONTAL)
        self.hr_stars_ctrl = wx.CheckBox(
            root, label="Render scene breaks as &* * *  (instead of a thin rule)"
        )
        self.hr_stars_ctrl.SetName("Render scene breaks as asterisks")
        opts2.Add(self.hr_stars_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        self.strip_notes_ctrl = wx.CheckBox(
            root, label="Strip &author's notes (A/N paragraphs)"
        )
        self.strip_notes_ctrl.SetName("Strip author's notes")
        opts2.Add(self.strip_notes_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        root_sizer.Add(opts2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        out_sizer = wx.BoxSizer(wx.HORIZONTAL)
        out_sizer.Add(wx.StaticText(root, label="&Save to:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        default_dir = str(Path.home() / "Downloads")
        self.output_ctrl = wx.TextCtrl(root, value=default_dir)
        self.output_ctrl.SetName("Save to folder")
        out_sizer.Add(self.output_ctrl, 1, wx.RIGHT, 4)

        browse_btn = wx.Button(root, label="&Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        out_sizer.Add(browse_btn, 0)

        root_sizer.Add(out_sizer, 0, wx.EXPAND | wx.ALL, pad)

        # ── Status log ───────────────────────────────────────
        root_sizer.Add(wx.StaticText(root, label="S&tatus:"), 0, wx.LEFT | wx.TOP, pad)
        self.log_ctrl = wx.TextCtrl(
            root,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self.log_ctrl.SetName("Status log")
        root_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        root.SetSizer(root_sizer)

        self._log_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_log_flush, self._log_timer)
        self._log_timer.Start(_LOG_FLUSH_INTERVAL_MS)

        # Accelerators
        accel = wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord("D"), self.dl_btn.GetId()),
            (wx.ACCEL_CTRL, ord("U"), self.update_btn.GetId()),
            (wx.ACCEL_CTRL, ord("W"), self.watch_btn.GetId()),
        ])
        self.SetAcceleratorTable(accel)

        # Timer for clipboard polling (2 second interval)
        self._clip_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_clip_timer, self._clip_timer)

    def _build_download_tab(self, notebook):
        panel = wx.Panel(notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        sizer.Add(wx.StaticText(panel, label="Story &URL or ID:"), 0, wx.LEFT | wx.TOP, pad)
        self.url_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.url_ctrl.SetName("Story URL or ID")
        self.url_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_download)
        sizer.Add(self.url_ctrl, 0, wx.EXPAND | wx.ALL, pad)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.dl_btn = wx.Button(panel, label="&Download")
        self.dl_btn.SetDefault()
        self.dl_btn.Bind(wx.EVT_BUTTON, self._on_download)
        btn_sizer.Add(self.dl_btn, 0, wx.RIGHT, 8)

        self.update_btn = wx.Button(panel, label="U&pdate Existing File...")
        self.update_btn.Bind(wx.EVT_BUTTON, self._on_update)
        btn_sizer.Add(self.update_btn, 0, wx.RIGHT, 8)

        self.watch_btn = wx.ToggleButton(panel, label="&Watch Clipboard")
        self.watch_btn.SetName("Watch Clipboard toggle")
        self.watch_btn.Bind(wx.EVT_TOGGLEBUTTON, self._on_watch_toggle)
        btn_sizer.Add(self.watch_btn, 0, wx.RIGHT, 8)

        self.voices_btn = wx.Button(panel, label="Preview &Voices...")
        self.voices_btn.SetName("Preview character voices")
        self.voices_btn.Bind(wx.EVT_BUTTON, self._on_preview_voices)
        btn_sizer.Add(self.voices_btn, 0)

        sizer.Add(btn_sizer, 0, wx.ALL, pad)
        sizer.AddStretchSpacer(1)

        panel.SetSizer(sizer)
        notebook.AddPage(panel, "Download")

    def _build_search_tab(self, notebook, site_key, spec):
        panel = wx.Panel(notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6
        state = {
            "search_fn": spec["search_fn"],
            "filter_ctrls": {},
            "text_ctrls": {},
            "checkbox_ctrls": {},
            "results": [],
            "site_key": site_key,
            "next_page": 1,
            "last_query": None,
            "last_filters": {},
        }

        # Query row
        q_row = wx.BoxSizer(wx.HORIZONTAL)
        q_row.Add(wx.StaticText(panel, label="&Query:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        state["query_ctrl"] = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        state["query_ctrl"].SetName(f"{spec['label']} query")
        state["query_ctrl"].Bind(
            wx.EVT_TEXT_ENTER, lambda evt, k=site_key: self._on_search(k)
        )
        q_row.Add(state["query_ctrl"], 1, wx.RIGHT, 4)

        state["search_btn"] = wx.Button(panel, label="S&earch")
        state["search_btn"].Bind(
            wx.EVT_BUTTON, lambda evt, k=site_key: self._on_search(k)
        )
        q_row.Add(state["search_btn"], 0)
        sizer.Add(q_row, 0, wx.EXPAND | wx.ALL, pad)

        # Choice filters (combo boxes)
        if spec.get("filters"):
            fgrid = wx.FlexGridSizer(rows=0, cols=8, hgap=4, vgap=4)
            for label, key, choices in spec["filters"]:
                fgrid.Add(
                    wx.StaticText(panel, label=label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.Choice(panel, choices=choices)
                ctrl.SetSelection(0)
                ctrl.SetName(label.replace("&", "").rstrip(":"))
                fgrid.Add(ctrl, 0, wx.RIGHT, 12)
                state["filter_ctrls"][key] = ctrl
            sizer.Add(fgrid, 0, wx.EXPAND | wx.ALL, pad)

        # Free-text filters
        if spec.get("text_filters"):
            tgrid = wx.FlexGridSizer(rows=0, cols=4, hgap=4, vgap=4)
            for label, key in spec["text_filters"]:
                tgrid.Add(
                    wx.StaticText(panel, label=label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.TextCtrl(panel, size=(140, -1))
                ctrl.SetName(label.replace("&", "").rstrip(":"))
                tgrid.Add(ctrl, 0, wx.RIGHT, 12)
                state["text_ctrls"][key] = ctrl
            sizer.Add(tgrid, 0, wx.EXPAND | wx.ALL, pad)

        # Multi-pickers (checkable list dialogs for tags/genres/warnings)
        # — each entry contributes a read-only TextCtrl showing the
        # current picks plus a "Pick..." button that opens MultiPickerDialog.
        # The TextCtrl is registered in `text_ctrls` so existing collect
        # / snapshot / restore logic handles persistence unchanged.
        if spec.get("multi_pickers"):
            for mp_label, mp_key, mp_title, mp_options in spec["multi_pickers"]:
                row = wx.BoxSizer(wx.HORIZONTAL)
                row.Add(
                    wx.StaticText(panel, label=mp_label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.TextCtrl(panel, size=(320, -1))
                ctrl.SetName(mp_label.replace("&", "").rstrip(":"))
                row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
                btn = wx.Button(panel, label="Pic&k...")
                btn.Bind(
                    wx.EVT_BUTTON,
                    lambda evt, c=ctrl, t=mp_title, o=mp_options:
                        self._open_multi_picker(c, t, o),
                )
                row.Add(btn, 0)
                sizer.Add(row, 0, wx.EXPAND | wx.ALL, pad)
                state["text_ctrls"][mp_key] = ctrl

        # Checkboxes
        if spec.get("checkboxes"):
            cb_row = wx.BoxSizer(wx.HORIZONTAL)
            for label, key in spec["checkboxes"]:
                ctrl = wx.CheckBox(panel, label=label)
                cb_row.Add(ctrl, 0, wx.RIGHT, 16)
                state["checkbox_ctrls"][key] = ctrl
            sizer.Add(cb_row, 0, wx.EXPAND | wx.ALL, pad)

        # Results list
        sizer.Add(wx.StaticText(panel, label="&Results:"), 0, wx.LEFT | wx.TOP, pad)
        state["results_ctrl"] = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        state["results_ctrl"].SetName(f"{spec['label']} results")
        for i, (col_label, width) in enumerate(_SEARCH_COLUMNS):
            state["results_ctrl"].InsertColumn(i, col_label, width=width)
        state["results_ctrl"].Bind(
            wx.EVT_LIST_ITEM_SELECTED,
            lambda evt, k=site_key: self._on_result_select(evt, k),
        )
        state["results_ctrl"].Bind(
            wx.EVT_LIST_ITEM_ACTIVATED,
            lambda evt, k=site_key: self._on_result_activated(k),
        )
        sizer.Add(state["results_ctrl"], 1, wx.EXPAND | wx.ALL, pad)

        # Summary
        sizer.Add(wx.StaticText(panel, label="S&ummary:"), 0, wx.LEFT | wx.TOP, pad)
        state["summary_ctrl"] = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 70),
        )
        state["summary_ctrl"].SetName(f"{spec['label']} summary")
        sizer.Add(state["summary_ctrl"], 0, wx.EXPAND | wx.ALL, pad)

        dl_row = wx.BoxSizer(wx.HORIZONTAL)
        state["search_dl_btn"] = wx.Button(panel, label="Do&wnload Selected")
        state["search_dl_btn"].Bind(
            wx.EVT_BUTTON, lambda evt, k=site_key: self._on_search_download(k)
        )
        state["search_dl_btn"].Disable()
        dl_row.Add(state["search_dl_btn"], 0, wx.RIGHT, 8)

        state["show_parts_btn"] = wx.Button(panel, label="Show &Parts...")
        state["show_parts_btn"].Bind(
            wx.EVT_BUTTON, lambda evt, k=site_key: self._on_show_parts(k)
        )
        state["show_parts_btn"].Disable()
        dl_row.Add(state["show_parts_btn"], 0, wx.RIGHT, 8)

        state["load_more_btn"] = wx.Button(panel, label="Load &More")
        state["load_more_btn"].Bind(
            wx.EVT_BUTTON, lambda evt, k=site_key: self._on_load_more(k)
        )
        state["load_more_btn"].Disable()
        dl_row.Add(state["load_more_btn"], 0)
        sizer.Add(dl_row, 0, wx.ALL, pad)

        panel.SetSizer(sizer)
        notebook.AddPage(panel, spec["label"])
        self._tabs[site_key] = state

    # ── Helpers ───────────────────────────────────────────────

    def _log(self, msg):
        with self._log_lock:
            self._log_queue.append(msg + "\n")

    def _on_log_flush(self, event):
        if not self._log_queue:
            return
        with self._log_lock:
            chunk = "".join(self._log_queue)
            self._log_queue.clear()
        if not chunk:
            return
        self.log_ctrl.AppendText(chunk)
        line_count = self.log_ctrl.GetNumberOfLines()
        if line_count > _LOG_MAX_LINES:
            cut_line = line_count - _LOG_TRIM_TO_LINES
            cut_pos = self.log_ctrl.XYToPosition(0, cut_line)
            if cut_pos > 0:
                self.log_ctrl.Remove(0, cut_pos)

    def _set_busy(self, busy):
        def _update():
            self._downloading = busy
            self.dl_btn.Enable(not busy)
            self.update_btn.Enable(not busy)
            self.voices_btn.Enable(not busy)
            for tab in self._tabs.values():
                tab["search_btn"].Enable(not busy)
                has_selection = tab["results_ctrl"].GetFirstSelected() != -1
                selected_is_series = False
                if has_selection:
                    idx = tab["results_ctrl"].GetFirstSelected()
                    if 0 <= idx < len(tab["results"]):
                        selected_is_series = bool(
                            tab["results"][idx].get("is_series")
                        )
                tab["search_dl_btn"].Enable(not busy and has_selection)
                tab["show_parts_btn"].Enable(
                    not busy and has_selection and selected_is_series
                )
                tab["load_more_btn"].Enable(
                    not busy and tab.get("last_query") is not None
                )
        wx.CallAfter(_update)

    def _on_browse(self, event):
        dlg = wx.DirDialog(
            self, "Choose output folder",
            defaultPath=self.output_ctrl.GetValue(),
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.output_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    # ── Prefs ────────────────────────────────────────────────

    def _load_prefs(self):
        from . import prefs as _p

        tmpl = self.prefs.get(_p.KEY_NAME_TEMPLATE)
        if tmpl:
            self.name_ctrl.SetValue(tmpl)

        fmt = self.prefs.get(_p.KEY_FORMAT)
        if fmt:
            formats = [
                self.format_ctrl.GetString(i)
                for i in range(self.format_ctrl.GetCount())
            ]
            if fmt in formats:
                self.format_ctrl.SetSelection(formats.index(fmt))

        out = self.prefs.get(_p.KEY_OUTPUT_DIR)
        if out:
            self.output_ctrl.SetValue(out)

        self.hr_stars_ctrl.SetValue(self.prefs.get_bool(_p.KEY_HR_AS_STARS))
        self.strip_notes_ctrl.SetValue(self.prefs.get_bool(_p.KEY_STRIP_NOTES))

        for site_key, pref_key in (
            ("ffn", _p.KEY_SEARCH_STATE_FFN),
            ("ao3", _p.KEY_SEARCH_STATE_AO3),
            ("royalroad", _p.KEY_SEARCH_STATE_ROYALROAD),
            ("literotica", _p.KEY_SEARCH_STATE_LITEROTICA),
        ):
            if site_key not in self._tabs:
                continue
            raw = self.prefs.get(pref_key)
            if not raw:
                continue
            try:
                state = json.loads(raw)
            except (TypeError, ValueError):
                continue
            self._apply_search_state(self._tabs[site_key], state)

    def _save_prefs(self):
        from . import prefs as _p

        self.prefs.set(_p.KEY_NAME_TEMPLATE, self.name_ctrl.GetValue())
        self.prefs.set(
            _p.KEY_FORMAT,
            self.format_ctrl.GetString(self.format_ctrl.GetSelection()),
        )
        self.prefs.set(_p.KEY_OUTPUT_DIR, self.output_ctrl.GetValue())
        self.prefs.set_bool(_p.KEY_HR_AS_STARS, self.hr_stars_ctrl.GetValue())
        self.prefs.set_bool(_p.KEY_STRIP_NOTES, self.strip_notes_ctrl.GetValue())

        for site_key, pref_key in (
            ("ffn", _p.KEY_SEARCH_STATE_FFN),
            ("ao3", _p.KEY_SEARCH_STATE_AO3),
            ("royalroad", _p.KEY_SEARCH_STATE_ROYALROAD),
            ("literotica", _p.KEY_SEARCH_STATE_LITEROTICA),
        ):
            if site_key not in self._tabs:
                continue
            state = self._snapshot_search_state(self._tabs[site_key])
            self.prefs.set(pref_key, json.dumps(state))

    @staticmethod
    def _snapshot_search_state(tab):
        # `query` is intentionally NOT persisted — a stored search
        # string reappearing across sessions is more annoying than
        # useful. Filters, text filters, and checkboxes DO persist
        # because re-setting language / sort / genre / tag picks on
        # every launch is painful.
        return {
            "filters": {
                key: ctrl.GetStringSelection()
                for key, ctrl in tab["filter_ctrls"].items()
            },
            "text": {
                key: ctrl.GetValue()
                for key, ctrl in tab["text_ctrls"].items()
            },
            "checks": {
                key: bool(ctrl.GetValue())
                for key, ctrl in tab["checkbox_ctrls"].items()
            },
        }

    @staticmethod
    def _apply_search_state(tab, state):
        if not isinstance(state, dict):
            return
        # Ignore any legacy "query" that older versions wrote — the
        # field is no longer part of the persisted schema.
        for key, value in (state.get("filters") or {}).items():
            ctrl = tab["filter_ctrls"].get(key)
            if ctrl and isinstance(value, str) and value:
                ctrl.SetStringSelection(value)
        for key, value in (state.get("text") or {}).items():
            ctrl = tab["text_ctrls"].get(key)
            if ctrl and isinstance(value, str):
                ctrl.SetValue(value)
        for key, value in (state.get("checks") or {}).items():
            ctrl = tab["checkbox_ctrls"].get(key)
            if ctrl is not None:
                ctrl.SetValue(bool(value))

    def _on_close(self, event):
        try:
            self._save_prefs()
        except Exception:
            pass
        if hasattr(self, "_log_timer"):
            self._log_timer.Stop()
        event.Skip()

    # ── Update check ─────────────────────────────────────────

    def _start_update_check(self):
        from . import prefs as _p, self_update

        # Clean up any leftover .exe.old from a previous update
        self_update.cleanup_old_exe()

        if not self.prefs.get_bool(_p.KEY_CHECK_UPDATES):
            return

        threading.Thread(target=self._run_update_check, daemon=True).start()

    def _run_update_check(self):
        from . import prefs as _p, self_update

        try:
            info = self_update.check_for_update()
        except Exception as exc:
            # Silent — a failed check shouldn't bug the user
            wx.CallAfter(self._log, f"(Update check failed: {exc})")
            return
        if info is None:
            return

        skipped = self.prefs.get(_p.KEY_SKIPPED_VERSION)
        if skipped and skipped == info["tag"]:
            return

        wx.CallAfter(self._prompt_update, info)

    def _prompt_update(self, info):
        from . import __version__
        from . import prefs as _p, self_update

        tag = info["tag"]
        size_mb = (info.get("size") or 0) / 1024 / 1024

        if not self_update.can_self_replace():
            # Linux/Mac/dev install: offer to open the release page
            msg = (
                f"Version {tag} is available (you have {__version__}).\n\n"
                f"Automatic update is only supported in the Windows build. "
                f"Open the release page to update manually?"
            )
            dlg = wx.MessageDialog(
                self, msg, "Update Available",
                style=wx.YES_NO | wx.CANCEL | wx.YES_DEFAULT,
            )
            dlg.SetYesNoCancelLabels(
                "&Open Release Page", "Re&mind Me Later", "&Skip This Version",
            )
            result = dlg.ShowModal()
            dlg.Destroy()
            if result == wx.ID_YES and info.get("release_url"):
                webbrowser.open(info["release_url"])
            elif result == wx.ID_CANCEL:
                self.prefs.set(_p.KEY_SKIPPED_VERSION, tag)
            return

        msg = (
            f"Version {tag} is available. You currently have {__version__}.\n\n"
            f"What will happen if you update:\n"
            f"  \u2022 ffn-dl will download the new version (about "
            f"{size_mb:.0f} MB).\n"
            f"  \u2022 The app will close, replace itself, and reopen "
            f"automatically.\n"
            f"  \u2022 Your settings, cached chapters, and saved files are "
            f"untouched.\n"
            f"  \u2022 If the download fails or is cancelled, the current "
            f"version keeps running \u2014 nothing is changed until the "
            f"new file is fully downloaded.\n\n"
            f"Update now?"
        )
        dlg = wx.MessageDialog(
            self, msg, "Update Available",
            style=wx.YES_NO | wx.CANCEL | wx.YES_DEFAULT,
        )
        dlg.SetYesNoCancelLabels(
            "&Update Now", "Re&mind Me Later", "&Skip This Version",
        )
        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_YES:
            self._perform_update(info)
        elif result == wx.ID_CANCEL:
            self.prefs.set(_p.KEY_SKIPPED_VERSION, tag)

    def _perform_update(self, info):
        import time

        from . import self_update

        # Save prefs now so they're on disk before the swap
        try:
            self._save_prefs()
        except Exception:
            pass

        progress = wx.ProgressDialog(
            "Downloading update",
            f"Downloading {info['tag']}...",
            maximum=100,
            parent=self,
            style=(
                wx.PD_APP_MODAL | wx.PD_CAN_ABORT
                | wx.PD_ELAPSED_TIME | wx.PD_REMAINING_TIME
            ),
        )
        cancel_event = threading.Event()
        # progress_cb runs on the worker thread, but wxPython widgets are
        # not thread-safe — calling progress.Update() directly from the
        # worker deadlocks the main event loop (freeze). Marshal display
        # updates through wx.CallAfter and read the cancel state via a
        # threading.Event that the main thread sets when the user clicks
        # Abort. Throttle to ~10 Hz so we don't flood the main thread.
        last_call = [0.0]

        def _apply_update(done, total):
            if cancel_event.is_set():
                return
            if total <= 0:
                return
            pct = min(100, int(done * 100 / total))
            done_mb = done / 1024 / 1024
            total_mb = total / 1024 / 1024
            kept_going, _ = progress.Update(
                pct, f"Downloaded {done_mb:.0f} / {total_mb:.0f} MB"
            )
            if not kept_going:
                cancel_event.set()

        def progress_cb(done, total):
            if cancel_event.is_set():
                raise RuntimeError("Update cancelled by user.")
            now = time.monotonic()
            # Always push the final update; throttle intermediate ones
            if done < total and now - last_call[0] < 0.1:
                return
            last_call[0] = now
            wx.CallAfter(_apply_update, done, total)

        def worker():
            try:
                self_update.download_and_replace(info, progress_cb=progress_cb)
            except Exception as exc:
                wx.CallAfter(self._update_failed, progress, exc)
                return
            wx.CallAfter(self._update_succeeded, progress, info["tag"])

        threading.Thread(target=worker, daemon=True).start()

    def _update_failed(self, progress, exc):
        progress.Destroy()
        wx.MessageBox(
            f"Update failed: {exc}\n\nYour current version is unchanged.",
            "Update Error",
            wx.OK | wx.ICON_ERROR,
            parent=self,
        )

    def _update_succeeded(self, progress, tag):
        from . import self_update
        progress.Destroy()
        wx.MessageBox(
            f"Updated to {tag}. The app will now restart.",
            "Update Complete",
            wx.OK,
            parent=self,
        )
        # Prefs snapshot at _perform_update is stale by now — the user
        # may have toggled filters or edited fields while the download
        # ran. Save again so the post-restart app sees the latest state.
        try:
            self._save_prefs()
        except Exception:
            pass
        # Force wx.Config's in-memory buffer to disk before we spawn
        # the new process. Without this, the child can open wx.Config
        # before the parent has flushed, reading stale values.
        try:
            self.prefs.flush()
        except Exception:
            pass
        # Stop log/clip timers and hide the frame so the new process
        # doesn't see a second visible window during its early startup.
        try:
            if hasattr(self, "_log_timer"):
                self._log_timer.Stop()
            if hasattr(self, "_clip_timer"):
                self._clip_timer.Stop()
            self.Hide()
        except Exception:
            pass
        self_update.restart()

    # ── Download ─────────────────────────────────────────────

    def _on_download(self, event):
        url = self.url_ctrl.GetValue().strip()
        if not url:
            self._log("Error: Please enter a story URL or ID.")
            return
        if self._downloading:
            return
        self._set_busy(True)
        self._log(f"Starting download: {url}")
        threading.Thread(
            target=self._run_download, args=(url,), daemon=True
        ).start()

    def _on_preview_voices(self, event):
        url = self.url_ctrl.GetValue().strip()
        if not url:
            self._log("Error: Enter a story URL or ID first.")
            return
        if self._downloading:
            return
        self._set_busy(True)
        self._log(f"Preview: fetching metadata for {url}")
        threading.Thread(
            target=self._run_preview_voices, args=(url,), daemon=True,
        ).start()

    def _run_preview_voices(self, url):
        try:
            scraper = self._scraper_for(url)
            scraper.parse_story_id(url)

            def progress(current, total, title, cached):
                tag = " (cached)" if cached else ""
                self._log(f"  [{current}/{total}] {title}{tag}")

            # One chapter is enough to get a speaker inventory for most
            # fics. Users running preview on a 500-chapter fic shouldn't
            # wait for a full fetch.
            story = scraper.download(
                url, progress_callback=progress, chapters=[(1, 1)],
            )

            from . import tts
            output_dir = Path(self.output_ctrl.GetValue())
            output_dir.mkdir(parents=True, exist_ok=True)
            map_path = output_dir / f".ffn-voices-{story.id}.json"

            voices, mapper = tts.detect_voices(story, map_path=map_path)
            self._log(
                f"Detected {len(voices)} character(s). "
                f"Voice map: {map_path.name}"
            )
        except Exception as exc:
            self._log(f"Preview failed: {exc}")
            self._set_busy(False)
            return

        wx.CallAfter(self._open_voice_dialog, voices, mapper, tts.NARRATOR_VOICE)
        self._set_busy(False)

    def _open_voice_dialog(self, voices, mapper, narrator_voice):
        if not voices:
            wx.MessageBox(
                "No speaking characters detected in chapter 1. The fic "
                "may be first-person narration with no dialogue, or the "
                "dialogue attribution heuristic couldn't find attributed "
                "speakers.",
                "Preview",
                wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        dlg = VoicePreviewDialog(self, voices, mapper, narrator_voice)
        dlg.ShowModal()
        dlg.Destroy()

    def _on_update(self, event):
        if self._downloading:
            return
        dlg = wx.FileDialog(
            self, "Select file to update",
            wildcard="Supported files (*.epub;*.html;*.txt)|*.epub;*.html;*.txt",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        path = dlg.GetPath()
        dlg.Destroy()

        from .updater import extract_source_url, count_chapters

        try:
            url = extract_source_url(path)
            existing = count_chapters(path)
        except (ValueError, FileNotFoundError) as e:
            self._log(f"Error: {e}")
            return

        suffix = Path(path).suffix.lower()
        fmt_map = {".epub": 0, ".html": 1, ".txt": 2}
        self.format_ctrl.SetSelection(fmt_map.get(suffix, 0))
        self.output_ctrl.SetValue(str(Path(path).parent))

        self._set_busy(True)
        self._log(f"Updating: {url} (existing file has {existing} chapters)")
        threading.Thread(
            target=self._run_download, args=(url,),
            kwargs={"skip_chapters": existing, "is_update": True},
            daemon=True,
        ).start()

    # ── Search ───────────────────────────────────────────────

    def _open_multi_picker(self, ctrl, title, options):
        """Open the multi-pick dialog, seed it with the comma-separated
        labels already in `ctrl`, and write the picked labels back on OK.
        `options` is the ordered label list to show.
        """
        current = [
            s.strip() for s in ctrl.GetValue().split(",") if s.strip()
        ]
        dlg = MultiPickerDialog(self, title, list(options), initial=current)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                ctrl.SetValue(", ".join(dlg.picked_labels()))
        finally:
            dlg.Destroy()

    def _collect_filters(self, tab):
        filters = {}
        for key, ctrl in tab["filter_ctrls"].items():
            idx = ctrl.GetSelection()
            if idx <= 0:
                # First entry is always "any"/"all"/"best match" — no filter
                continue
            filters[key] = ctrl.GetString(idx)
        for key, ctrl in tab["text_ctrls"].items():
            value = ctrl.GetValue().strip()
            if value:
                filters[key] = value
        for key, ctrl in tab["checkbox_ctrls"].items():
            if ctrl.GetValue():
                filters[key] = True
        return filters

    def _on_search(self, site_key):
        tab = self._tabs[site_key]
        query = tab["query_ctrl"].GetValue().strip()
        if self._downloading:
            return
        filters = self._collect_filters(tab)
        # Most searches need a free-text query, but several site/filter
        # combinations are valid *without* one:
        #   • RR list browse (Rising Stars, Best Rated, …)
        #   • RR filter-only browse (tags, genres, warnings, or numeric
        #     bounds with no title) — RR's /fictions/search accepts
        #     tagsAdd-only, and that's the whole point of the Pick
        #     Genres / Pick Tags dialogs.
        #   • Literotica category browse — the category slug IS the
        #     browse target.
        list_browse = (
            site_key == "royalroad"
            and filters.get("list")
            and filters["list"].strip().lower() != "search"
        )
        rr_filter_only = (
            site_key == "royalroad"
            and any(
                filters.get(k)
                for k in (
                    "tags", "tags_picked", "genres", "warnings",
                    "status", "type", "order_by",
                    "min_words", "max_words", "min_pages", "max_pages",
                    "min_rating",
                )
            )
        )
        lit_cat_browse = (
            site_key == "literotica" and filters.get("category")
        )
        if not query and not (list_browse or rr_filter_only or lit_cat_browse):
            self._log("Error: Please enter a search query.")
            return
        self._set_busy(True)
        tab["results_ctrl"].DeleteAllItems()
        tab["summary_ctrl"].SetValue("")
        tab["results"] = []
        tab["_raw_results"] = []
        tab["next_page"] = 1
        tab["last_query"] = query
        tab["last_filters"] = filters
        filter_str = (
            " [" + ", ".join(f"{k}={v}" for k, v in filters.items()) + "]"
            if filters else ""
        )
        site_label = {
            "ao3": "AO3", "ffn": "FFN",
            "royalroad": "Royal Road", "literotica": "Literotica",
        }.get(site_key, site_key)
        self._log(f"Searching {site_label} for: {query}{filter_str}")
        threading.Thread(
            target=self._run_search,
            args=(site_key, query, filters, 1, False),
            daemon=True,
        ).start()

    def _on_load_more(self, site_key):
        tab = self._tabs[site_key]
        # last_query may be empty when the user is browsing an RR list
        # (no free-text query); gate on whether a search has actually
        # been run, via the presence of last_filters.
        if self._downloading or tab.get("last_query") is None:
            return
        self._set_busy(True)
        next_page = tab.get("next_page", 2)
        self._log(f"Loading page {next_page}...")
        threading.Thread(
            target=self._run_search,
            args=(site_key, tab["last_query"], tab["last_filters"], next_page, True),
            daemon=True,
        ).start()

    def _run_search(self, site_key, query, filters, page, append):
        from .search import fetch_until_limit
        tab = self._tabs[site_key]
        try:
            page_results, next_page = fetch_until_limit(
                tab["search_fn"], query,
                limit=25, start_page=page, **filters,
            )
        except Exception as e:
            self._log(f"Search error: {e}")
            self._set_busy(False)
            return
        wx.CallAfter(
            self._populate_results, site_key, page_results, next_page, append,
        )
        self._set_busy(False)

    def _populate_results(self, site_key, new_results, next_page, append):
        from .search import collapse_ao3_series, collapse_literotica_series
        tab = self._tabs[site_key]

        # Keep the raw (uncollapsed) results across load-more so we can
        # re-run collapse on the full set — otherwise parts of the same
        # series that span page boundaries (e.g. `Miss Abby` on page 1,
        # `Miss Abby Pt. 02` on page 2) never find each other.
        if append:
            raw = list(tab.get("_raw_results") or []) + list(new_results)
        else:
            raw = list(new_results)
        tab["_raw_results"] = raw

        if site_key == "ao3":
            processed = collapse_ao3_series(raw)
        elif site_key == "literotica":
            processed = collapse_literotica_series(raw)
        else:
            processed = list(raw)

        previous_count = len(tab["results"]) if append else 0
        tab["results"] = processed
        tab["next_page"] = next_page

        ctrl = tab["results_ctrl"]
        ctrl.Freeze()
        try:
            ctrl.DeleteAllItems()
            for r in tab["results"]:
                row = ctrl.InsertItem(ctrl.GetItemCount(), self._result_title(r))
                ctrl.SetItem(row, 1, r.get("author", "") or "")
                ctrl.SetItem(row, 2, r.get("fandom", "") or "")
                ctrl.SetItem(row, 3, str(r.get("words", "")))
                ctrl.SetItem(row, 4, str(r.get("chapters", "")))
                ctrl.SetItem(row, 5, r.get("rating", "") or "")
                ctrl.SetItem(row, 6, r.get("status", "") or "")
        finally:
            ctrl.Thaw()

        tab["load_more_btn"].Enable(bool(new_results) and not self._downloading)
        if not tab["results"]:
            self._log("No results found." if not append else "No more results.")
            return

        if append:
            added = len(tab["results"]) - previous_count
            focus_row = previous_count if added > 0 else 0
            self._log(
                f"Loaded more. Total {len(tab['results'])} rows "
                f"(+{max(added, 0)})."
                if added > 0 else "No more results."
            )
        else:
            focus_row = 0
            self._log(f"Found {len(tab['results'])} results.")

        ctrl.SetFocus()
        ctrl.Focus(focus_row)
        ctrl.Select(focus_row)

    @staticmethod
    def _result_title(r):
        if r.get("is_series"):
            parts = len(r.get("series_parts") or [])
            return f"[Series · {parts} part(s)] {r['title']}"
        return r.get("title", "")

    def _on_result_select(self, event, site_key):
        tab = self._tabs[site_key]
        idx = event.GetIndex()
        if 0 <= idx < len(tab["results"]):
            r = tab["results"][idx]
            summary = r.get("summary", "") or ""
            if r.get("is_series"):
                parts = r.get("series_parts") or []
                part_lines = "\n".join(
                    f"  - {p.get('title', '(untitled)')}" for p in parts
                )
                preview = (
                    f"[Series of {len(parts)} part(s) from search results]\n"
                    f"{summary}\n\n{part_lines}"
                    if part_lines else f"[Series]\n{summary}"
                )
                tab["summary_ctrl"].SetValue(preview.strip())
                tab["show_parts_btn"].Enable(bool(parts))
            else:
                tab["summary_ctrl"].SetValue(summary or "(no summary)")
                tab["show_parts_btn"].Disable()
            tab["search_dl_btn"].Enable(not self._downloading)
        event.Skip()

    def _on_search_download(self, site_key):
        tab = self._tabs[site_key]
        idx = tab["results_ctrl"].GetFirstSelected()
        if idx < 0 or idx >= len(tab["results"]):
            return
        picked = tab["results"][idx]
        url = picked.get("url")
        if not url:
            self._log("Error: selected result has no URL.")
            return
        if self._downloading:
            return
        self._set_busy(True)
        if picked.get("is_series"):
            self._log(f"Starting series download: {url}")
            if picked.get("parts_only"):
                part_urls = [
                    p.get("url")
                    for p in (picked.get("series_parts") or [])
                    if p.get("url")
                ]
                series_name = picked.get("title") or "Series"
                threading.Thread(
                    target=self._run_series_merge_download,
                    args=(url,),
                    kwargs={
                        "series_name": series_name,
                        "part_urls": part_urls,
                    },
                    daemon=True,
                ).start()
            else:
                threading.Thread(
                    target=self._run_series_merge_download,
                    args=(url,), daemon=True,
                ).start()
        else:
            self._log(f"Starting download: {url}")
            threading.Thread(
                target=self._run_download, args=(url,), daemon=True
            ).start()

    def _on_result_activated(self, site_key):
        # Enter/double-click: for a regular work row, start the download;
        # for a series row, open the parts dialog so keyboard-only users
        # can actually see what's inside the series instead of blindly
        # kicking off a multi-part merge download.
        tab = self._tabs[site_key]
        idx = tab["results_ctrl"].GetFirstSelected()
        if 0 <= idx < len(tab["results"]):
            if tab["results"][idx].get("is_series"):
                self._on_show_parts(site_key)
                return
        self._on_search_download(site_key)

    def _on_show_parts(self, site_key):
        tab = self._tabs[site_key]
        idx = tab["results_ctrl"].GetFirstSelected()
        if idx < 0 or idx >= len(tab["results"]):
            return
        row = tab["results"][idx]
        if not row.get("is_series"):
            return
        parts = row.get("series_parts") or []
        if not parts:
            wx.MessageBox(
                "No parts have been loaded for this series yet.",
                "Series parts",
                wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        dlg = SeriesPartsDialog(self, row["title"], parts)
        if dlg.ShowModal() == wx.ID_OK:
            picked = dlg.picked_url()
            if picked and not self._downloading:
                self._set_busy(True)
                self._log(f"Starting part download: {picked}")
                threading.Thread(
                    target=self._run_download, args=(picked,), daemon=True
                ).start()
        dlg.Destroy()

    def _run_series_merge_download(self, series_url, *, series_name=None, part_urls=None):
        try:
            from .ao3 import AO3Scraper
            from .cli import _merge_stories
            from .literotica import LiteroticaScraper

            name = series_name
            work_urls = None
            if part_urls:
                # Literotica-style collapsed row. First try resolving the
                # canonical /series/se/<id> from the anchor part so we can
                # pick up chapters that never matched the search. Fall
                # back to the known part URLs if there's no series link.
                anchor = part_urls[0]
                try:
                    lit = LiteroticaScraper()
                    resolved = lit.resolve_series_url(anchor)
                except Exception as exc:
                    resolved = None
                    self._log(f"  (Couldn't resolve series URL: {exc})")
                if resolved:
                    self._log(f"Resolved full series: {resolved}")
                    try:
                        name, work_urls = lit.scrape_series_works(resolved)
                        series_url = resolved
                    except Exception as exc:
                        self._log(f"  (Series scrape failed: {exc}); using known parts.")
                        work_urls = None
                if not work_urls:
                    work_urls = part_urls
                    name = series_name or series_url
            else:
                if AO3Scraper.is_series_url(series_url):
                    scraper = AO3Scraper()
                elif LiteroticaScraper.is_series_url(series_url):
                    scraper = LiteroticaScraper()
                else:
                    scraper = self._scraper_for(series_url)

                self._log(f"Fetching series: {series_url}")
                name, work_urls = scraper.scrape_series_works(series_url)
            if not work_urls:
                self._log("No works found in this series.")
                return
            series_name = name

            self._log(f"Series: {series_name}")
            self._log(f"Downloading and merging {len(work_urls)} works...")

            def progress(current, total, title, cached):
                tag = " (cached)" if cached else ""
                self._log(f"    [{current}/{total}] {title}{tag}")

            stories = []
            for i, work_url in enumerate(work_urls, 1):
                self._log(f"\n[{i}/{len(work_urls)}] {work_url}")
                try:
                    work_scraper = self._scraper_for(work_url)
                    stories.append(
                        work_scraper.download(work_url, progress_callback=progress)
                    )
                except Exception as exc:
                    self._log(f"  Error: {exc}")

            if not stories:
                self._log("Nothing downloaded.")
                return

            merged = _merge_stories(series_name, series_url, stories)
            self._log(
                f"\nMerged {len(stories)} works / {len(merged.chapters)} sections"
            )
            path = self._export_story(merged)
            self._log(f"Saved: {path}")
        except Exception as exc:
            self._log(f"Series download failed: {exc}")
        finally:
            self._set_busy(False)

    # ── Clipboard watch ──────────────────────────────────────

    def _on_watch_toggle(self, event):
        if self.watch_btn.GetValue():
            self._watching = True
            self._watch_seen.clear()
            self._last_clip = self._get_clipboard()
            self._clip_timer.Start(2000)
            self._log("Watching clipboard. Copy a fanfiction URL to auto-download.")
            self.watch_btn.SetLabel("Stop &Watching")
        else:
            self._watching = False
            self._clip_timer.Stop()
            self._log("Clipboard watch stopped.")
            self.watch_btn.SetLabel("&Watch Clipboard")

    def _get_clipboard(self):
        text = ""
        if wx.TheClipboard.Open():
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_TEXT)):
                data = wx.TextDataObject()
                wx.TheClipboard.GetData(data)
                text = data.GetText().strip()
            wx.TheClipboard.Close()
        return text

    def _on_clip_timer(self, event):
        if not self._watching:
            return
        clip = self._get_clipboard()
        if clip == self._last_clip:
            return
        self._last_clip = clip

        match = _FFN_URL_RE.search(clip)
        if not match:
            return
        url = match.group(0)
        if url in self._watch_seen:
            return
        self._watch_seen.add(url)

        if self._downloading:
            self._log(f"Queued (busy): {url}")
            return

        self._log(f"Clipboard detected: {url}")
        self.url_ctrl.SetValue(url)
        self._set_busy(True)
        threading.Thread(
            target=self._run_download, args=(url,), daemon=True
        ).start()

    # ── Download worker ──────────────────────────────────────

    def _scraper_for(self, url):
        from .ao3 import AO3Scraper
        from .ficwad import FicWadScraper
        from .literotica import LiteroticaScraper
        from .mediaminer import MediaMinerScraper
        from .royalroad import RoyalRoadScraper
        from .scraper import FFNScraper

        text = url.lower()
        if "ficwad.com" in text:
            return FicWadScraper()
        if "archiveofourown.org" in text or "ao3.org" in text:
            return AO3Scraper()
        if "royalroad.com" in text:
            return RoyalRoadScraper()
        if "mediaminer.org" in text:
            return MediaMinerScraper()
        if "literotica.com" in text:
            return LiteroticaScraper()
        return FFNScraper()

    def _export_story(self, story):
        fmt = self.format_ctrl.GetString(self.format_ctrl.GetSelection())
        output_dir = self.output_ctrl.GetValue()
        template = self.name_ctrl.GetValue()
        hr_as_stars = self.hr_stars_ctrl.GetValue()
        strip_notes = self.strip_notes_ctrl.GetValue()

        if fmt == "audio":
            from .tts import generate_audiobook

            def audio_progress(current, total, title):
                self._log(f"  Synthesizing [{current}/{total}] {title}")

            self._log("\nGenerating audiobook...")
            return generate_audiobook(
                story, output_dir, progress_callback=audio_progress
            )

        from .exporters import EXPORTERS
        exporter = EXPORTERS[fmt]
        return exporter(
            story, output_dir, template=template,
            hr_as_stars=hr_as_stars, strip_notes=strip_notes,
        )

    def _run_download(self, url, skip_chapters=0, is_update=False):
        try:
            from .ao3 import AO3Scraper
            from .literotica import LiteroticaScraper

            scraper = self._scraper_for(url)

            if not is_update and AO3Scraper.is_bookmarks_url(url):
                self._run_picker_download(
                    url, AO3Scraper(), kind="bookmarks",
                )
                return

            if not is_update and scraper.is_author_url(url):
                self._run_picker_download(url, scraper, kind="author")
                return

            if not is_update and (
                AO3Scraper.is_series_url(url)
                or LiteroticaScraper.is_series_url(url)
            ):
                # Ensure scraper matches the series host
                if AO3Scraper.is_series_url(url) and not isinstance(scraper, AO3Scraper):
                    scraper = AO3Scraper()
                elif LiteroticaScraper.is_series_url(url) and not isinstance(scraper, LiteroticaScraper):
                    scraper = LiteroticaScraper()
                self._run_series_download(url, scraper)
                return

            scraper.parse_story_id(url)

            def progress(current, total, title, cached):
                tag = " (cached)" if cached else ""
                self._log(f"  [{current}/{total}] {title}{tag}")

            story = scraper.download(
                url, progress_callback=progress, skip_chapters=skip_chapters
            )

            if is_update and len(story.chapters) == 0:
                self._log("Up to date. No new chapters.")
                self._set_busy(False)
                return

            if is_update:
                self._log(f"Found {len(story.chapters)} new chapters. Re-exporting...")
                story = scraper.download(url, progress_callback=progress, skip_chapters=0)

            self._log(f"\n  Title:    {story.title}")
            self._log(f"  Author:   {story.author}")
            self._log(f"  Chapters: {len(story.chapters)}")

            path = self._export_story(story)
            self._log(f"\nDone! Saved to: {path}")

        except Exception as e:
            self._log(f"\nError: {e}")
        finally:
            self._set_busy(False)

    def _run_series_download(self, url, scraper):
        self._log(f"Fetching series: {url}")
        series_name, work_urls = scraper.scrape_series_works(url)
        if not work_urls:
            self._log("No works found in this series.")
            return
        self._log(f"Series: {series_name}")
        self._log(f"Found {len(work_urls)} works. Downloading in series order...")
        self._batch_download(work_urls, scraper, summary_label="Series")

    def _run_author_download(self, url, scraper):
        self._log(f"Fetching author page: {url}")
        author_name, story_urls = scraper.scrape_author_stories(url)
        if not story_urls:
            self._log("No stories found on the author page.")
            return
        self._log(f"Author: {author_name}")
        self._log(f"Found {len(story_urls)} stories. Downloading all...")
        self._batch_download(story_urls, scraper, summary_label="Author batch")

    def _run_picker_download(self, url, scraper, *, kind):
        """Fetch a work list (author page or AO3 bookmarks) and open the
        picker so the user can choose which works to download before we
        start pulling chapters.
        """
        from .ao3 import AO3Scraper
        from .scraper import FFNScraper

        label = "bookmarks" if kind == "bookmarks" else "author page"
        self._log(f"Fetching {label}: {url}")
        try:
            if kind == "bookmarks":
                owner, works = scraper.scrape_bookmark_works(url)
                title = f"Bookmarks: {owner}"
            elif isinstance(scraper, FFNScraper):
                owner, works = scraper.scrape_author_works(
                    url, include_favorites=True,
                )
                title = f"Stories by {owner}"
            elif hasattr(scraper, "scrape_author_works"):
                owner, works = scraper.scrape_author_works(url)
                title = f"Stories by {owner}"
            else:
                owner, story_urls = scraper.scrape_author_stories(url)
                works = [
                    {"title": u, "url": u, "author": owner, "section": "own"}
                    for u in story_urls
                ]
                title = f"Stories by {owner}"
        except Exception as exc:
            self._log(f"Failed to list {label}: {exc}")
            return

        if not works:
            self._log(f"No entries found on this {label}.")
            return
        self._log(f"Loaded {len(works)} entries. Showing picker...")

        def _handle_selection(selected_urls):
            if not selected_urls:
                self._log("(No selections — nothing downloaded.)")
                return
            self._log(f"Downloading {len(selected_urls)} selected...")
            self._set_busy(True)
            threading.Thread(
                target=self._run_picked_batch,
                args=(selected_urls, kind),
                daemon=True,
            ).start()

        wx.CallAfter(self._open_picker, title, works, _handle_selection)

    def _open_picker(self, title, works, on_ok):
        dlg = StoryPickerDialog(self, title, works)
        result = dlg.ShowModal()
        picked_urls = dlg.picked_urls() if result == wx.ID_OK else []
        dlg.Destroy()
        on_ok(picked_urls)

    def _run_picked_batch(self, urls, kind):
        try:
            # Each url may target a different scraper (e.g. bookmarks can
            # include works outside the owner's own, but on AO3 they're
            # still AO3 works). Use per-URL scraper resolution.
            succeeded = 0
            failed = []
            for i, story_url in enumerate(urls, 1):
                self._log(f"\n[{i}/{len(urls)}] {story_url}")
                scraper = self._scraper_for(story_url)

                def progress(current, total, t, cached):
                    tag = " (cached)" if cached else ""
                    self._log(f"    [{current}/{total}] {t}{tag}")

                try:
                    story = scraper.download(
                        story_url, progress_callback=progress,
                    )
                    path = self._export_story(story)
                    self._log(f"  Saved: {path}")
                    succeeded += 1
                except Exception as exc:
                    self._log(f"  Error: {exc}")
                    failed.append(story_url)
            label = "Bookmarks batch" if kind == "bookmarks" else "Author batch"
            self._log(
                f"\n{label} complete: {succeeded} succeeded, "
                f"{len(failed)} failed out of {len(urls)}."
            )
            for u in failed:
                self._log(f"  Failed: {u}")
        finally:
            self._set_busy(False)

    def _batch_download(self, story_urls, scraper, summary_label="Batch"):

        def progress(current, total, title, cached):
            tag = " (cached)" if cached else ""
            self._log(f"    [{current}/{total}] {title}{tag}")

        succeeded = 0
        failed = []
        for i, story_url in enumerate(story_urls, 1):
            self._log(f"\n[{i}/{len(story_urls)}] {story_url}")
            try:
                story = scraper.download(story_url, progress_callback=progress)
                path = self._export_story(story)
                self._log(f"  Saved: {path}")
                succeeded += 1
            except Exception as e:
                self._log(f"  Error: {e}")
                failed.append(story_url)

        self._log(
            f"\n{summary_label} complete: {succeeded} succeeded, "
            f"{len(failed)} failed out of {len(story_urls)}."
        )
        for u in failed:
            self._log(f"  Failed: {u}")


class VoicePreviewDialog(wx.Dialog):
    """Show detected characters, their assigned voices, and let users play
    a short sample or swap the voice before committing to an audiobook
    generation run. Changes are persisted to the same voice-map JSON the
    audiobook generator reads from, so saving and generating afterwards
    uses the edited mapping.
    """

    SAMPLE_TEXT = (
        "Hello. My name is {name}. I am a character in this story, "
        "and this is how I will sound in the audiobook."
    )

    def __init__(self, parent, voices, mapper, narrator_voice):
        super().__init__(
            parent, title="Preview character voices",
            size=(720, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._voices = voices  # list of {name, gender, voice, count}
        self._mapper = mapper
        self._narrator_voice = narrator_voice
        self._player = None
        self._tmp_dir = None
        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_ui(self):
        from . import tts
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Select a character and click Play Sample to hear "
                    "their assigned voice. Change Voice swaps to a "
                    "different option for that character."
                ),
            ),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Detected characters and their voices")
        for i, (label, width) in enumerate([
            ("Character", 180), ("Gender", 70), ("Lines", 60), ("Voice", 300),
        ]):
            self.list_ctrl.InsertColumn(i, label, width=width)
        self._refresh_rows()
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_play)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.play_btn = wx.Button(panel, label="&Play Sample")
        self.play_btn.Bind(wx.EVT_BUTTON, self._on_play)
        btn_row.Add(self.play_btn, 0, wx.RIGHT, 8)

        self.change_btn = wx.Button(panel, label="&Change Voice...")
        self.change_btn.Bind(wx.EVT_BUTTON, self._on_change_voice)
        btn_row.Add(self.change_btn, 0, wx.RIGHT, 8)

        self.narrator_btn = wx.Button(panel, label="Play &Narrator")
        self.narrator_btn.Bind(wx.EVT_BUTTON, self._on_play_narrator)
        btn_row.Add(self.narrator_btn, 0)

        btn_row.AddStretchSpacer(1)
        ok_btn = wx.Button(panel, id=wx.ID_OK, label="&OK")
        ok_btn.SetDefault()
        btn_row.Add(ok_btn, 0)

        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(sizer)

        # Pre-create a temp dir for sample files, reusing across plays
        import tempfile
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-preview-"))

    def _refresh_rows(self):
        self.list_ctrl.DeleteAllItems()
        for entry in self._voices:
            row = self.list_ctrl.GetItemCount()
            self.list_ctrl.InsertItem(row, entry["name"])
            self.list_ctrl.SetItem(row, 1, entry["gender"])
            self.list_ctrl.SetItem(row, 2, str(entry.get("count", "")))
            self.list_ctrl.SetItem(row, 3, entry["voice"])
        if self._voices:
            self.list_ctrl.Focus(0)
            self.list_ctrl.Select(0)

    def _selected_index(self):
        idx = self.list_ctrl.GetFirstSelected()
        return idx if 0 <= idx < len(self._voices) else -1

    def _stop_player(self):
        if self._player and self._player.poll() is None:
            try:
                self._player.terminate()
            except Exception:
                pass
        self._player = None

    def _play_voice(self, voice, name):
        from . import tts
        import threading
        self._stop_player()
        sample = self.SAMPLE_TEXT.format(name=name)
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] or "sample"
        out_path = self._tmp_dir / f"{safe_name}-{voice}.mp3"

        def worker():
            try:
                if not out_path.exists() or out_path.stat().st_size == 0:
                    tts.synthesize_sample(voice, sample, out_path)
                self._player = tts.play_audio_file(out_path)
            except Exception as exc:
                wx.CallAfter(
                    wx.MessageBox,
                    f"Could not play sample: {exc}",
                    "Preview error", wx.OK | wx.ICON_ERROR, self,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_play(self, event):
        idx = self._selected_index()
        if idx < 0:
            return
        entry = self._voices[idx]
        self._play_voice(entry["voice"], entry["name"])

    def _on_play_narrator(self, event):
        self._play_voice(self._narrator_voice, "Narrator")

    def _on_change_voice(self, event):
        idx = self._selected_index()
        if idx < 0:
            return
        entry = self._voices[idx]
        from .tts import FEMALE_VOICES, MALE_VOICES, NEUTRAL_VOICES

        if entry["gender"] == "female":
            candidates = FEMALE_VOICES
        elif entry["gender"] == "male":
            candidates = MALE_VOICES
        else:
            candidates = NEUTRAL_VOICES

        dlg = wx.SingleChoiceDialog(
            self,
            f"Pick a voice for {entry['name']}:",
            "Change voice",
            candidates,
        )
        try:
            current = candidates.index(entry["voice"])
            dlg.SetSelection(current)
        except ValueError:
            pass
        if dlg.ShowModal() == wx.ID_OK:
            new_voice = dlg.GetStringSelection()
            if new_voice and new_voice != entry["voice"]:
                entry["voice"] = new_voice
                self._mapper.mapping[entry["name"]] = new_voice
                self._mapper.save()
                self._refresh_rows()
                self.list_ctrl.Focus(idx)
                self.list_ctrl.Select(idx)
        dlg.Destroy()

    def _on_close(self, event):
        self._stop_player()
        import shutil as _shutil
        if self._tmp_dir and self._tmp_dir.exists():
            _shutil.rmtree(self._tmp_dir, ignore_errors=True)
        event.Skip()


class StoryPickerDialog(wx.Dialog):
    """Multi-select picker for an author's works or a bookmarks list.

    Uses a CheckListBox with per-item formatted labels — that gives NVDA
    a single readable string per row, plus native space-to-toggle.
    """

    _SORT_OPTIONS = [
        ("Default order", None),
        ("Title (A-Z)", "title_asc"),
        ("Title (Z-A)", "title_desc"),
        ("Word count (most first)", "words_desc"),
        ("Word count (least first)", "words_asc"),
        ("Chapter count (most first)", "chapters_desc"),
        ("Last updated (newest first)", "updated_desc"),
        ("Last updated (oldest first)", "updated_asc"),
        ("Section (own first)", "section"),
    ]

    def __init__(self, parent, title, works):
        super().__init__(
            parent, title=title,
            size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._works = list(works)
        self._order = list(range(len(self._works)))
        self._sort_key = None
        self._section_filter = "all"
        self._picked = []
        self._build_ui()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(
            wx.StaticText(panel, label="Sor&t by:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.sort_ctrl = wx.Choice(
            panel, choices=[label for label, _ in self._SORT_OPTIONS],
        )
        self.sort_ctrl.SetSelection(0)
        self.sort_ctrl.SetName("Sort order")
        self.sort_ctrl.Bind(wx.EVT_CHOICE, self._on_sort_change)
        controls.Add(self.sort_ctrl, 0, wx.RIGHT, 16)

        has_sections = any(
            w.get("section") in ("favorites", "bookmarks")
            for w in self._works
        )
        if has_sections:
            controls.Add(
                wx.StaticText(panel, label="Sho&w:"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
            )
            self.filter_ctrl = wx.Choice(
                panel, choices=["All", "Own only", "Favorites only"],
            )
            self.filter_ctrl.SetSelection(0)
            self.filter_ctrl.SetName("Section filter")
            self.filter_ctrl.Bind(wx.EVT_CHOICE, self._on_filter_change)
            controls.Add(self.filter_ctrl, 0, wx.RIGHT, 16)
        else:
            self.filter_ctrl = None

        select_all = wx.Button(panel, label="&Select All")
        select_all.Bind(wx.EVT_BUTTON, lambda e: self._set_all(True))
        controls.Add(select_all, 0, wx.RIGHT, 4)
        select_none = wx.Button(panel, label="Select &None")
        select_none.Bind(wx.EVT_BUTTON, lambda e: self._set_all(False))
        controls.Add(select_none, 0)

        sizer.Add(controls, 0, wx.EXPAND | wx.ALL, 8)

        self.list_ctrl = wx.CheckListBox(panel, choices=[])
        self.list_ctrl.SetName("Stories to download")
        # wx.CheckListBox's MSAA check-state reporting is unreliable with
        # NVDA on Windows — prepend "[x] " / "[ ] " to every label so the
        # state is read out as part of the item text. EVT_CHECKLISTBOX
        # refreshes the prefix on toggle; EVT_LISTBOX updates the summary
        # pane as the user arrows through.
        self.list_ctrl.Bind(wx.EVT_CHECKLISTBOX, self._on_item_toggled)
        self.list_ctrl.Bind(wx.EVT_LISTBOX, self._on_item_focus_changed)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Summary pane: mirrors the selected row's summary so keyboard
        # users don't have to abandon the dialog to see what a story is.
        sizer.Add(
            wx.StaticText(panel, label="S&ummary:"),
            0, wx.LEFT | wx.RIGHT, 8,
        )
        self.summary_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 80),
        )
        self.summary_ctrl.SetName("Story summary")
        sizer.Add(self.summary_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        hint = wx.StaticText(
            panel,
            label=(
                "Use the arrow keys to move, space to tick or untick, "
                "and press Download to fetch every ticked story."
            ),
        )
        sizer.Add(hint, 0, wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        dl_btn = wx.Button(panel, id=wx.ID_OK, label="&Download Selected")
        dl_btn.SetDefault()
        dl_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_row.Add(dl_btn, 0, wx.RIGHT, 8)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Cancel")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self._refresh()

    @staticmethod
    def _as_int(value):
        if value is None:
            return 0
        s = str(value).replace(",", "").strip()
        m = re.match(r"\d+", s)
        return int(m.group(0)) if m else 0

    def _label(self, w, checked=False):
        # Leading "[x] " / "[ ] " so NVDA reads the state as part of the
        # item; the native MSAA state is unreliable in CheckListBox.
        prefix = "[x] " if checked else "[ ] "
        parts = [prefix, w.get("title", "") or "(untitled)"]
        meta = []
        if w.get("author"):
            meta.append(f"by {w['author']}")
        if w.get("words"):
            meta.append(f"{w['words']} words")
        if w.get("chapters"):
            meta.append(f"{w['chapters']} ch")
        if w.get("rating"):
            meta.append(f"Rated {w['rating']}")
        if w.get("status"):
            meta.append(w["status"])
        if w.get("updated"):
            meta.append(f"upd {w['updated']}")
        if w.get("section") == "favorites":
            meta.append("[Favorite]")
        elif w.get("section") == "bookmarks":
            meta.append("[Bookmark]")
        if meta:
            parts.append(" — " + " · ".join(meta))
        return "".join(parts)

    def _visible_indices(self):
        idxs = []
        for i in self._order:
            w = self._works[i]
            if self._section_filter == "own" and w.get("section") != "own":
                continue
            if self._section_filter == "favorites" and w.get("section") not in (
                "favorites", "bookmarks",
            ):
                continue
            idxs.append(i)
        return idxs

    def _refresh(self):
        idxs = self._visible_indices()
        # Preserve ticks across re-sort/filter by URL
        ticked_urls = {
            self._works[self._visible_map[j]]["url"]
            for j in self.list_ctrl.GetCheckedItems()
        } if getattr(self, "_visible_map", None) else set()
        labels = [
            self._label(
                self._works[i],
                checked=self._works[i].get("url") in ticked_urls,
            )
            for i in idxs
        ]
        self.list_ctrl.Set(labels)
        self._visible_map = idxs
        restored = [
            j for j, i in enumerate(idxs)
            if self._works[i].get("url") in ticked_urls
        ]
        if restored:
            self.list_ctrl.SetCheckedItems(restored)
        # Refresh the summary pane for whatever row is currently focused.
        self._update_summary()

    def _update_label_at(self, row):
        if not (0 <= row < len(self._visible_map)):
            return
        w = self._works[self._visible_map[row]]
        checked = self.list_ctrl.IsChecked(row)
        self.list_ctrl.SetString(row, self._label(w, checked=checked))

    def _update_summary(self):
        if not hasattr(self, "summary_ctrl"):
            return
        row = self.list_ctrl.GetSelection()
        if row == wx.NOT_FOUND or not (0 <= row < len(self._visible_map)):
            self.summary_ctrl.SetValue("")
            return
        w = self._works[self._visible_map[row]]
        summary = w.get("summary") or ""
        if not summary:
            summary = "(no summary)"
        self.summary_ctrl.SetValue(summary)

    def _on_item_toggled(self, event):
        self._update_label_at(event.GetSelection())
        event.Skip()

    def _on_item_focus_changed(self, event):
        self._update_summary()
        event.Skip()

    def _on_sort_change(self, event):
        idx = self.sort_ctrl.GetSelection()
        _, key = self._SORT_OPTIONS[idx] if 0 <= idx < len(self._SORT_OPTIONS) else (None, None)
        self._sort_key = key
        self._apply_sort()
        self._refresh()

    def _on_filter_change(self, event):
        sel = self.filter_ctrl.GetSelection()
        self._section_filter = {0: "all", 1: "own", 2: "favorites"}.get(sel, "all")
        self._refresh()

    def _apply_sort(self):
        works = self._works
        default = list(range(len(works)))

        def words(i):
            return self._as_int(works[i].get("words"))

        def chapters(i):
            return self._as_int(works[i].get("chapters"))

        key = self._sort_key
        if key is None:
            self._order = default
        elif key == "title_asc":
            self._order = sorted(default, key=lambda i: (works[i].get("title") or "").lower())
        elif key == "title_desc":
            self._order = sorted(default, key=lambda i: (works[i].get("title") or "").lower(), reverse=True)
        elif key == "words_desc":
            self._order = sorted(default, key=words, reverse=True)
        elif key == "words_asc":
            self._order = sorted(default, key=words)
        elif key == "chapters_desc":
            self._order = sorted(default, key=chapters, reverse=True)
        elif key == "updated_desc":
            self._order = sorted(default, key=lambda i: works[i].get("updated") or "", reverse=True)
        elif key == "updated_asc":
            self._order = sorted(default, key=lambda i: works[i].get("updated") or "")
        elif key == "section":
            self._order = sorted(default, key=lambda i: (works[i].get("section") != "own", (works[i].get("title") or "").lower()))

    def _set_all(self, checked):
        indices = list(range(self.list_ctrl.GetCount()))
        if checked:
            self.list_ctrl.SetCheckedItems(indices)
        else:
            self.list_ctrl.SetCheckedItems([])
        # Rewrite every label so the "[x] / [ ]" prefix reflects the new state.
        for row in indices:
            self._update_label_at(row)

    def _on_ok(self, event):
        ticked = self.list_ctrl.GetCheckedItems()
        self._picked = [
            self._works[self._visible_map[j]]["url"] for j in ticked
        ]
        self.EndModal(wx.ID_OK)

    def picked_urls(self):
        return list(self._picked)


class MultiPickerDialog(wx.Dialog):
    """Tick-list picker for categorical filters (Royal Road genres, tags,
    content warnings, etc.).

    Same NVDA trick as StoryPickerDialog — every row label is rewritten
    with a literal `[x] ` / `[ ] ` prefix on toggle so the check state
    is part of the readable item text. The dialog returns the ordered
    list of picked *labels* (not slugs); callers can resolve labels to
    whatever canonical form they store.
    """

    def __init__(self, parent, title, options, initial=()):
        super().__init__(
            parent, title=title,
            size=(420, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        # `options` is the ordered list of labels; `initial` the subset
        # that should start ticked. We compare case-insensitively so a
        # saved "litrpg" still ticks "LitRPG" on the next launch.
        self._labels = list(options)
        initial_lower = {str(x).strip().lower() for x in initial}
        self._initial_checks = [
            lbl.lower() in initial_lower for lbl in self._labels
        ]
        self._picked = []
        self._build_ui()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(
            wx.StaticText(panel, label="Fi&lter:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.filter_ctrl = wx.TextCtrl(panel)
        self.filter_ctrl.SetName("Filter options")
        self.filter_ctrl.Bind(wx.EVT_TEXT, self._on_filter_text)
        controls.Add(self.filter_ctrl, 1, wx.RIGHT, 8)
        select_all = wx.Button(panel, label="&Select All")
        select_all.Bind(wx.EVT_BUTTON, lambda e: self._set_visible_all(True))
        controls.Add(select_all, 0, wx.RIGHT, 4)
        select_none = wx.Button(panel, label="Select &None")
        select_none.Bind(wx.EVT_BUTTON, lambda e: self._set_visible_all(False))
        controls.Add(select_none, 0)
        sizer.Add(controls, 0, wx.EXPAND | wx.ALL, 8)

        self.list_ctrl = wx.CheckListBox(panel, choices=[])
        self.list_ctrl.SetName("Options")
        self.list_ctrl.Bind(wx.EVT_CHECKLISTBOX, self._on_item_toggled)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        hint = wx.StaticText(
            panel,
            label=(
                "Arrow keys to move, space to tick or untick. "
                "Type in the filter field to narrow the list."
            ),
        )
        sizer.Add(hint, 0, wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        ok_btn = wx.Button(panel, id=wx.ID_OK, label="&OK")
        ok_btn.SetDefault()
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_row.Add(ok_btn, 0, wx.RIGHT, 8)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Cancel")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

        # _checks tracks the authoritative checked state for every label
        # (index parallel to self._labels). _visible_map maps the list
        # control's visible rows → indices into self._labels.
        self._checks = list(self._initial_checks)
        self._visible_map = list(range(len(self._labels)))
        self._refresh()

    def _label_text(self, idx, checked):
        prefix = "[x] " if checked else "[ ] "
        return prefix + self._labels[idx]

    def _refresh(self):
        self.list_ctrl.Set([
            self._label_text(i, self._checks[i])
            for i in self._visible_map
        ])
        self.list_ctrl.SetCheckedItems([
            row for row, i in enumerate(self._visible_map)
            if self._checks[i]
        ])

    def _on_filter_text(self, event):
        needle = self.filter_ctrl.GetValue().strip().lower()
        if not needle:
            self._visible_map = list(range(len(self._labels)))
        else:
            self._visible_map = [
                i for i, lbl in enumerate(self._labels)
                if needle in lbl.lower()
            ]
        self._refresh()
        event.Skip()

    def _on_item_toggled(self, event):
        row = event.GetSelection()
        if 0 <= row < len(self._visible_map):
            i = self._visible_map[row]
            self._checks[i] = self.list_ctrl.IsChecked(row)
            self.list_ctrl.SetString(
                row, self._label_text(i, self._checks[i]),
            )
        event.Skip()

    def _set_visible_all(self, checked):
        for row, i in enumerate(self._visible_map):
            self._checks[i] = checked
        self._refresh()

    def _on_ok(self, event):
        self._picked = [
            self._labels[i] for i, ok in enumerate(self._checks) if ok
        ]
        self.EndModal(wx.ID_OK)

    def picked_labels(self):
        return list(self._picked)


class SeriesPartsDialog(wx.Dialog):
    """Show the parts of a series and let the user pick one to download on
    its own. Returns wx.ID_OK if a part was picked; retrieve it via
    `picked_url()`.
    """

    def __init__(self, parent, series_name, parts):
        super().__init__(
            parent, title=f"Parts of {series_name}",
            size=(560, 400),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._parts = parts
        self._picked = None

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    f"{len(parts)} part(s) of {series_name} loaded from "
                    "search. Pick one to download on its own, or close "
                    "this dialog and click Download Selected to merge the "
                    "full series into a single file."
                ),
            ),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Series parts")
        for i, (label, width) in enumerate([
            ("Part", 260), ("Author", 140), ("Words", 80), ("Rating", 80),
        ]):
            self.list_ctrl.InsertColumn(i, label, width=width)
        for p in parts:
            row = self.list_ctrl.InsertItem(
                self.list_ctrl.GetItemCount(), p.get("title", "") or "",
            )
            self.list_ctrl.SetItem(row, 1, p.get("author", "") or "")
            self.list_ctrl.SetItem(row, 2, str(p.get("words", "") or ""))
            self.list_ctrl.SetItem(row, 3, p.get("rating", "") or "")
        if parts:
            self.list_ctrl.Focus(0)
            self.list_ctrl.Select(0)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        dl_btn = wx.Button(panel, id=wx.ID_OK, label="&Download Part")
        dl_btn.SetDefault()
        dl_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_row.Add(dl_btn, 0, wx.RIGHT, 8)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Close")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

    def _on_activate(self, event):
        self._on_ok(event)

    def _on_ok(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if 0 <= idx < len(self._parts):
            self._picked = self._parts[idx].get("url")
        self.EndModal(wx.ID_OK)

    def picked_url(self):
        return self._picked


def main():
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
