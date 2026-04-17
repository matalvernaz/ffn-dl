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
from pathlib import Path


_FFN_URL_RE = re.compile(
    r"https?://(?:www\.)?("
    r"fanfiction\.net/s/\d+"
    r"|ficwad\.com/story/\d+"
    r"|(?:archiveofourown\.org|ao3\.org)/works/\d+"
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
        FFN_RATING, FFN_STATUS, FFN_WORDS, search_ffn,
    )
    return {
        "label": "Search FFN",
        "search_fn": search_ffn,
        "filters": [
            ("&Rating:", "rating", list(FFN_RATING)),
            ("&Language:", "language", list(FFN_LANGUAGE)),
            ("S&tatus:", "status", list(FFN_STATUS)),
            ("&Genre:", "genre", list(FFN_GENRE)),
            ("&Words:", "min_words", list(FFN_WORDS)),
            ("&Crossover:", "crossover", list(FFN_CROSSOVER)),
            ("&Match in:", "match", list(FFN_MATCH)),
        ],
    }


def _ao3_search_spec():
    from .search import AO3_COMPLETE, AO3_CROSSOVER, AO3_RATING, AO3_SORT, search_ao3
    return {
        "label": "Search AO3",
        "search_fn": search_ao3,
        "filters": [
            ("&Rating:", "rating", list(AO3_RATING)),
            ("S&tatus:", "complete", list(AO3_COMPLETE)),
            ("&Crossover:", "crossover", list(AO3_CROSSOVER)),
            ("Sor&t by:", "sort", list(AO3_SORT)),
        ],
        "text_filters": [
            ("&Fandom:", "fandom"),
            ("&Character:", "character"),
            ("&Relationship:", "relationship"),
            ("Lang. &code:", "language"),
            ("&Word count:", "word_count"),
        ],
        "checkboxes": [
            ("&Single-chapter only", "single_chapter"),
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
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.log_ctrl.SetName("Status log")
        root_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        root.SetSizer(root_sizer)

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
        btn_sizer.Add(self.watch_btn, 0)

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
            lambda evt, k=site_key: self._on_search_download(k),
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

        state["search_dl_btn"] = wx.Button(panel, label="Do&wnload Selected")
        state["search_dl_btn"].Bind(
            wx.EVT_BUTTON, lambda evt, k=site_key: self._on_search_download(k)
        )
        state["search_dl_btn"].Disable()
        sizer.Add(state["search_dl_btn"], 0, wx.ALL, pad)

        panel.SetSizer(sizer)
        notebook.AddPage(panel, spec["label"])
        self._tabs[site_key] = state

    # ── Helpers ───────────────────────────────────────────────

    def _log(self, msg):
        wx.CallAfter(self.log_ctrl.AppendText, msg + "\n")

    def _set_busy(self, busy):
        def _update():
            self._downloading = busy
            self.dl_btn.Enable(not busy)
            self.update_btn.Enable(not busy)
            for tab in self._tabs.values():
                tab["search_btn"].Enable(not busy)
                has_selection = tab["results_ctrl"].GetFirstSelected() != -1
                tab["search_dl_btn"].Enable(not busy and has_selection)
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
        ):
            if site_key not in self._tabs:
                continue
            state = self._snapshot_search_state(self._tabs[site_key])
            self.prefs.set(pref_key, json.dumps(state))

    @staticmethod
    def _snapshot_search_state(tab):
        return {
            "query": tab["query_ctrl"].GetValue(),
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
        query = state.get("query")
        if isinstance(query, str):
            tab["query_ctrl"].SetValue(query)
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
        self._update_cancelled = False

        def progress_cb(done, total):
            if self._update_cancelled:
                raise RuntimeError("Update cancelled by user.")
            if total > 0:
                pct = min(100, int(done * 100 / total))
                done_mb = done / 1024 / 1024
                total_mb = total / 1024 / 1024
                kept_going, _ = progress.Update(
                    pct, f"Downloaded {done_mb:.0f} / {total_mb:.0f} MB"
                )
                if not kept_going:
                    self._update_cancelled = True
                    raise RuntimeError("Update cancelled by user.")

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
        if not query:
            self._log("Error: Please enter a search query.")
            return
        if self._downloading:
            return
        filters = self._collect_filters(tab)
        self._set_busy(True)
        tab["results_ctrl"].DeleteAllItems()
        tab["summary_ctrl"].SetValue("")
        tab["results"] = []
        filter_str = (
            " [" + ", ".join(f"{k}={v}" for k, v in filters.items()) + "]"
            if filters else ""
        )
        site_label = "AO3" if site_key == "ao3" else "FFN"
        self._log(f"Searching {site_label} for: {query}{filter_str}")
        threading.Thread(
            target=self._run_search, args=(site_key, query, filters), daemon=True,
        ).start()

    def _run_search(self, site_key, query, filters):
        tab = self._tabs[site_key]
        try:
            results = tab["search_fn"](query, **filters)
        except Exception as e:
            self._log(f"Search error: {e}")
            self._set_busy(False)
            return
        wx.CallAfter(self._populate_results, site_key, results)
        self._set_busy(False)

    def _populate_results(self, site_key, results):
        tab = self._tabs[site_key]
        tab["results"] = results
        tab["results_ctrl"].DeleteAllItems()
        if not results:
            self._log("No results found.")
            return

        for r in results:
            row = tab["results_ctrl"].InsertItem(
                tab["results_ctrl"].GetItemCount(), r["title"]
            )
            tab["results_ctrl"].SetItem(row, 1, r["author"])
            tab["results_ctrl"].SetItem(row, 2, r["fandom"])
            tab["results_ctrl"].SetItem(row, 3, str(r["words"]))
            tab["results_ctrl"].SetItem(row, 4, str(r["chapters"]))
            tab["results_ctrl"].SetItem(row, 5, r["rating"])
            tab["results_ctrl"].SetItem(row, 6, r["status"])

        self._log(f"Found {len(results)} results.")
        tab["results_ctrl"].SetFocus()
        tab["results_ctrl"].Focus(0)
        tab["results_ctrl"].Select(0)

    def _on_result_select(self, event, site_key):
        tab = self._tabs[site_key]
        idx = event.GetIndex()
        if 0 <= idx < len(tab["results"]):
            r = tab["results"][idx]
            tab["summary_ctrl"].SetValue(r.get("summary", "") or "(no summary)")
            tab["search_dl_btn"].Enable(not self._downloading)
        event.Skip()

    def _on_search_download(self, site_key):
        tab = self._tabs[site_key]
        idx = tab["results_ctrl"].GetFirstSelected()
        if idx < 0 or idx >= len(tab["results"]):
            return
        url = tab["results"][idx]["url"]
        if not url:
            self._log("Error: selected result has no URL.")
            return
        if self._downloading:
            return
        self._set_busy(True)
        self._log(f"Starting download: {url}")
        threading.Thread(
            target=self._run_download, args=(url,), daemon=True
        ).start()

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
        from .scraper import FFNScraper

        text = url.lower()
        if "ficwad.com" in text:
            return FicWadScraper()
        if "archiveofourown.org" in text or "ao3.org" in text:
            return AO3Scraper()
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

            scraper = self._scraper_for(url)

            if not is_update and scraper.is_author_url(url):
                self._run_author_download(url, scraper)
                return

            if not is_update and AO3Scraper.is_series_url(url):
                if not isinstance(scraper, AO3Scraper):
                    scraper = AO3Scraper()
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


def main():
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
