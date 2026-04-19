"""Accessible wxPython GUI for ffn-dl.

Uses native Win32 controls via wxPython so NVDA, JAWS, and other
screen readers can read every widget natively.
"""

import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import wx
import webbrowser
from collections import deque
from pathlib import Path


logger = logging.getLogger(__name__)

_LOG_FLUSH_INTERVAL_MS = 100
"""How often the UI pulls queued log lines onto the main thread."""

# In-memory log pane trims from _LOG_MAX_LINES down to _LOG_TRIM_TO_LINES
# on overflow (20% headroom). Trimming further would throw away recent
# context; trimming less would make the UI thrash as every new line
# triggers another trim. 5k lines ≈ one heavy download session.
_LOG_MAX_LINES = 5000
_LOG_TRIM_TO_LINES = 4000

# 1 MB × 3 backups — enough to catch the last handful of downloads
# when a user needs to share logs for a bug report, small enough that
# a portable zip on a flash drive doesn't balloon.
_LOG_FILE_MAX_BYTES = 1 * 1024 * 1024
_LOG_FILE_BACKUPS = 3

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class _WxLogHandler(logging.Handler):
    """Pipe Python ``logging`` records into the GUI's status pane.

    Logging can fire from worker threads (scrapers, updater, TTS),
    but wxPython widget calls have to land on the main thread —
    ``wx.CallAfter`` marshals for us. ``format()`` is called on the
    calling thread (cheap string work) so the main thread just has
    to append to the deque.
    """

    def __init__(self, target):
        super().__init__()
        self._target = target

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        try:
            wx.CallAfter(self._target, msg)
        except RuntimeError:
            # wx.App is already torn down (shutdown race). Nothing
            # sensible to do — the log line is going to the void.
            pass


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


def _wattpad_search_spec():
    from .search import WP_COMPLETED, WP_MATURE, search_wattpad
    return {
        "label": "Search Wattpad",
        "search_fn": search_wattpad,
        "filters": [
            ("&Mature:", "mature", list(WP_MATURE)),
            ("S&tatus:", "completed", list(WP_COMPLETED)),
        ],
    }


class MainFrame(wx.Frame):
    def __init__(self):
        from . import __version__
        super().__init__(
            None,
            title=f"ffn-dl {__version__} - Fanfiction Downloader",
            size=(820, 720),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        from .prefs import Prefs
        self.prefs = Prefs()
        self._downloading = False
        self._watching = False
        self._watch_seen = set()
        self._last_clip = ""
        # site_key → open SearchFrame (lazy-created on first menu invocation)
        self._search_frames = {}
        self._log_queue = deque()
        self._log_lock = threading.Lock()
        self._build_ui()
        self._load_prefs()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()
        self._start_update_check()

    def _build_ui(self):
        root = wx.Panel(self)
        self._root_panel = root
        root_sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # ── Download controls (top of frame) ─────────────────
        self._build_download_controls(root, root_sizer, pad)

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
            root,
            label=(
                "Mark scene &breaks clearly "
                "(* * * in text, a silence pause in audiobooks)"
            ),
        )
        self.hr_stars_ctrl.SetName(
            "Mark scene breaks clearly — asterisks in text output, "
            "silence pause in audiobook output"
        )
        opts2.Add(self.hr_stars_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        self.strip_notes_ctrl = wx.CheckBox(
            root, label="Strip &author's notes (A/N paragraphs)"
        )
        self.strip_notes_ctrl.SetName("Strip author's notes")
        opts2.Add(self.strip_notes_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        root_sizer.Add(opts2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        # ── Audiobook settings (visible only when Format = audio) ────
        from . import attribution as _attribution_module
        self._attribution_module = _attribution_module
        self.audio_panel = wx.Panel(root)
        audio_sizer = wx.BoxSizer(wx.HORIZONTAL)

        audio_sizer.Add(
            wx.StaticText(self.audio_panel, label="Speech &rate:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.speech_rate_ctrl = wx.SpinCtrl(
            self.audio_panel, min=-50, max=100, initial=0, size=(70, -1),
        )
        self.speech_rate_ctrl.SetName("Speech rate percent")
        self.speech_rate_ctrl.SetToolTip(
            "Integer percent delta applied to every TTS call. "
            "Example: -20 for 20% slower, +30 for 30% faster."
        )
        audio_sizer.Add(self.speech_rate_ctrl, 0, wx.RIGHT, 4)
        audio_sizer.Add(
            wx.StaticText(self.audio_panel, label="% "),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16,
        )

        audio_sizer.Add(
            wx.StaticText(self.audio_panel, label="&Attribution:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        # Friendly display labels — the backend key is the lowercase
        # first token so they round-trip cleanly.
        self._attribution_choices = list(_attribution_module.available())
        display_labels = [
            _attribution_module.BACKENDS[b]["display"]
            for b in self._attribution_choices
        ]
        self.attribution_ctrl = wx.Choice(self.audio_panel, choices=display_labels)
        self.attribution_ctrl.SetSelection(0)
        self.attribution_ctrl.SetName("Attribution backend")
        self.attribution_ctrl.Bind(wx.EVT_CHOICE, self._on_attribution_change)
        audio_sizer.Add(self.attribution_ctrl, 0, wx.RIGHT, 4)

        # Secondary dropdown for backends with size variants (BookNLP).
        # Paired with a caption StaticText so both can be hidden together.
        self.size_label = wx.StaticText(self.audio_panel, label="Si&ze:")
        audio_sizer.Add(self.size_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.attribution_size_ctrl = wx.Choice(self.audio_panel, choices=[])
        self.attribution_size_ctrl.SetName("Attribution model size")
        self.attribution_size_ctrl.Bind(wx.EVT_CHOICE, self._on_size_change)
        audio_sizer.Add(self.attribution_size_ctrl, 0, wx.RIGHT, 8)

        self.attribution_status = wx.StaticText(self.audio_panel, label="")
        self.attribution_status.SetName("Attribution status")
        audio_sizer.Add(self.attribution_status, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)

        self.attribution_install_btn = wx.Button(
            self.audio_panel, label="&Install...", size=(90, -1),
        )
        self.attribution_install_btn.Bind(wx.EVT_BUTTON, self._on_install_attribution)
        audio_sizer.Add(self.attribution_install_btn, 0)

        # Track the currently-displayed size keys so we can map the
        # Choice's selection index back to a backend-specific size name.
        self._size_keys_shown = []

        self.audio_panel.SetSizer(audio_sizer)
        root_sizer.Add(self.audio_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        self.format_ctrl.Bind(wx.EVT_CHOICE, self._on_format_change)
        self._update_audio_panel_visibility()
        self._refresh_attribution_status()

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
        # Log level, "Save log to file", and "Open log folder" live in
        # the View menu instead of cluttering the status row.
        # Backing state is held on these attributes so _apply_logging_config
        # can stay source-of-truth regardless of where the user toggled.
        self._log_level_idx = _LOG_LEVELS.index("INFO")
        self._log_to_file_enabled = False

        root_sizer.Add(
            wx.StaticText(root, label="S&tatus:"),
            0, wx.LEFT | wx.TOP | wx.RIGHT, pad,
        )

        self.log_ctrl = wx.TextCtrl(
            root,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self.log_ctrl.SetName("Status log")
        root_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        # Logging plumbing: bridge Python's root logger to _log() so
        # scraper / updater / TTS log records show up in the status pane
        # and the (optional) file, and detach on shutdown so a closed
        # app doesn't chase a dead wx.CallAfter.
        self._wx_log_handler = None
        self._file_log_handler = None

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

        # Menu bar — search sites, log controls, help. Must be built
        # after the log handlers exist (the View menu toggles them).
        self._build_menu_bar()

        # Timer for clipboard polling (2 second interval)
        self._clip_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_clip_timer, self._clip_timer)

    def _build_download_controls(self, panel, sizer, pad):
        sizer.Add(
            wx.StaticText(panel, label="Story &URL or ID:"),
            0, wx.LEFT | wx.TOP, pad,
        )
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

    # ── Helpers ───────────────────────────────────────────────

    def _log(self, msg):
        with self._log_lock:
            self._log_queue.append(msg + "\n")

    # ── Logging controls ─────────────────────────────────────

    def _log_dir(self) -> Path:
        """Directory for log files. Portable build keeps logs next to
        the exe so they travel with the install; dev/pip uses the
        same dotfile root as other ffn-dl state."""
        from . import portable
        d = portable.portable_root() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _log_file(self) -> Path:
        return self._log_dir() / "ffn-dl.log"

    def _apply_logging_config(self):
        """Reconfigure root-logger handlers from the current state.

        Called after a level change, a file-toggle, and once at
        startup after prefs load. Idempotent: detaches any handlers
        it previously attached before re-attaching fresh ones.
        Reads ``self._log_level_idx`` / ``self._log_to_file_enabled``
        rather than wx controls so it works the same whether the user
        toggled via the View menu or via loaded prefs.
        """
        root = logging.getLogger()
        level_name = _LOG_LEVELS[self._log_level_idx]
        level = getattr(logging, level_name, logging.INFO)
        root.setLevel(level)

        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

        if self._wx_log_handler is None:
            self._wx_log_handler = _WxLogHandler(self._log)
            self._wx_log_handler.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(self._wx_log_handler)
        self._wx_log_handler.setLevel(level)

        want_file = self._log_to_file_enabled
        have_file = self._file_log_handler is not None
        if want_file and not have_file:
            try:
                fh = logging.handlers.RotatingFileHandler(
                    self._log_file(),
                    maxBytes=_LOG_FILE_MAX_BYTES,
                    backupCount=_LOG_FILE_BACKUPS,
                    encoding="utf-8",
                )
                fh.setFormatter(fmt)
                fh.setLevel(level)
                root.addHandler(fh)
                self._file_log_handler = fh
                self._log(f"(Logging to {self._log_file()})")
            except OSError as exc:
                self._log(f"(Could not open log file: {exc})")
                self._log_to_file_enabled = False
                if getattr(self, "_log_to_file_item", None) is not None:
                    self._log_to_file_item.Check(False)
        elif not want_file and have_file:
            root.removeHandler(self._file_log_handler)
            try:
                self._file_log_handler.close()
            except Exception:
                pass
            self._file_log_handler = None
        elif have_file:
            self._file_log_handler.setLevel(level)

    def _detach_log_handlers(self):
        """Remove our handlers from the root logger on shutdown."""
        root = logging.getLogger()
        for attr in ("_wx_log_handler", "_file_log_handler"):
            h = getattr(self, attr, None)
            if h is None:
                continue
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
            setattr(self, attr, None)

    def _set_log_level_idx(self, idx):
        """Common path for View menu clicks and prefs-loaded startup."""
        from . import prefs as _p
        self._log_level_idx = idx
        self._apply_logging_config()
        self.prefs.set(_p.KEY_LOG_LEVEL, _LOG_LEVELS[idx])

    def _set_log_to_file(self, enabled):
        from . import prefs as _p
        self._log_to_file_enabled = bool(enabled)
        self._apply_logging_config()
        self.prefs.set_bool(_p.KEY_LOG_TO_FILE, self._log_to_file_enabled)

    def _on_open_log_folder(self, event):
        folder = self._log_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            wx.MessageBox(
                f"Could not open {folder}: {exc}",
                "Open log folder",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )

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
            # Broadcast to every open search window so their buttons
            # reflect the main frame's download state.
            for frame in list(self._search_frames.values()):
                try:
                    frame.apply_busy(busy)
                except Exception:
                    pass
        wx.CallAfter(_update)

    def _on_browse(self, event):
        dlg = wx.DirDialog(
            self, "Choose output folder",
            defaultPath=self.output_ctrl.GetValue(),
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.output_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    # ── Audiobook settings ──────────────────────────────────

    def _on_format_change(self, event):
        self._update_audio_panel_visibility()

    def _update_audio_panel_visibility(self):
        is_audio = (
            self.format_ctrl.GetString(self.format_ctrl.GetSelection()) == "audio"
        )
        self.audio_panel.Show(is_audio)
        self.audio_panel.GetContainingSizer().Layout()
        self.Layout()

    def _selected_attribution_backend(self):
        idx = self.attribution_ctrl.GetSelection()
        if idx < 0 or idx >= len(self._attribution_choices):
            return "builtin"
        return self._attribution_choices[idx]

    def _refresh_attribution_status(self):
        backend = self._selected_attribution_backend()
        if backend == "builtin":
            self.attribution_status.SetLabel("(built-in)")
            self.attribution_install_btn.Enable(False)
            self.attribution_install_btn.SetLabel("&Install...")
            return
        reason = self._attribution_module.install_unsupported_reason(backend)
        if reason:
            self.attribution_status.SetLabel("(install unsupported)")
            self.attribution_install_btn.Enable(False)
            self.attribution_install_btn.SetLabel("&Install...")
            return
        if self._attribution_module.is_installed(backend):
            self.attribution_status.SetLabel("(installed)")
            self.attribution_install_btn.Enable(True)
            self.attribution_install_btn.SetLabel("Re&install...")
        else:
            self.attribution_status.SetLabel("(not installed)")
            self.attribution_install_btn.Enable(True)
            self.attribution_install_btn.SetLabel("&Install...")

    def _on_attribution_change(self, event):
        self._refresh_attribution_status()
        self._refresh_size_choices()
        backend = self._selected_attribution_backend()
        if backend == "builtin":
            return
        reason = self._attribution_module.install_unsupported_reason(backend)
        if reason:
            # Frozen .exe — deliver the explanation once, cleanly.
            for line in reason.splitlines():
                self._log(line)
            return
        if not self._attribution_module.is_installed(backend):
            self._log(
                f"Attribution backend '{backend}' is not installed. "
                f"Click Install or run: ffn-dl --install-attribution {backend}"
            )

    def _on_size_change(self, event):
        # Purely cosmetic — value is read on demand via _selected_size().
        pass

    def _refresh_size_choices(self, preferred=None):
        """Populate the size dropdown from the selected backend's sizes
        registry. Hides the size row entirely when the backend offers
        no size variants. `preferred` lets callers (e.g. prefs load)
        force a specific option if it exists in the new size list."""
        backend = self._selected_attribution_backend()
        sizes = self._attribution_module.sizes_for(backend) or {}
        if not sizes:
            self._size_keys_shown = []
            self.attribution_size_ctrl.Clear()
            self.size_label.Hide()
            self.attribution_size_ctrl.Hide()
            self.audio_panel.Layout()
            return

        keys = list(sizes.keys())
        labels = [sizes[k]["display"] for k in keys]
        self._size_keys_shown = keys
        self.attribution_size_ctrl.Set(labels)
        default = preferred if preferred in keys else self._attribution_module.default_size(backend)
        if default in keys:
            self.attribution_size_ctrl.SetSelection(keys.index(default))
        else:
            self.attribution_size_ctrl.SetSelection(0)
        self.size_label.Show()
        self.attribution_size_ctrl.Show()
        self.audio_panel.Layout()

    def _selected_size(self):
        """Return the backend-specific size key (e.g. 'small', 'big')
        or None if the current backend has no size variants."""
        if not self._size_keys_shown:
            return None
        idx = self.attribution_size_ctrl.GetSelection()
        if idx < 0 or idx >= len(self._size_keys_shown):
            return self._attribution_module.default_size(
                self._selected_attribution_backend()
            )
        return self._size_keys_shown[idx]

    def _on_install_attribution(self, event):
        backend = self._selected_attribution_backend()
        if backend == "builtin":
            return
        info = self._attribution_module.BACKENDS[backend]
        size = info.get("size_hint", "?")
        # In the frozen .exe we warn about the total on-disk cost
        # (embedded Python + torch + package), which is far bigger
        # than the "backend size" alone.
        import sys as _sys
        frozen = bool(getattr(_sys, "frozen", False))
        if frozen:
            footprint = (
                "Downloads ~10 MB of embedded Python on first run, then "
                "pulls torch + transformers (~300 MB) and the model "
                "package (~90 MB for fastcoref, ~150 MB for BookNLP). "
                "Everything lives in %LOCALAPPDATA%\\ffn-dl\\neural\\."
            )
        else:
            footprint = f"Download size: {size}. This runs `pip install {info['pip_name']}`."
        msg = (
            f"Install '{backend}'?\n\n"
            f"{info.get('description', '')}\n\n"
            f"{footprint}"
        )
        if wx.MessageBox(msg, "Confirm install", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return

        self._log(f"\nInstalling {backend} in the background...")
        self.attribution_install_btn.Enable(False)
        self.attribution_status.SetLabel("(installing...)")

        def run():
            def cb(line):
                # Marshal log lines back to the main thread.
                wx.CallAfter(self._log, line)
            ok = self._attribution_module.install(backend, log_callback=cb)
            wx.CallAfter(self._after_install, backend, ok, frozen)

        threading.Thread(target=run, daemon=True).start()

    def _after_install(self, backend, ok, frozen):
        if ok:
            self._log(f"Installed {backend} successfully.")
            if frozen:
                # A .pth-using package like torch usually needs a fresh
                # interpreter to import cleanly. Don't try to hot-load;
                # prompt for a restart.
                wx.MessageBox(
                    f"{backend} was installed successfully.\n\n"
                    "Please restart ffn-dl so the new modules are "
                    "loaded before you generate an audiobook.",
                    "Restart required",
                    wx.OK | wx.ICON_INFORMATION,
                )
        else:
            self._log(f"Install of {backend} failed — see log above for pip output.")
        self._refresh_attribution_status()

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

        try:
            rate = int(self.prefs.get(_p.KEY_SPEECH_RATE) or 0)
        except (TypeError, ValueError):
            rate = 0
        self.speech_rate_ctrl.SetValue(max(-50, min(100, rate)))

        backend = self.prefs.get(_p.KEY_ATTRIBUTION_BACKEND) or "builtin"
        if backend in self._attribution_choices:
            self.attribution_ctrl.SetSelection(
                self._attribution_choices.index(backend)
            )
        saved_size = self.prefs.get(_p.KEY_ATTRIBUTION_MODEL_SIZE) or None
        self._refresh_attribution_status()
        self._refresh_size_choices(preferred=saved_size)
        self._update_audio_panel_visibility()

        level = (self.prefs.get(_p.KEY_LOG_LEVEL) or "INFO").upper()
        if level in _LOG_LEVELS:
            self._log_level_idx = _LOG_LEVELS.index(level)
        self._log_to_file_enabled = self.prefs.get_bool(_p.KEY_LOG_TO_FILE)
        self._apply_logging_config()
        # Sync the View-menu radio/check items to match the restored state.
        for lvl_name, item in getattr(self, "_log_level_items", {}).items():
            item.Check(lvl_name == _LOG_LEVELS[self._log_level_idx])
        if getattr(self, "_log_to_file_item", None) is not None:
            self._log_to_file_item.Check(self._log_to_file_enabled)

        # Search-state prefs are loaded lazily by each SearchFrame on
        # the first Ctrl+N / menu open — not here.

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
        self.prefs.set(_p.KEY_SPEECH_RATE, self.speech_rate_ctrl.GetValue())
        self.prefs.set(_p.KEY_ATTRIBUTION_BACKEND, self._selected_attribution_backend())
        self.prefs.set(_p.KEY_ATTRIBUTION_MODEL_SIZE, self._selected_size() or "")
        self.prefs.set(_p.KEY_LOG_LEVEL, _LOG_LEVELS[self._log_level_idx])
        self.prefs.set_bool(_p.KEY_LOG_TO_FILE, self._log_to_file_enabled)

        # Let any open search frames snapshot their own state to prefs.
        for frame in list(self._search_frames.values()):
            try:
                frame.save_state()
            except (RuntimeError, AttributeError, OSError):
                logger.debug("save_state on search frame failed", exc_info=True)

    def _on_close(self, event):
        # Snapshot each open search frame's state to prefs, then destroy
        # the frames. Destroy() doesn't fire EVT_CLOSE, so the explicit
        # save_state call is the only thing persisting their filters.
        for frame in list(self._search_frames.values()):
            try:
                frame.save_state()
            except (RuntimeError, AttributeError, OSError):
                logger.debug("save_state on close failed", exc_info=True)
            try:
                frame.Destroy()
            except (RuntimeError, AttributeError):
                logger.debug("frame.Destroy on close failed", exc_info=True)
        self._search_frames.clear()
        try:
            self._save_prefs()
        except (RuntimeError, OSError):
            logger.debug("_save_prefs on close failed", exc_info=True)
        if hasattr(self, "_log_timer"):
            self._log_timer.Stop()
        self._detach_log_handlers()
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
            # Route to the file logger too — the GUI panel is gone the
            # moment the user closes the window, so a pane-only message
            # leaves no trail to debug curl/TLS/rate-limit failures from.
            logger.warning("Update check failed", exc_info=True)
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
        progress.Destroy()
        wx.MessageBox(
            f"Updated to {tag}. The app will now close and reopen "
            f"automatically once the new files are in place.",
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
        self._detach_log_handlers()
        # ZipExtractor.exe has already been spawned by
        # download_and_replace; it's blocked on our PID. Exiting releases
        # its WaitForExit(), after which it overwrites the install and
        # relaunches ffn-dl.exe itself — do NOT call self_update.restart()
        # here, that would race the helper's relaunch.
        sys.exit(0)

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

    # ── Search frames (opened via Search menu or Ctrl+1..5) ──

    def _open_search_frame(self, site_key, spec):
        """Pop up a non-modal search window for one site. Reuses the
        existing frame if already open so Ctrl+N doesn't spawn duplicates.
        """
        frame = self._search_frames.get(site_key)
        if frame is not None:
            try:
                frame.Raise()
                frame.SetFocus()
                return
            except RuntimeError:
                # Frame was destroyed without unregistering (shouldn't
                # happen, but don't crash if it did).
                self._search_frames.pop(site_key, None)
        frame = SearchFrame(self, site_key, spec)
        self._search_frames[site_key] = frame
        frame.Show()
        frame.Raise()

    def _notify_search_frame_closed(self, site_key):
        self._search_frames.pop(site_key, None)

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

        from .sites import extract_story_url
        url = extract_story_url(clip)
        if not url:
            return
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
        from .sites import detect_scraper
        return detect_scraper(url)()

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

            backend = self._selected_attribution_backend()
            size = self._selected_size()
            rate = self.speech_rate_ctrl.GetValue()
            size_note = f", size={size}" if size else ""
            self._log(
                f"\nGenerating audiobook (attribution={backend}{size_note}, rate={rate:+d}%)..."
            )
            return generate_audiobook(
                story, output_dir,
                progress_callback=audio_progress,
                speech_rate=rate,
                attribution_backend=backend,
                attribution_model_size=size,
                strip_notes=strip_notes,
                hr_as_stars=hr_as_stars,
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
        dlg = StoryPickerDialog(self, title, works, prefs=self.prefs)
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

    # ── Menu bar ──────────────────────────────────────────────

    _SEARCH_MENU_ITEMS = (
        # (accel, site_key, spec_fn, menu_label)
        ("Ctrl+1", "ffn", _ffn_search_spec, "Search &FFN..."),
        ("Ctrl+2", "ao3", _ao3_search_spec, "Search &AO3..."),
        ("Ctrl+3", "royalroad", _royalroad_search_spec, "Search &Royal Road..."),
        ("Ctrl+4", "literotica", _literotica_search_spec, "Search &Literotica..."),
        ("Ctrl+5", "wattpad", _wattpad_search_spec, "Search &Wattpad..."),
    )

    def _build_menu_bar(self):
        bar = wx.MenuBar()

        file_menu = wx.Menu()
        exit_item = file_menu.Append(wx.ID_EXIT, "E&xit")
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), exit_item)
        bar.Append(file_menu, "&File")

        search_menu = wx.Menu()
        for accel, site_key, spec_fn, label in self._SEARCH_MENU_ITEMS:
            item = search_menu.Append(wx.ID_ANY, f"{label}\t{accel}")
            # Closure captures site_key / spec_fn, not the loop variables.
            self.Bind(
                wx.EVT_MENU,
                lambda evt, k=site_key, s=spec_fn:
                    self._open_search_frame(k, s()),
                item,
            )
        bar.Append(search_menu, "&Search")

        library_menu = wx.Menu()
        library_item = library_menu.Append(
            wx.ID_ANY, "&Library...\tCtrl+L",
        )
        self.Bind(wx.EVT_MENU, self._on_library_menu, library_item)
        bar.Append(library_menu, "&Library")

        view_menu = wx.Menu()
        log_submenu = wx.Menu()
        self._log_level_items = {}
        for lvl in _LOG_LEVELS:
            item = log_submenu.AppendRadioItem(wx.ID_ANY, lvl)
            self._log_level_items[lvl] = item
            self.Bind(
                wx.EVT_MENU,
                lambda evt, name=lvl: self._on_log_level_menu(name),
                item,
            )
        view_menu.AppendSubMenu(log_submenu, "Log &Level")
        self._log_to_file_item = view_menu.AppendCheckItem(
            wx.ID_ANY, "&Save log to file",
        )
        self.Bind(wx.EVT_MENU, self._on_log_to_file_menu, self._log_to_file_item)
        view_menu.AppendSeparator()
        open_log = view_menu.Append(wx.ID_ANY, "&Open log folder")
        self.Bind(wx.EVT_MENU, self._on_open_log_folder, open_log)
        bar.Append(view_menu, "&View")

        help_menu = wx.Menu()
        manual_item = help_menu.Append(wx.ID_HELP, "Read the &Manual\tF1")
        self.Bind(wx.EVT_MENU, self._on_open_manual, manual_item)
        check_item = help_menu.Append(wx.ID_ANY, "&Check for Updates...")
        self.Bind(wx.EVT_MENU, self._on_check_updates_menu, check_item)
        help_menu.AppendSeparator()
        about_item = help_menu.Append(wx.ID_ABOUT, "&About ffn-dl")
        self.Bind(wx.EVT_MENU, self._on_about, about_item)
        bar.Append(help_menu, "&Help")

        self.SetMenuBar(bar)

        # Reflect current log-level / log-to-file state. _load_prefs runs
        # after _build_ui and will re-sync these once prefs are read.
        current_level = _LOG_LEVELS[self._log_level_idx]
        self._log_level_items[current_level].Check(True)
        self._log_to_file_item.Check(self._log_to_file_enabled)

    def _on_log_level_menu(self, level_name):
        if level_name in _LOG_LEVELS:
            self._set_log_level_idx(_LOG_LEVELS.index(level_name))

    def _on_log_to_file_menu(self, event):
        self._set_log_to_file(self._log_to_file_item.IsChecked())

    def _on_library_menu(self, event):
        """Open the library-management dialog.

        Lazy import keeps gui.py's startup cost unaffected for users
        who never touch the library features.
        """
        from .library.gui import LibraryDialog

        dlg = LibraryDialog(self, self.prefs)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _on_check_updates_menu(self, event):
        """Manual trigger: unlike the silent launch check, this surfaces
        the 'no update available' case so the user sees their click did
        something.
        """
        from . import self_update
        self._log("Checking for updates...")

        def worker():
            try:
                info = self_update.check_for_update()
            except Exception as exc:
                logger.warning("Update check failed", exc_info=True)
                wx.CallAfter(self._log, f"Update check failed: {exc}")
                wx.CallAfter(
                    wx.MessageBox,
                    f"Update check failed:\n\n{exc}",
                    "Check for Updates",
                    wx.OK | wx.ICON_WARNING, self,
                )
                return
            if info is None:
                wx.CallAfter(self._log, "You have the latest version.")
                wx.CallAfter(
                    wx.MessageBox,
                    "You have the latest version of ffn-dl.",
                    "Check for Updates",
                    wx.OK | wx.ICON_INFORMATION, self,
                )
                return
            # User asked explicitly — clear any previously-skipped version.
            from . import prefs as _p
            self.prefs.set(_p.KEY_SKIPPED_VERSION, "")
            wx.CallAfter(self._prompt_update, info)

        threading.Thread(target=worker, daemon=True).start()

    def _on_about(self, event):
        import wx.adv
        from . import __version__
        info = wx.adv.AboutDialogInfo()
        info.SetName("ffn-dl")
        info.SetVersion(__version__)
        info.SetDescription(
            "Cross-platform fanfiction downloader.\n\n"
            "Supports FanFiction.Net, Archive of Our Own, FicWad, "
            "Royal Road, MediaMiner, Literotica, and Wattpad. "
            "Exports to EPUB, HTML, TXT, and character-voiced audiobooks."
        )
        info.SetWebSite("https://github.com/matalvernaz/ffn-dl")
        info.SetCopyright("(c) Matthew Alvernaz")
        wx.adv.AboutBox(info, self)

    def _on_open_manual(self, event):
        """Open the project README (the user-facing manual) in the browser."""
        webbrowser.open("https://github.com/matalvernaz/ffn-dl#readme")


class SearchFrame(wx.Frame):
    """Non-modal per-site search window.

    Opened via the Search menu (Ctrl+1..5). Stays open alongside the
    main frame so the user can keep one window per site up at once and
    leave filter state in place while downloads run in the background.

    "Download Selected" / "Show Parts" push work back into the main
    frame's download pipeline, which owns the format, output folder,
    and audio settings.
    """

    _SITE_LABELS = {
        "ffn": "FFN",
        "ao3": "AO3",
        "royalroad": "Royal Road",
        "literotica": "Literotica",
        "wattpad": "Wattpad",
    }

    _PREF_KEY_BY_SITE = {
        "ffn": "search_state_ffn",
        "ao3": "search_state_ao3",
        "royalroad": "search_state_royalroad",
        "literotica": "search_state_literotica",
        "wattpad": "search_state_wattpad",
    }

    def __init__(self, main_frame, site_key, spec):
        super().__init__(
            main_frame,
            title=spec["label"],
            size=(820, 640),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.main_frame = main_frame
        self.site_key = site_key
        self.spec = spec
        self.search_fn = spec["search_fn"]
        self.filter_ctrls = {}
        self.text_ctrls = {}
        self.checkbox_ctrls = {}
        self.results = []
        self._raw_results = []
        self.next_page = 1
        self.last_query = None
        self.last_filters = {}

        self._build_ui()
        self._load_state()
        self.apply_busy(bool(self.main_frame._downloading))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # Query row
        q_row = wx.BoxSizer(wx.HORIZONTAL)
        q_row.Add(
            wx.StaticText(panel, label="&Query:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.query_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.query_ctrl.SetName(f"{self.spec['label']} query")
        self.query_ctrl.Bind(wx.EVT_TEXT_ENTER, lambda e: self._on_search())
        q_row.Add(self.query_ctrl, 1, wx.RIGHT, 4)

        self.search_btn = wx.Button(panel, label="S&earch")
        self.search_btn.Bind(wx.EVT_BUTTON, lambda e: self._on_search())
        q_row.Add(self.search_btn, 0)
        sizer.Add(q_row, 0, wx.EXPAND | wx.ALL, pad)

        # Choice filters
        if self.spec.get("filters"):
            fgrid = wx.FlexGridSizer(rows=0, cols=8, hgap=4, vgap=4)
            for label, key, choices in self.spec["filters"]:
                fgrid.Add(
                    wx.StaticText(panel, label=label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.Choice(panel, choices=choices)
                ctrl.SetSelection(0)
                ctrl.SetName(label.replace("&", "").rstrip(":"))
                fgrid.Add(ctrl, 0, wx.RIGHT, 12)
                self.filter_ctrls[key] = ctrl
            sizer.Add(fgrid, 0, wx.EXPAND | wx.ALL, pad)

        # Free-text filters
        if self.spec.get("text_filters"):
            tgrid = wx.FlexGridSizer(rows=0, cols=4, hgap=4, vgap=4)
            for label, key in self.spec["text_filters"]:
                tgrid.Add(
                    wx.StaticText(panel, label=label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.TextCtrl(panel, size=(140, -1))
                ctrl.SetName(label.replace("&", "").rstrip(":"))
                tgrid.Add(ctrl, 0, wx.RIGHT, 12)
                self.text_ctrls[key] = ctrl
            sizer.Add(tgrid, 0, wx.EXPAND | wx.ALL, pad)

        # Multi-pickers (checkable-list dialogs for tags/genres/warnings)
        if self.spec.get("multi_pickers"):
            for mp_label, mp_key, mp_title, mp_options in self.spec["multi_pickers"]:
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
                self.text_ctrls[mp_key] = ctrl

        # Checkboxes
        if self.spec.get("checkboxes"):
            cb_row = wx.BoxSizer(wx.HORIZONTAL)
            for label, key in self.spec["checkboxes"]:
                ctrl = wx.CheckBox(panel, label=label)
                cb_row.Add(ctrl, 0, wx.RIGHT, 16)
                self.checkbox_ctrls[key] = ctrl
            sizer.Add(cb_row, 0, wx.EXPAND | wx.ALL, pad)

        # Results list
        sizer.Add(
            wx.StaticText(panel, label="&Results:"),
            0, wx.LEFT | wx.TOP, pad,
        )
        self.results_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.results_ctrl.SetName(f"{self.spec['label']} results")
        for i, (col_label, width) in enumerate(_SEARCH_COLUMNS):
            self.results_ctrl.InsertColumn(i, col_label, width=width)
        self.results_ctrl.Bind(
            wx.EVT_LIST_ITEM_SELECTED, self._on_result_select,
        )
        self.results_ctrl.Bind(
            wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_result_activated(),
        )
        sizer.Add(self.results_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        # Summary
        sizer.Add(
            wx.StaticText(panel, label="S&ummary:"),
            0, wx.LEFT | wx.TOP, pad,
        )
        self.summary_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 70),
        )
        self.summary_ctrl.SetName(f"{self.spec['label']} summary")
        sizer.Add(self.summary_ctrl, 0, wx.EXPAND | wx.ALL, pad)

        dl_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_dl_btn = wx.Button(panel, label="Do&wnload Selected")
        self.search_dl_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_search_download(),
        )
        self.search_dl_btn.Disable()
        dl_row.Add(self.search_dl_btn, 0, wx.RIGHT, 8)

        self.show_parts_btn = wx.Button(panel, label="Show &Parts...")
        self.show_parts_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_show_parts(),
        )
        self.show_parts_btn.Disable()
        dl_row.Add(self.show_parts_btn, 0, wx.RIGHT, 8)

        self.load_more_btn = wx.Button(panel, label="Load &More")
        self.load_more_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_load_more(),
        )
        self.load_more_btn.Disable()
        dl_row.Add(self.load_more_btn, 0)
        sizer.Add(dl_row, 0, wx.ALL, pad)

        panel.SetSizer(sizer)

    # ── Delegates ─────────────────────────────────────────────

    def _log(self, msg):
        self.main_frame._log(msg)

    # ── State persistence ─────────────────────────────────────

    def _load_state(self):
        raw = self.main_frame.prefs.get(self._PREF_KEY_BY_SITE[self.site_key])
        if not raw:
            return
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(state, dict):
            return
        # Ignore any legacy "query" a previous version wrote — query is
        # intentionally not persisted.
        for key, value in (state.get("filters") or {}).items():
            ctrl = self.filter_ctrls.get(key)
            if ctrl and isinstance(value, str) and value:
                ctrl.SetStringSelection(value)
        for key, value in (state.get("text") or {}).items():
            ctrl = self.text_ctrls.get(key)
            if ctrl and isinstance(value, str):
                ctrl.SetValue(value)
        for key, value in (state.get("checks") or {}).items():
            ctrl = self.checkbox_ctrls.get(key)
            if ctrl is not None:
                ctrl.SetValue(bool(value))

    def save_state(self):
        state = {
            "filters": {
                key: ctrl.GetStringSelection()
                for key, ctrl in self.filter_ctrls.items()
            },
            "text": {
                key: ctrl.GetValue()
                for key, ctrl in self.text_ctrls.items()
            },
            "checks": {
                key: bool(ctrl.GetValue())
                for key, ctrl in self.checkbox_ctrls.items()
            },
        }
        self.main_frame.prefs.set(
            self._PREF_KEY_BY_SITE[self.site_key], json.dumps(state),
        )

    # ── Busy state, driven from MainFrame._set_busy ──────────

    def apply_busy(self, busy):
        self.search_btn.Enable(not busy)
        has_selection = self.results_ctrl.GetFirstSelected() != -1
        selected_is_series = False
        if has_selection:
            idx = self.results_ctrl.GetFirstSelected()
            if 0 <= idx < len(self.results):
                selected_is_series = bool(
                    self.results[idx].get("is_series")
                )
        self.search_dl_btn.Enable(not busy and has_selection)
        self.show_parts_btn.Enable(
            not busy and has_selection and selected_is_series
        )
        self.load_more_btn.Enable(not busy and self.last_query is not None)

    # ── Multi-picker ──────────────────────────────────────────

    def _open_multi_picker(self, ctrl, title, options):
        current = [
            s.strip() for s in ctrl.GetValue().split(",") if s.strip()
        ]
        dlg = MultiPickerDialog(self, title, list(options), initial=current)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                ctrl.SetValue(", ".join(dlg.picked_labels()))
        finally:
            dlg.Destroy()

    # ── Search ────────────────────────────────────────────────

    def _collect_filters(self):
        filters = {}
        for key, ctrl in self.filter_ctrls.items():
            idx = ctrl.GetSelection()
            if idx <= 0:
                # First entry is always "any"/"all"/"best match" — no filter
                continue
            filters[key] = ctrl.GetString(idx)
        for key, ctrl in self.text_ctrls.items():
            value = ctrl.GetValue().strip()
            if value:
                filters[key] = value
        for key, ctrl in self.checkbox_ctrls.items():
            if ctrl.GetValue():
                filters[key] = True
        return filters

    def _on_search(self):
        query = self.query_ctrl.GetValue().strip()
        if self.main_frame._downloading:
            return
        filters = self._collect_filters()
        # Most searches need a free-text query, but several site/filter
        # combinations are valid without one:
        #   • RR list browse (Rising Stars, Best Rated, …)
        #   • RR filter-only browse (tags, genres, warnings, numeric bounds)
        #   • Literotica category browse — the category slug IS the target.
        list_browse = (
            self.site_key == "royalroad"
            and filters.get("list")
            and filters["list"].strip().lower() != "search"
        )
        rr_filter_only = (
            self.site_key == "royalroad"
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
            self.site_key == "literotica" and filters.get("category")
        )
        if not query and not (list_browse or rr_filter_only or lit_cat_browse):
            self._log("Error: Please enter a search query.")
            return
        self.main_frame._set_busy(True)
        self.results_ctrl.DeleteAllItems()
        self.summary_ctrl.SetValue("")
        self.results = []
        self._raw_results = []
        self.next_page = 1
        self.last_query = query
        self.last_filters = filters
        filter_str = (
            " [" + ", ".join(f"{k}={v}" for k, v in filters.items()) + "]"
            if filters else ""
        )
        site_label = self._SITE_LABELS.get(self.site_key, self.site_key)
        self._log(f"Searching {site_label} for: {query}{filter_str}")
        threading.Thread(
            target=self._run_search,
            args=(query, filters, 1, False),
            daemon=True,
        ).start()

    def _on_load_more(self):
        if self.main_frame._downloading or self.last_query is None:
            return
        self.main_frame._set_busy(True)
        self._log(f"Loading page {self.next_page}...")
        threading.Thread(
            target=self._run_search,
            args=(self.last_query, self.last_filters, self.next_page, True),
            daemon=True,
        ).start()

    def _run_search(self, query, filters, page, append):
        from .search import fetch_until_limit
        try:
            page_results, next_page = fetch_until_limit(
                self.search_fn, query,
                limit=25, start_page=page, **filters,
            )
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._log(f"Search error: {e}")
            self._log(tb.rstrip())
            wx.CallAfter(
                wx.MessageBox,
                f"Search failed:\n\n{e}",
                "Search Error",
                wx.OK | wx.ICON_ERROR, self,
            )
            self.main_frame._set_busy(False)
            return
        wx.CallAfter(self._populate_results, page_results, next_page, append)
        self.main_frame._set_busy(False)

    def _populate_results(self, new_results, next_page, append):
        from .search import collapse_ao3_series, collapse_literotica_series

        # Keep the raw (uncollapsed) results across load-more so we can
        # re-run collapse on the full set — otherwise parts of the same
        # series that span page boundaries never find each other.
        if append:
            raw = list(self._raw_results or []) + list(new_results)
        else:
            raw = list(new_results)
        self._raw_results = raw

        if self.site_key == "ao3":
            processed = collapse_ao3_series(raw)
        elif self.site_key == "literotica":
            processed = collapse_literotica_series(raw)
        else:
            processed = list(raw)

        previous_count = len(self.results) if append else 0
        self.results = processed
        self.next_page = next_page

        ctrl = self.results_ctrl
        ctrl.Freeze()
        try:
            ctrl.DeleteAllItems()
            for r in self.results:
                row = ctrl.InsertItem(
                    ctrl.GetItemCount(), self._result_title(r),
                )
                ctrl.SetItem(row, 1, r.get("author", "") or "")
                ctrl.SetItem(row, 2, r.get("fandom", "") or "")
                ctrl.SetItem(row, 3, str(r.get("words", "")))
                ctrl.SetItem(row, 4, str(r.get("chapters", "")))
                ctrl.SetItem(row, 5, r.get("rating", "") or "")
                ctrl.SetItem(row, 6, r.get("status", "") or "")
        finally:
            ctrl.Thaw()

        self.load_more_btn.Enable(
            bool(new_results) and not self.main_frame._downloading
        )
        if not self.results:
            self._log(
                "No results found." if not append else "No more results."
            )
            return

        if append:
            added = len(self.results) - previous_count
            focus_row = previous_count if added > 0 else 0
            self._log(
                f"Loaded more. Total {len(self.results)} rows "
                f"(+{max(added, 0)})."
                if added > 0 else "No more results."
            )
        else:
            focus_row = 0
            self._log(f"Found {len(self.results)} results.")

        ctrl.SetFocus()
        ctrl.Focus(focus_row)
        ctrl.Select(focus_row)

    @staticmethod
    def _result_title(r):
        if r.get("is_series"):
            parts = len(r.get("series_parts") or [])
            return f"[Series · {parts} part(s)] {r['title']}"
        return r.get("title", "")

    def _on_result_select(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self.results):
            r = self.results[idx]
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
                self.summary_ctrl.SetValue(preview.strip())
                self.show_parts_btn.Enable(bool(parts))
            else:
                self.summary_ctrl.SetValue(summary or "(no summary)")
                self.show_parts_btn.Disable()
            self.search_dl_btn.Enable(not self.main_frame._downloading)
        event.Skip()

    def _on_search_download(self):
        idx = self.results_ctrl.GetFirstSelected()
        if idx < 0 or idx >= len(self.results):
            return
        picked = self.results[idx]
        url = picked.get("url")
        if not url:
            self._log("Error: selected result has no URL.")
            return
        if self.main_frame._downloading:
            return
        self.main_frame._set_busy(True)
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
                    target=self.main_frame._run_series_merge_download,
                    args=(url,),
                    kwargs={
                        "series_name": series_name,
                        "part_urls": part_urls,
                    },
                    daemon=True,
                ).start()
            else:
                threading.Thread(
                    target=self.main_frame._run_series_merge_download,
                    args=(url,), daemon=True,
                ).start()
        else:
            self._log(f"Starting download: {url}")
            threading.Thread(
                target=self.main_frame._run_download,
                args=(url,), daemon=True,
            ).start()

    def _on_result_activated(self):
        # Enter/double-click: series rows open the parts dialog so
        # keyboard-only users can actually see what's inside the series
        # instead of blindly kicking off a multi-part merge.
        idx = self.results_ctrl.GetFirstSelected()
        if 0 <= idx < len(self.results):
            if self.results[idx].get("is_series"):
                self._on_show_parts()
                return
        self._on_search_download()

    def _on_show_parts(self):
        idx = self.results_ctrl.GetFirstSelected()
        if idx < 0 or idx >= len(self.results):
            return
        row = self.results[idx]
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
            if picked and not self.main_frame._downloading:
                self.main_frame._set_busy(True)
                self._log(f"Starting part download: {picked}")
                threading.Thread(
                    target=self.main_frame._run_download,
                    args=(picked,), daemon=True,
                ).start()
        dlg.Destroy()

    # ── Close ─────────────────────────────────────────────────

    def _on_close(self, event):
        try:
            self.save_state()
        except Exception:
            pass
        try:
            self.main_frame._notify_search_frame_closed(self.site_key)
        except Exception:
            pass
        event.Skip()


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

    def __init__(self, parent, title, works, prefs=None):
        super().__init__(
            parent, title=title,
            size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._works = list(works)
        self._order = list(range(len(self._works)))
        self._prefs = prefs
        self._sort_key = self._load_saved_sort_key()
        self._section_filter = "all"
        self._picked = []
        self._apply_sort()
        self._build_ui()

    def _load_saved_sort_key(self):
        if self._prefs is None:
            return None
        from .prefs import KEY_STORY_PICKER_SORT
        saved = self._prefs.get(KEY_STORY_PICKER_SORT, "")
        if not saved:
            return None
        valid_keys = {key for _, key in self._SORT_OPTIONS if key is not None}
        return saved if saved in valid_keys else None

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
        initial_idx = next(
            (i for i, (_, k) in enumerate(self._SORT_OPTIONS) if k == self._sort_key),
            0,
        )
        self.sort_ctrl.SetSelection(initial_idx)
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
        if self._prefs is not None:
            from .prefs import KEY_STORY_PICKER_SORT
            self._prefs.set(KEY_STORY_PICKER_SORT, key or "")
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
