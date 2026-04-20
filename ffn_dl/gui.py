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


from .gui_dialogs import (
    MultiPickerDialog,
    SeriesPartsDialog,
    StoryPickerDialog,
    VoicePreviewDialog,
)
from .gui_search import (
    SearchFrame,
    _ao3_search_spec,
    _ffn_search_spec,
    _literotica_search_spec,
    _royalroad_search_spec,
    _wattpad_search_spec,
)


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
        self._busy_kind = None
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
        self._start_watchlist_poller()

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

        # Cap third-party loggers at INFO even when the user picks DEBUG.
        # Without this, 90%+ of a DEBUG log is HF filelock polling,
        # httpcore/httpx request tracing from BookNLP's model fetch, and
        # asyncio proactor churn — none of it ffn-dl's own output, and
        # it makes real diagnosis painful because the signal drowns.
        noisy_level = max(level, logging.INFO)
        for noisy in (
            "filelock", "asyncio",
            "urllib3", "httpcore", "httpcore.http11", "httpcore.connection",
            "httpx", "h5py._conv",
        ):
            logging.getLogger(noisy).setLevel(noisy_level)

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

    def _set_busy(self, busy, kind=None):
        """Toggle the global busy flag and optionally tag what's running.

        ``kind`` is one of ``"download"``, ``"preview"``, ``"search"``
        (or ``None`` when clearing). It drives the close-confirmation
        prompt's message so users see *what* they're cancelling, not a
        generic "work in progress" banner.
        """
        def _update():
            self._downloading = busy
            self._busy_kind = kind if busy else None
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
        if getattr(self, "_confirm_close_item", None) is not None:
            self._confirm_close_item.Check(
                self.prefs.get_bool(_p.KEY_CONFIRM_CANCEL_ON_CLOSE)
            )

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
        # If a background job is still running, closing the window
        # silently cancels it — which has bitten users mid-audiobook
        # more than once. Prompt first, with a "Don't ask again"
        # checkbox that flips the pref off for users who'd rather not
        # see it. Veto the close on No; event.Veto() stops Wx from
        # tearing down the frame.
        if self._downloading and self._should_confirm_close():
            if not self._confirm_close_during_busy():
                event.Veto()
                return

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
        if getattr(self, "_watchlist_poller", None) is not None:
            self._watchlist_poller.stop()
        try:
            self._save_prefs()
        except (RuntimeError, OSError):
            logger.debug("_save_prefs on close failed", exc_info=True)
        if hasattr(self, "_log_timer"):
            self._log_timer.Stop()
        self._detach_log_handlers()
        event.Skip()

    def _should_confirm_close(self):
        from . import prefs as _p
        return self.prefs.get_bool(_p.KEY_CONFIRM_CANCEL_ON_CLOSE)

    def _confirm_close_during_busy(self):
        """Show the close-cancel confirmation. Returns True if the user
        wants to proceed with closing (cancelling the job), False to
        keep the window open.
        """
        from . import prefs as _p

        kind = self._busy_kind
        if kind == "preview":
            title = "Voice preview in progress"
            body = (
                "A voice preview is still fetching chapter data.\n\n"
                "Close ffn-dl and cancel the preview? "
                "Cached chapters are kept either way."
            )
        elif kind == "search":
            title = "Search in progress"
            body = (
                "A search is still running.\n\n"
                "Close ffn-dl and cancel the search?"
            )
        else:
            # Default covers "download" and any unexpected value.
            # Mention audiobooks explicitly because losing a half-built
            # M4B after 30+ minutes of TTS synthesis is the worst-case
            # scenario this prompt exists to prevent.
            is_audio = False
            try:
                is_audio = (
                    self.format_ctrl.GetString(
                        self.format_ctrl.GetSelection()
                    ) == "audio"
                )
            except (RuntimeError, AttributeError):
                pass
            if is_audio:
                title = "Audiobook generation in progress"
                body = (
                    "An audiobook is still being built.\n\n"
                    "Close ffn-dl and cancel it? "
                    "Downloaded chapters stay cached, but any audio "
                    "synthesised so far will be discarded."
                )
            else:
                title = "Download in progress"
                body = (
                    "A download is still running.\n\n"
                    "Close ffn-dl and cancel it? "
                    "Chapters already fetched stay cached and will "
                    "not need to be re-downloaded next time."
                )

        dlg = wx.RichMessageDialog(
            self, body, title,
            style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        dlg.SetYesNoLabels("&Close anyway", "&Keep running")
        dlg.ShowCheckBox("&Don't ask again")
        result = dlg.ShowModal()
        dont_ask = dlg.IsCheckBoxChecked()
        dlg.Destroy()

        if dont_ask:
            self.prefs.set_bool(_p.KEY_CONFIRM_CANCEL_ON_CLOSE, False)
            if hasattr(self, "_confirm_close_item"):
                self._confirm_close_item.Check(False)

        return result == wx.ID_YES

    # ── Watchlist autopoll ───────────────────────────────────

    def _start_watchlist_poller(self):
        """Instantiate the watchlist poller and, if the user has
        autopoll enabled, start its background thread. The poller is
        kept around in either case so the Preferences dialog can flip
        autopoll on/off at runtime by calling ``reconfigure()``.
        """
        from . import prefs as _p
        from .watchlist_poller import WatchlistPoller

        self._watchlist_poller = WatchlistPoller(self.prefs)
        if self.prefs.get_bool(_p.KEY_WATCH_AUTOPOLL):
            self._watchlist_poller.start()

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
        self._set_busy(True, kind="download")
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
        self._set_busy(True, kind="preview")
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

        self._set_busy(True, kind="download")
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
        self._set_busy(True, kind="download")
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
            self._set_busy(True, kind="download")
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
        self._confirm_close_item = file_menu.AppendCheckItem(
            wx.ID_ANY, "&Warn before closing during downloads",
        )
        self.Bind(
            wx.EVT_MENU, self._on_confirm_close_menu,
            self._confirm_close_item,
        )
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, "E&xit")
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), exit_item)
        bar.Append(file_menu, "&File")

        edit_menu = wx.Menu()
        prefs_item = edit_menu.Append(
            wx.ID_PREFERENCES, "&Preferences...\tCtrl+,",
        )
        self.Bind(wx.EVT_MENU, self._on_preferences_menu, prefs_item)
        bar.Append(edit_menu, "&Edit")

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

    def _on_confirm_close_menu(self, event):
        from . import prefs as _p
        self.prefs.set_bool(
            _p.KEY_CONFIRM_CANCEL_ON_CLOSE,
            self._confirm_close_item.IsChecked(),
        )

    def _on_preferences_menu(self, event):
        from .preferences import PreferencesDialog

        dlg = PreferencesDialog(self, self.prefs, main_frame=self)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def apply_preferences(self):
        """Called from PreferencesDialog after OK. Re-reads every pref
        the main form mirrors and pushes it into the live controls so
        the change takes effect immediately, without waiting for an
        app restart. Also re-syncs the View/File menu check items and
        re-applies logging config.
        """
        from . import prefs as _p

        # Download-form fields that mirror prefs
        self.output_ctrl.SetValue(self.prefs.get(_p.KEY_OUTPUT_DIR) or "")
        self.name_ctrl.SetValue(self.prefs.get(_p.KEY_NAME_TEMPLATE) or "")

        fmt = (self.prefs.get(_p.KEY_FORMAT) or "epub").lower()
        fmt_choices = ["epub", "html", "txt", "audio"]
        if fmt in fmt_choices:
            self.format_ctrl.SetSelection(fmt_choices.index(fmt))
            self._update_audio_panel_visibility()

        self.hr_stars_ctrl.SetValue(self.prefs.get_bool(_p.KEY_HR_AS_STARS))
        self.strip_notes_ctrl.SetValue(self.prefs.get_bool(_p.KEY_STRIP_NOTES))

        try:
            rate = int(self.prefs.get(_p.KEY_SPEECH_RATE) or "0")
        except (TypeError, ValueError):
            rate = 0
        self.speech_rate_ctrl.SetValue(max(-50, min(100, rate)))

        backend = self.prefs.get(_p.KEY_ATTRIBUTION_BACKEND) or "builtin"
        if backend in self._attribution_choices:
            self.attribution_ctrl.SetSelection(
                self._attribution_choices.index(backend)
            )
            self._refresh_attribution_status()
            self._refresh_size_choices(
                preferred=self.prefs.get(_p.KEY_ATTRIBUTION_MODEL_SIZE) or None,
            )

        # Logging: level and file-output may have changed — route through
        # the existing setters so menu check items re-sync and the live
        # handlers get rebuilt.
        level = (self.prefs.get(_p.KEY_LOG_LEVEL) or "INFO").upper()
        if level in _LOG_LEVELS:
            self._set_log_level_idx(_LOG_LEVELS.index(level))
            for lvl_name, item in getattr(self, "_log_level_items", {}).items():
                item.Check(lvl_name == level)
        self._set_log_to_file(self.prefs.get_bool(_p.KEY_LOG_TO_FILE))
        if getattr(self, "_log_to_file_item", None) is not None:
            self._log_to_file_item.Check(self._log_to_file_enabled)

        # File-menu "warn before closing" toggle
        if getattr(self, "_confirm_close_item", None) is not None:
            self._confirm_close_item.Check(
                self.prefs.get_bool(_p.KEY_CONFIRM_CANCEL_ON_CLOSE)
            )

        # Watchlist autopoll — reconfigure picks up interval changes
        # and starts/stops the thread to match the current pref.
        if getattr(self, "_watchlist_poller", None) is not None:
            self._watchlist_poller.reconfigure()

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


def main():
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
