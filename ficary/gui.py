"""Accessible wxPython GUI for ficary.

Uses native Win32 controls via wxPython so NVDA, JAWS, and other
screen readers can read every widget natively.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import wx
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class _DownloadParams:
    """Immutable snapshot of every wx-widget setting a download/export
    worker needs.

    Snapshotted on the main thread at the moment the user triggers a
    job (Download click, Update, batch picker OK, search-frame Download
    Selected) and threaded through to ``_export_story`` /
    ``_resolve_output_dir`` so worker threads never read wx widgets
    directly. Reading widgets off-main was the long-standing round-5
    follow-up: wxPython on Windows usually returns sensible data but
    can race during widget destruction (e.g. close-during-export).

    The snapshot also fixes a separate UX bug: a queued batch used to
    pick up whatever the format dropdown happened to read when each
    worker eventually ran. Snapshotting at enqueue time pins the
    settings the user saw when they clicked Download.
    """

    fmt: str
    raw_output_dir: str
    filename_template: str
    hr_as_stars: bool
    strip_notes: bool
    llm_strip_notes: bool
    llm_render_config: Optional[dict] = None
    audio_backend: Optional[str] = None
    audio_size: Optional[str] = None
    speech_rate: Optional[int] = None
    enabled_tts_providers: tuple = ()
    use_fichub: bool = False
    merge_series: bool = False
    webnovel_cookie: str = ""
    ao3_cookie: str = ""


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


def _announce_label(ctrl: "wx.Window", text: str) -> None:
    """Update a StaticText (or similar) so a screen reader picks up the
    change.

    ``wx.StaticText.SetLabel`` alone is silent on Windows: NVDA only
    announces a control whose accessible *name* changed, not its
    visible label. Calling :meth:`wx.Window.SetName` with the same
    string flips the MSAA name attribute, which NVDA reads on its next
    poll. Where the platform exposes :meth:`wx.Accessible.NotifyEvent`,
    we also fire ``wx.ACC_EVENT_OBJECT_NAMECHANGE`` so the reader is
    woken immediately rather than on the next focus event.

    Used for transient status text — "(installing...)", "(installed)",
    download progress, install failure reasons — that the blind user
    otherwise can't tell has changed."""
    ctrl.SetLabel(text)
    try:
        ctrl.SetName(text)
    except Exception:
        return
    # ``GetAccessible()`` returns ``None`` for every stock wx control
    # that hasn't had a custom accessible object attached — which is
    # essentially all of them for this app. The old code bailed at
    # that point, meaning the immediate MSAA name-change wake-up
    # never fired and NVDA only picked up the change on the next focus
    # poll. ``wx.Accessible.NotifyEvent`` itself is a stand-alone
    # static-style notifier that works on the control directly, no
    # custom accessible object required.
    try:
        wx.Accessible.NotifyEvent(
            wx.ACC_EVENT_OBJECT_NAMECHANGE,
            ctrl,
            wx.ACC_SELF,
            0,
        )
    except Exception:
        # NotifyEvent isn't wired on every platform/version; failure
        # just means the reader won't be poked immediately, not that
        # the label didn't update.
        return


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
        # Lines mirrored to the file logger by ``exporters._emit`` are
        # already on their way to the GUI status pane via the progress
        # callback — skip them here so the user doesn't see every
        # ``[llm-an]`` (and similar) line twice. File handlers don't
        # check this flag, so the on-disk transcript is still complete.
        if getattr(record, "ui_already_emitted", False):
            return
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


from . import legacy as _legacy
from .download_queue import (
    DownloadQueues,
    WORKER_THREAD_PREFIX,
    site_from_thread_name,
)
from .gui_dialogs import (
    OptionalFeaturesDialog,
    StoryPickerDialog,
    VoicePreviewDialog,
)
from .gui_search import (
    SearchFrame,
    _ao3_search_spec,
    _erotica_search_spec,
    _ffn_search_spec,
    _royalroad_search_spec,
    _wattpad_search_spec,
)


def _show_update_dialog(
    parent,
    *,
    body: str,
    primary_label: str,
    primary_result: str,
    release_url: str,
) -> str:
    """Modal four-button update prompt.

    ``wx.MessageDialog`` only carries three buttons (YES/NO/CANCEL),
    so this helper rolls a custom :class:`wx.Dialog` to surface a
    fourth "View Release Notes" action without forcing the user
    through a yes/no funnel that hides the changelog.

    Returned strings:
        ``"update"`` / ``"open_release"`` — primary action; the caller
            decides which by passing ``primary_result``.
        ``"release_notes"`` — open the release page but defer the
            install decision (the caller usually re-arms the snooze).
        ``"later"`` — explicit "Remind Me Later" or ESC/close.
        ``"skip"`` — pin this tag so it never re-prompts.

    The "View Release Notes" button is omitted entirely when no
    release URL is available (e.g. the GitHub API call returned an
    asset without an ``html_url``) so we never present a button that
    can't do its job.
    """
    dlg = wx.Dialog(
        parent,
        title="Update Available",
        style=wx.DEFAULT_DIALOG_STYLE,
    )
    panel = wx.Panel(dlg)
    sizer = wx.BoxSizer(wx.VERTICAL)

    text = wx.StaticText(panel, label=body)
    text.Wrap(560)
    # Tag the body text so screen readers (NVDA, VoiceOver) announce
    # it as the dialog message rather than an unlabelled region.
    text.SetName("Update details")
    sizer.Add(text, 0, wx.ALL, 16)

    btn_row = wx.BoxSizer(wx.HORIZONTAL)
    primary_btn = wx.Button(panel, label=primary_label)
    primary_btn.Bind(wx.EVT_BUTTON, lambda _e: dlg.EndModal(1))

    notes_btn = None
    if release_url:
        notes_btn = wx.Button(panel, label="View &Release Notes")
        notes_btn.Bind(wx.EVT_BUTTON, lambda _e: dlg.EndModal(2))

    later_btn = wx.Button(panel, label="Re&mind Me Later")
    later_btn.Bind(wx.EVT_BUTTON, lambda _e: dlg.EndModal(3))

    skip_btn = wx.Button(panel, label="&Skip This Version")
    skip_btn.Bind(wx.EVT_BUTTON, lambda _e: dlg.EndModal(4))

    btn_row.Add(primary_btn, 0, wx.RIGHT, 8)
    if notes_btn is not None:
        btn_row.Add(notes_btn, 0, wx.RIGHT, 8)
    btn_row.Add(later_btn, 0, wx.RIGHT, 8)
    btn_row.Add(skip_btn, 0)
    sizer.Add(btn_row, 0, wx.ALIGN_RIGHT | wx.ALL, 16)

    panel.SetSizerAndFit(sizer)
    dlg.Fit()
    dlg.SetAffirmativeId(1)  # ENTER triggers the primary action
    dlg.SetEscapeId(3)       # ESC = "Remind Me Later"
    primary_btn.SetDefault()
    primary_btn.SetFocus()

    try:
        result = dlg.ShowModal()
    finally:
        dlg.Destroy()

    return {
        1: primary_result,
        2: "release_notes",
        3: "later",
        4: "skip",
    }.get(result, "later")


class MainFrame(wx.Frame):
    def __init__(self):
        from . import __version__
        super().__init__(
            None,
            title=f"Ficary {__version__} - Fanfiction Downloader & Reader",
            size=(820, 720),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        from .prefs import Prefs
        self.prefs = Prefs()
        # ``_global_busy`` covers operations that don't route through
        # the per-site download queue: searches, voice previews, and
        # batch/picker flows. Per-site downloads (single URLs, library
        # update-all Phase 3) track their state in ``_active_sites``
        # instead, populated by the DownloadQueues listener so a
        # manual AO3 download can run while an FFN sweep is in flight.
        self._global_busy = False
        self._busy_kind = None
        # Picker-flow ownership transfer flag. ``_run_picker_download``
        # runs on a raw worker thread (not the per-site queue) and
        # spawns a follow-up ``_run_picked_batch`` thread when the user
        # confirms selections. The follow-up takes over busy ownership,
        # but the outer ``_run_download`` finally would otherwise
        # clear ``_global_busy`` before the batch ran. The picker
        # handler sets this flag before ``picker_done.set()`` so the
        # outer finally can see "ownership transferred — leave busy
        # alone" and skip its clear. ``threading.Event``'s set/wait
        # establishes the happens-before relationship needed for the
        # worker thread to observe the write.
        self._picker_transferred_busy = False
        # site_name → (active, pending) for sites with jobs running
        # or queued. Updated from ``_on_site_queue_change`` on the
        # main thread; drives the close-confirmation dialog's summary.
        self._active_sites: dict[str, tuple[int, int]] = {}
        DownloadQueues.add_listener(self._on_site_queue_change)
        self._watching = False
        self._watch_seen = set()
        self._last_clip = ""
        # site_key → open SearchFrame (lazy-created on first menu invocation)
        self._search_frames = {}
        self._watchlist_frame = None
        self._library_frame = None
        self._reader_frame = None
        # Per-session record of fandom-subfolder create decisions:
        # fandom-folder name → True (create) / False (don't create).
        # Re-asked every launch so the user stays in control if they
        # rearrange folders by hand between sessions.
        self._fandom_folder_decisions: dict[str, bool] = {}
        # Attribution backends with a background ``pip install`` worker
        # still running. ``_on_install_attribution`` refuses to spawn a
        # second install for an already-running backend; without this
        # guard, switching the backend dropdown re-enables the Install
        # button mid-flight and a second click races the first installer
        # over the same venv.
        self._installing_attribution: set[str] = set()
        # Worker threads parked on a wx.MessageBox dispatched via
        # wx.CallAfter register their ``threading.Event`` here so the
        # close handler can wake them instantly when the frame is going
        # away. Without this, a Quit-during-download blocks the worker
        # for up to two minutes (the wait timeout) because the message
        # box never gets drawn against a frame that's already destroyed.
        self._pending_worker_dialogs: set[threading.Event] = set()
        self._pending_worker_dialogs_lock = threading.Lock()
        self._log_queue = deque()
        self._log_lock = threading.Lock()
        self._build_ui()
        self._load_prefs()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()
        self._start_update_check()
        # Deferred so the main window paints first — a modal dialog
        # popping up during the paint cycle looks like a crash.
        wx.CallAfter(self._maybe_offer_library_as_default)
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
        opts2.Add(self.strip_notes_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)

        # LLM backstop: paired with the regex strip. Enabled
        # independently of the audiobook attribution backend so a
        # user can run a paid-API LLM on HTML/EPUB exports without
        # inheriting the audiobook narrator path.
        self.llm_strip_notes_ctrl = wx.CheckBox(
            root, label="Use &LLM to catch missed A/N (slower)"
        )
        self.llm_strip_notes_ctrl.SetName("Use LLM to catch missed author's notes")
        self.llm_strip_notes_ctrl.SetToolTip(
            "Run the configured LLM over each chapter after the regex "
            "pass. Catches notes the regex misses but adds one "
            "round-trip per chapter — local Ollama is free but slow, "
            "paid APIs charge per token. Results are cached per story "
            "so re-exports don't re-spend. Click 'LLM settings…' to "
            "pick the provider/model."
        )
        opts2.Add(self.llm_strip_notes_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)

        # FFN-only fast path. fanfiction.net throttles direct scraping
        # to ~6s/chapter; FicHub (https://fichub.net) serves a shared,
        # pre-built cache so the whole fic arrives in one request. Off
        # by default because its copy can lag the latest chapters.
        self.fichub_ctrl = wx.CheckBox(
            root,
            label="&Fast fanfiction.net download via FicHub (may lag latest chapters)",
        )
        self.fichub_ctrl.SetName(
            "Fast fanfiction.net download via FicHub shared cache — "
            "much faster but may lag the most recent chapters"
        )
        self.fichub_ctrl.SetToolTip(
            "fanfiction.net rate-limits direct downloads to about six "
            "seconds per chapter. FicHub keeps a shared cache of fics "
            "and serves the whole story in one request, so a long fic "
            "downloads in seconds instead of minutes. FicHub's copy can "
            "lag the source by a few chapters, so leave this off when "
            "you specifically want the very latest chapter or are "
            "updating an existing download — it's ignored for updates "
            "and falls back to a normal download if FicHub doesn't have "
            "the fic."
        )
        opts2.Add(self.fichub_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 16)

        # Combine a series (AO3 /series/ or Literotica /series/se/) pasted
        # into the download box into a single file with every part as a
        # chapter, instead of one file per part. Off by default so upgrading
        # doesn't change the behavior existing users already get; persisted
        # so a user who always wants merged series sets it once.
        self.merge_series_ctrl = wx.CheckBox(
            root,
            label="Combine a &series into one book (instead of one file per part)",
        )
        self.merge_series_ctrl.SetName(
            "Combine a series into one book instead of one file per part"
        )
        self.merge_series_ctrl.SetToolTip(
            "When you download an AO3 or Literotica series URL, produce one "
            "file in the selected format with every part as a chapter, "
            "instead of one file per part. Leave off to keep one file per "
            "part."
        )
        opts2.Add(
            self.merge_series_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 16
        )

        # Always-visible shortcut to the LLM settings dialog. The
        # audio toolbar has its own copy that surfaces only when
        # Format=audio + Attribution=LLM, but the A/N strip above
        # works for every export format — the user has to be able
        # to reach the dialog from HTML/EPUB/TXT mode too.
        self.llm_settings_main_btn = wx.Button(
            root, label="LLM settings&…", size=(120, -1),
        )
        self.llm_settings_main_btn.SetName("Open LLM settings")
        self.llm_settings_main_btn.SetToolTip(
            "Configure the LLM used by the A/N strip option to the "
            "left (and by the audiobook attribution backend when "
            "Format=audio + Attribution=LLM)."
        )
        self.llm_settings_main_btn.Bind(wx.EVT_BUTTON, self._on_llm_settings)
        opts2.Add(
            self.llm_settings_main_btn, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8,
        )
        root_sizer.Add(opts2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        # webnovel.com optional auth. A logged-in browser "Cookie:" header
        # lets the user download chapters their account has unlocked; left
        # blank, only free chapters are fetched. Password-styled because
        # the cookie is a session secret; stored plain-text in prefs, the
        # same as the LLM API key / Pushover / Discord secrets already are.
        wn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        wn_sizer.Add(
            wx.StaticText(root, label="&Webnovel.com cookie:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.webnovel_cookie_ctrl = wx.TextCtrl(
            root, size=(260, -1), style=wx.TE_PASSWORD,
        )
        self.webnovel_cookie_ctrl.SetName(
            "Webnovel.com session cookie — paste a logged-in browser Cookie "
            "header to download chapters you have unlocked; leave blank for "
            "free chapters only"
        )
        self.webnovel_cookie_ctrl.SetToolTip(
            "Optional, only used for webnovel.com. Paste the 'Cookie:' "
            "header from a logged-in webnovel.com browser session to fetch "
            "chapters your account has unlocked. Leave blank to download "
            "only the free chapters (locked ones become placeholders). "
            "Stored in your local settings; coins are never spent."
        )
        wn_sizer.Add(self.webnovel_cookie_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        root_sizer.Add(wn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        # AO3 optional auth. A logged-in browser "Cookie:" header unlocks
        # restricted / Archive-locked works and your private bookmarks;
        # blank means anonymous. Same plain-text-in-prefs posture as above.
        ao3c_sizer = wx.BoxSizer(wx.HORIZONTAL)
        ao3c_sizer.Add(
            wx.StaticText(root, label="A&O3 cookie:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.ao3_cookie_ctrl = wx.TextCtrl(
            root, size=(260, -1), style=wx.TE_PASSWORD,
        )
        self.ao3_cookie_ctrl.SetName(
            "AO3 session cookie — paste a logged-in browser Cookie header to "
            "download restricted works and private bookmarks; leave blank for "
            "anonymous access"
        )
        self.ao3_cookie_ctrl.SetToolTip(
            "Optional, only used for archiveofourown.org. Paste the 'Cookie:' "
            "header from a logged-in AO3 browser session to download "
            "restricted / Archive-locked works and your own private bookmarks "
            "and marked-for-later. Leave blank for anonymous access. Stored "
            "in your local settings."
        )
        ao3c_sizer.Add(self.ao3_cookie_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        root_sizer.Add(ao3c_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

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
        audio_sizer.Add(self.attribution_install_btn, 0, wx.RIGHT, 4)

        # LLM-only "Settings..." button — opens a modal for provider /
        # model / API key / endpoint. Hidden unless the LLM backend is
        # selected so the audio toolbar stays uncluttered for everyone
        # using the heuristic / neural backends.
        self.llm_settings_btn = wx.Button(
            self.audio_panel, label="LLM &settings...", size=(140, -1),
        )
        self.llm_settings_btn.SetName("LLM settings")
        self.llm_settings_btn.Bind(wx.EVT_BUTTON, self._on_llm_settings)
        audio_sizer.Add(self.llm_settings_btn, 0, wx.RIGHT, 4)
        self.llm_settings_btn.Hide()

        # Multi-provider TTS controls — pick which providers contribute
        # voices to the synthesis pool, and a button to install Piper /
        # download voices on first use.
        self.tts_providers_btn = wx.Button(
            self.audio_panel, label="&TTS providers...", size=(150, -1),
        )
        self.tts_providers_btn.SetName("Manage TTS providers")
        self.tts_providers_btn.Bind(wx.EVT_BUTTON, self._on_tts_providers)
        audio_sizer.Add(self.tts_providers_btn, 0)

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
        # Auto-prefix log lines emitted on a per-site queue worker so
        # the shared status pane stays readable when two sites are
        # downloading concurrently. Messages from the main thread and
        # from ad-hoc background threads (probe pools, TTS) fall
        # through unprefixed.
        site = site_from_thread_name(threading.current_thread().name)
        if site:
            msg = f"[{site}] {msg}"
        with self._log_lock:
            self._log_queue.append(msg + "\n")

    # ── Logging controls ─────────────────────────────────────

    def _log_dir(self) -> Path:
        """Directory for log files. Portable build keeps logs next to
        the exe so they travel with the install; dev/pip uses the
        same dotfile root as other ficary state."""
        from . import portable
        d = portable.portable_root() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _log_file(self) -> Path:
        return self._log_dir() / "ficary.log"

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
        # asyncio proactor churn — none of it ficary's own output, and
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

    @property
    def _downloading(self) -> bool:
        """Legacy compatibility shim: True when *anything* is running.

        Callers scattered across the search frames and clipboard
        watcher still ask "is the app busy?" with this check. Per-site
        downloads (single URLs, library update-all) no longer disable
        buttons, but they do count toward busy for the close-confirm
        dialog and the "don't start another batch/search" guards.
        """
        return self._global_busy or bool(self._active_sites)

    def _has_active_background_work(self) -> bool:
        """True if a download/search/preview/batch is still running.

        Shared by the close-confirmation dialog and the update guard
        so both surfaces protect the same set of in-flight operations.
        """
        return self._global_busy or bool(self._active_sites)

    @_downloading.setter
    def _downloading(self, _value):  # pragma: no cover — legacy shim
        # The old code assigned ``self._downloading = True/False``
        # directly. State now derives from ``_global_busy`` plus the
        # queue snapshot, so those writes silently no-op.
        pass

    def _set_busy(self, busy, kind=None):
        """Toggle the *global* busy flag — used by operations that
        don't route through the per-site download queue (searches,
        voice previews, batch picker flows, series-merge runs).
        Per-site downloads update busy state through the
        ``DownloadQueues`` listener instead, so a queued AO3 download
        does not block an FFN search or a cross-site batch.

        ``kind`` is one of ``"download"``, ``"preview"``, ``"search"``
        (or ``None`` when clearing). It drives the close-confirmation
        prompt's message so users see *what* they're cancelling, not a
        generic "work in progress" banner.
        """
        # Flip the busy state SYNCHRONOUSLY so callers that immediately
        # read ``self._global_busy`` see the updated value. Earlier the
        # whole body ran via ``wx.CallAfter``, which meant two rapid
        # Download clicks could both observe ``_global_busy == False``
        # and both spawn workers. Bool assignment is atomic in CPython
        # so cross-thread sets are safe; only the UI refresh has to
        # marshal back to the main thread.
        self._global_busy = bool(busy)
        self._busy_kind = kind if busy else None
        wx.CallAfter(self._refresh_busy_ui)

    def _refresh_busy_ui(self):
        """Re-apply button enable state from the current busy flags.

        Global operations (searches, voice previews, batch downloads)
        still disable the main buttons so the user can't triple-book
        the single thread those paths share. Per-site queue activity
        leaves the buttons enabled — a same-site click just queues
        behind the running job.
        """
        busy = self._global_busy
        try:
            self.dl_btn.Enable(not busy)
            self.update_btn.Enable(not busy)
            self.voices_btn.Enable(not busy)
        except RuntimeError:
            return
        for frame in list(self._search_frames.values()):
            try:
                frame.apply_busy(busy)
            except Exception:
                pass

    def _on_site_queue_change(self, site_name, active, pending):
        """``DownloadQueues`` listener — fires on the worker thread
        whenever a site's job counts change. Marshals to the main
        thread before mutating shared state or touching wx controls.
        """
        def _apply():
            if active == 0 and pending == 0:
                self._active_sites.pop(site_name, None)
            else:
                self._active_sites[site_name] = (active, pending)
        try:
            wx.CallAfter(_apply)
        except RuntimeError:
            # wx teardown race — the listener will be removed on
            # close, but an in-flight notification can still land
            # here. Nothing sensible to do besides drop it.
            pass

    def _enqueue_site_job(self, url, job_fn, *, kind="download"):
        """Queue ``job_fn`` on the per-site worker for ``url``'s host.

        The body runs on a ``dlq-<site>`` thread, so any ``self._log``
        lines the job emits get an automatic ``[<site>] `` prefix from
        the thread-name check in ``_log``. Returns the ``Future`` so
        callers that need to wait on completion (library update-all)
        can await it.
        """
        from .sites import detect_scraper

        scraper_cls = detect_scraper(url)
        site_name = getattr(scraper_cls, "site_name", "unknown")
        # Make queueing visible: a click on Download for a site that's
        # already busy does nothing obvious today, which made this
        # feel like a broken button. Log the queue position so users
        # see their click took effect.
        snapshot = DownloadQueues.snapshot().get(site_name)
        if snapshot is not None:
            active, pending = snapshot
            if active + pending > 0:
                self._log(
                    f"Queued on {site_name}: {url} "
                    f"(behind {active + pending} job"
                    f"{'s' if (active + pending) != 1 else ''})"
                )
        return DownloadQueues.enqueue(site_name, job_fn)

    def _is_batch_url(self, url) -> bool:
        """True if ``url`` fans out to a picker or multi-work flow.

        Batch URLs (AO3 bookmarks, author pages, series pages) open a
        picker dialog on the main thread and then kick off N separate
        downloads. That flow doesn't map onto the one-job-per-enqueue
        model cleanly yet, so batches stay on the legacy global-busy
        path — they block other batches/searches but still run
        concurrently with per-site single-URL downloads.
        """
        from .ao3 import AO3Scraper
        from .erotica import LiteroticaScraper
        from .sites import detect_scraper

        if AO3Scraper.is_bookmarks_url(url):
            return True
        scraper_cls = detect_scraper(url)
        scraper = scraper_cls()
        if scraper.is_author_url(url):
            return True
        if (
            AO3Scraper.is_series_url(url)
            or LiteroticaScraper.is_series_url(url)
        ):
            return True
        return False

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
        # The LLM backend has no install step (urllib + json from the
        # stdlib are enough); show its config button instead and hide
        # the install button.
        is_llm = backend == "llm"
        self.llm_settings_btn.Show(is_llm)
        self.attribution_install_btn.Show(not is_llm)
        self.audio_panel.Layout()
        if is_llm:
            _announce_label(self.attribution_status, self._llm_status_label())
            return
        # If a previous install is still running for this backend, keep
        # the button locked and surface that state to the screen reader
        # rather than re-enabling it on every dropdown change.
        if backend in self._installing_attribution:
            _announce_label(self.attribution_status, "(installing...)")
            self.attribution_install_btn.Enable(False)
            self.attribution_install_btn.SetLabel("&Install...")
            return
        if backend == "builtin":
            _announce_label(self.attribution_status, "(built-in)")
            self.attribution_install_btn.Enable(False)
            self.attribution_install_btn.SetLabel("&Install...")
            return
        reason = self._attribution_module.install_unsupported_reason(backend)
        if reason:
            _announce_label(self.attribution_status, "(install unsupported)")
            self.attribution_install_btn.Enable(False)
            self.attribution_install_btn.SetLabel("&Install...")
            return
        if self._attribution_module.is_installed(backend):
            _announce_label(self.attribution_status, "(installed)")
            self.attribution_install_btn.Enable(True)
            self.attribution_install_btn.SetLabel("Re&install...")
        else:
            _announce_label(self.attribution_status, "(not installed)")
            self.attribution_install_btn.Enable(True)
            self.attribution_install_btn.SetLabel("&Install...")

    def _llm_status_label(self):
        """One-line summary of the LLM config — ``provider/model`` when
        configured, ``(needs setup)`` when not. Read live from prefs so
        the dialog's Save updates the toolbar without a redraw call."""
        from . import prefs as _p

        provider = (self.prefs.get(_p.KEY_LLM_PROVIDER) or "").strip()
        model = (self.prefs.get(_p.KEY_LLM_MODEL) or "").strip()
        if not provider or not model:
            return "(needs setup)"
        return f"({provider} / {model})"

    def _on_llm_settings(self, event):
        from .gui_dialogs import LlmSettingsDialog

        dlg = LlmSettingsDialog(self, self.prefs)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        # Status label reads from prefs — refresh after Save.
        _announce_label(self.attribution_status, self._llm_status_label())

    def _on_tts_providers(self, event):
        from .gui_dialogs import TtsProvidersDialog

        dlg = TtsProvidersDialog(self, self.prefs, log_callback=self._log)
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()

    def _enabled_tts_providers(self) -> list[str] | None:
        """Resolve the user's TTS-provider preference into the list the
        audiobook generator wants. Empty / unset means "all installed
        providers" — the generator handles that as None."""
        from . import prefs as _p

        raw = (self.prefs.get(_p.KEY_TTS_PROVIDERS) or "").strip()
        if not raw:
            return None
        names = [n.strip().lower() for n in raw.split(",") if n.strip()]
        return names or None

    def _llm_config_for_render(self):
        """Read the saved LLM prefs and build the kwargs dict that
        ``generate_audiobook`` forwards to the LLM backend. Returns
        None when the user hasn't picked a model yet — the dispatcher
        treats that as a config error and falls back to builtin so
        the render still produces audio."""
        from . import prefs as _p

        provider = (self.prefs.get(_p.KEY_LLM_PROVIDER) or "").strip() or "ollama"
        model = (self.prefs.get(_p.KEY_LLM_MODEL) or "").strip()
        api_key = (self.prefs.get(_p.KEY_LLM_API_KEY) or "").strip()
        endpoint = (self.prefs.get(_p.KEY_LLM_ENDPOINT) or "").strip()
        if not model:
            return None
        config = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "endpoint": endpoint,
        }
        # 0 (the default) means "fall back to env var / built-in default";
        # only forward an explicit user-set value so the env override still
        # works for users who never opened the dialog.
        try:
            timeout_pref = int(self.prefs.get(_p.KEY_LLM_REQUEST_TIMEOUT_S) or 0)
        except (TypeError, ValueError):
            timeout_pref = 0
        if timeout_pref > 0:
            config["request_timeout_s"] = timeout_pref
        return config

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
                f"Click Install or run: ficary --install-attribution {backend}"
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
        # Switching the backend dropdown re-enables the Install button
        # in ``_refresh_attribution_status``, which lets the user click
        # Install a second time on a backend whose previous install
        # thread is still running — two concurrent pip-install workers
        # against the same backend will race over the same venv. Track
        # in-progress installs and refuse to spawn a second one.
        if backend in self._installing_attribution:
            self._log(f"{backend} install already in progress; ignoring.")
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
                "Everything lives in %LOCALAPPDATA%\\ficary\\neural\\."
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
        _announce_label(self.attribution_status, "(installing...)")
        self._installing_attribution.add(backend)

        def run():
            def cb(line):
                # Marshal log lines back to the main thread.
                wx.CallAfter(self._log, line)
            try:
                ok = self._attribution_module.install(backend, log_callback=cb)
            except Exception as exc:
                # Without this guard, an unexpected raise from the pip
                # subprocess (OSError, network exception, permission
                # error) skipped the ``wx.CallAfter(self._after_install…)``
                # line below. ``_installing_attribution`` then kept the
                # backend forever and the install button stayed disabled
                # with "(installing...)" as the status — only fixable
                # by a restart.
                wx.CallAfter(
                    self._log,
                    f"Install of {backend} crashed: {exc}",
                )
                ok = False
            wx.CallAfter(self._after_install, backend, ok, frozen)

        threading.Thread(target=run, daemon=True).start()

    def _after_install(self, backend, ok, frozen):
        self._installing_attribution.discard(backend)
        if ok:
            self._log(f"Installed {backend} successfully.")
            if frozen:
                # A .pth-using package like torch usually needs a fresh
                # interpreter to import cleanly. Don't try to hot-load;
                # prompt for a restart.
                wx.MessageBox(
                    f"{backend} was installed successfully.\n\n"
                    "Please restart ficary so the new modules are "
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
        else:
            # No explicit save location stored yet — if the user has a
            # configured library, default to its root so fandom-folder
            # auto-routing kicks in immediately on the first download.
            library_root = (self.prefs.get(_p.KEY_LIBRARY_PATH, "") or "").strip()
            if library_root:
                self.output_ctrl.SetValue(library_root)

        self.hr_stars_ctrl.SetValue(self.prefs.get_bool(_p.KEY_HR_AS_STARS))
        self.strip_notes_ctrl.SetValue(self.prefs.get_bool(_p.KEY_STRIP_NOTES))
        self.llm_strip_notes_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_LLM_STRIP_NOTES)
        )
        self.fichub_ctrl.SetValue(self.prefs.get_bool(_p.KEY_FICHUB))
        self.merge_series_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_MERGE_SERIES)
        )
        self.webnovel_cookie_ctrl.SetValue(
            self.prefs.get(_p.KEY_WEBNOVEL_COOKIE) or ""
        )
        self.ao3_cookie_ctrl.SetValue(self.prefs.get(_p.KEY_AO3_COOKIE) or "")

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
        self.prefs.set_bool(
            _p.KEY_LLM_STRIP_NOTES, self.llm_strip_notes_ctrl.GetValue(),
        )
        self.prefs.set_bool(_p.KEY_FICHUB, self.fichub_ctrl.GetValue())
        self.prefs.set_bool(
            _p.KEY_MERGE_SERIES, self.merge_series_ctrl.GetValue()
        )
        self.prefs.set(
            _p.KEY_WEBNOVEL_COOKIE,
            self.webnovel_cookie_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_AO3_COOKIE, self.ao3_cookie_ctrl.GetValue().strip(),
        )
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

        # Same idea for an in-progress Ollama model pull. The pull
        # worker is a daemon thread holding an open HTTP stream;
        # exiting the app kills the thread, which leaves Ollama with
        # a partial weight file it has to redo from scratch. Warn so
        # the user can finish the pull first if they want to.
        from . import ollama_install
        if ollama_install.has_active_pulls():
            choice = wx.MessageBox(
                "An Ollama model is still downloading. Quitting "
                "ficary now will cancel the download and Ollama will "
                "need to start over next time. Quit anyway?",
                "Pull in progress",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                self,
            )
            if choice != wx.YES:
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
        # The reader is a child frame: parent Destroy skips its EVT_CLOSE,
        # which is the only place the session reading position, state-DB
        # close, and sleep-timer cancel happen. Close() (not Destroy) runs
        # that handler.
        if getattr(self, "_reader_frame", None) is not None:
            try:
                self._reader_frame.Close()
            except (RuntimeError, AttributeError):
                logger.debug("reader frame close failed", exc_info=True)
            self._reader_frame = None
        # Release the audio device + poller/fade threads if audio was used.
        try:
            from .audio.engine import shutdown_engine
            shutdown_engine()
        except Exception:
            logger.debug("audio engine shutdown failed", exc_info=True)
        if getattr(self, "_watchlist_poller", None) is not None:
            self._watchlist_poller.stop()
        try:
            self._save_prefs()
        except (RuntimeError, OSError):
            logger.debug("_save_prefs on close failed", exc_info=True)
        if hasattr(self, "_log_timer"):
            self._log_timer.Stop()
        # Same treatment for the clipboard-watch timer — left running
        # past close, it kept calling _on_clip_timer which would touch
        # destroyed widgets if the user closed the window during an
        # active clipboard-watch session.
        if hasattr(self, "_clip_timer"):
            self._clip_timer.Stop()
        self._detach_log_handlers()
        # Unregister the DownloadQueues listener so the global queue
        # singleton stops holding a reference to this MainFrame after
        # shutdown. Without this the listener can keep the destroyed
        # frame alive (and scheduling stale ``wx.CallAfter``s) for as
        # long as the process is alive.
        try:
            DownloadQueues.remove_listener(self._on_site_queue_change)
        except (KeyError, ValueError, RuntimeError, AttributeError):
            logger.debug("DownloadQueues listener removal failed", exc_info=True)
        # Wake any worker thread parked on a wx.MessageBox we marshalled
        # via wx.CallAfter — once we Skip() the event, the frame is
        # destroyed and the prompt() will never run, so the worker would
        # otherwise block until the 120s timeout fires. Treat as
        # "user said no" by leaving ``answer["value"]`` at its False
        # default and just signalling the event.
        with self._pending_worker_dialogs_lock:
            for evt in list(self._pending_worker_dialogs):
                evt.set()
            self._pending_worker_dialogs.clear()
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

        # Per-site downloads list their active sites explicitly —
        # users want to know "what am I interrupting", not a generic
        # "a download is running" banner that hides the fact that two
        # different sites are both mid-flight.
        if self._active_sites and not self._global_busy:
            parts = []
            for name, (active, pending) in sorted(self._active_sites.items()):
                total = active + pending
                parts.append(
                    f"  \u2022 {name}: {total} job"
                    f"{'s' if total != 1 else ''}"
                )
            title = "Downloads in progress"
            body = (
                "Per-site downloads are still running:\n\n"
                + "\n".join(parts)
                + "\n\n"
                "Close ficary and cancel them? Chapters already "
                "fetched stay cached and will not need to be "
                "re-downloaded next time."
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

        kind = self._busy_kind
        if kind == "preview":
            title = "Voice preview in progress"
            body = (
                "A voice preview is still fetching chapter data.\n\n"
                "Close ficary and cancel the preview? "
                "Cached chapters are kept either way."
            )
        elif kind == "search":
            title = "Search in progress"
            body = (
                "A search is still running.\n\n"
                "Close ficary and cancel the search?"
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
                    "Close ficary and cancel it? "
                    "Downloaded chapters stay cached, but any audio "
                    "synthesised so far will be discarded."
                )
            else:
                title = "Download in progress"
                body = (
                    "A download is still running.\n\n"
                    "Close ficary and cancel it? "
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

        # "Remind Me Later" stores a wake-up timestamp; suppress the
        # prompt until that's elapsed so a user who declined this
        # morning isn't pestered every relaunch for the same release.
        try:
            snoozed_until = float(self.prefs.get(_p.KEY_UPDATE_SNOOZED_UNTIL, 0) or 0)
        except (TypeError, ValueError):
            snoozed_until = 0
        if snoozed_until and time.time() < snoozed_until:
            return

        wx.CallAfter(self._prompt_update, info)

    def _prompt_update(self, info):
        from . import __version__
        from . import prefs as _p, self_update

        tag = info["tag"]
        size_mb = (info.get("size") or 0) / 1024 / 1024
        release_url = info.get("release_url") or ""

        if not self_update.can_self_replace():
            primary_label = "&Open Release Page"
            primary_result = "open_release"
            body = (
                f"Version {tag} is available (you have {__version__}).\n\n"
                f"Automatic update is only supported in the Windows build. "
                f"Open the release page to update manually?"
            )
        else:
            primary_label = "&Update Now"
            primary_result = "update"
            body = (
                f"Version {tag} is available. You currently have "
                f"{__version__}.\n\n"
                f"What will happen if you update:\n"
                f"  \u2022 ficary will download the new version (about "
                f"{size_mb:.0f} MB).\n"
                f"  \u2022 The app will close, replace itself, and reopen "
                f"automatically.\n"
                f"  \u2022 Your settings, cached chapters, and saved files "
                f"are untouched.\n"
                f"  \u2022 If the download fails or is cancelled, the "
                f"current version keeps running \u2014 nothing is changed "
                f"until the new file is fully downloaded.\n\n"
                f"Update now?"
            )

        result = _show_update_dialog(
            self,
            body=body,
            primary_label=primary_label,
            primary_result=primary_result,
            release_url=release_url,
        )

        if result == "update":
            self._perform_update(info)
        elif result == "open_release":
            if release_url:
                webbrowser.open(release_url)
        elif result == "release_notes":
            # User wanted to read the changelog before deciding. Open
            # the page and re-arm the snooze so we don't ambush them
            # again next launch \u2014 they'll come back to install when
            # they're ready.
            if release_url:
                webbrowser.open(release_url)
            self.prefs.set(
                _p.KEY_UPDATE_SNOOZED_UNTIL,
                int(time.time()) + _p.UPDATE_SNOOZE_S,
            )
        elif result == "skip":
            self.prefs.set(_p.KEY_SKIPPED_VERSION, tag)
        else:  # "later" or dialog dismissed without a button
            self.prefs.set(
                _p.KEY_UPDATE_SNOOZED_UNTIL,
                int(time.time()) + _p.UPDATE_SNOOZE_S,
            )

    def _perform_update(self, info):
        from . import self_update

        # Refuse to launch the updater while any background work is
        # active. _update_succeeded calls sys.exit(0) once the download
        # is done — by then ZipExtractor.exe is already blocked on our
        # PID, so we can't interactively cancel from there. The only
        # safe interaction point is before download starts. The check
        # mirrors the close-confirmation predicate so any path the
        # user's normal "X-out" prompt would catch also catches here.
        if self._has_active_background_work():
            wx.MessageBox(
                "Downloads, searches, or audiobook renders are still "
                "running.\n\nFinish or cancel them before updating — "
                "ficary has to close itself to swap in the new version, "
                "and that would interrupt any work in progress.",
                "Update blocked",
                wx.OK | wx.ICON_WARNING,
                self,
            )
            return

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
            # Read WasCancelled() directly so a user's Abort click is
            # observed even when the throttle below skips the
            # wx.CallAfter (e.g. during a single multi-second network
            # read where _apply_update never fires). Reading it from
            # the worker is safe enough — at worst we see a stale value
            # for one extra callback before noticing.
            if cancel_event.is_set() or progress.WasCancelled():
                cancel_event.set()
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
        # relaunches ficary.exe itself — do NOT call self_update.restart()
        # here, that would race the helper's relaunch.
        sys.exit(0)

    # ── Download ─────────────────────────────────────────────

    def _on_download(self, event):
        url = self.url_ctrl.GetValue().strip()
        if not url:
            self._log("Error: Please enter a story URL or ID.")
            return
        # Snapshot wx-widget settings on the main thread here so the
        # worker thread reads only from the immutable params object.
        # Snapshotting at enqueue time also pins the settings to what
        # the user saw when they clicked — a queued batch can no
        # longer get its format silently swapped if the user edits the
        # form between clicks.
        params = self._snapshot_download_params()
        if self._is_batch_url(url):
            # Batch flows still serialize globally — they pop a picker
            # dialog and then fan out to many downloads, which the
            # single-job queue model doesn't cover yet.
            if self._global_busy:
                return
            self._set_busy(True, kind="download")
            self._log(f"Starting download: {url}")
            threading.Thread(
                target=self._run_download, args=(url,),
                kwargs={"params": params}, daemon=True,
            ).start()
            return
        self._log(f"Starting download: {url}")
        self._enqueue_site_job(
            url, lambda u=url, p=params: self._run_download(u, params=p),
        )

    def _on_add_from_url_list(self, event):
        """Open the bulk URL-list picker; enqueue every fic the user
        ticks through the same per-site queue a single download uses."""
        from .gui_dialogs import AddFromUrlListDialog

        dlg = AddFromUrlListDialog(self)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            urls = dlg.picked_urls()
        finally:
            dlg.Destroy()
        if not urls:
            return
        # One snapshot for the whole batch — every queued fic uses the
        # settings that were active when the user OK'd the picker.
        params = self._snapshot_download_params()
        self._log(f"Add from URL list: enqueuing {len(urls)} fic(s).")
        for url in urls:
            self._enqueue_site_job(
                url,
                lambda u=url, p=params: self._run_download(u, params=p),
            )

    def _on_preview_voices(self, event):
        url = self.url_ctrl.GetValue().strip()
        if not url:
            self._log("Error: Enter a story URL or ID first.")
            return
        if self._global_busy:
            self._log("Already busy; finish the current job before previewing.")
            return
        # Snapshot output_dir on the main thread; the worker no longer
        # reads self.output_ctrl directly.
        output_dir = (self.output_ctrl.GetValue() or "").strip()
        self._log(f"Preview: fetching metadata for {url}")
        # Voice preview is a global-busy operation: it runs one off-
        # queue worker, opens a modal dialog at completion, and
        # populates the per-story voice map that the audiobook
        # generator reads back. The previous wiring routed it through
        # the per-site queue with ``kind="preview"``, but
        # ``_enqueue_site_job`` ignores ``kind`` — so close-confirmation
        # never showed the preview-specific prompt, the main buttons
        # stayed enabled, and concurrent previews on different sites
        # could pop overlapping dialogs. Now matches the
        # voice-dialog-driven semantics described in the close-
        # confirmation branch.
        self._set_busy(True, kind="preview")
        threading.Thread(
            target=self._run_preview_voices_with_busy,
            args=(url, output_dir), daemon=True,
        ).start()

    def _run_preview_voices_with_busy(self, url, output_dir: str):
        try:
            self._run_preview_voices(url, output_dir)
        finally:
            self._set_busy(False)

    def _run_preview_voices(self, url, output_dir: str):
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
            # output_dir is snapshotted on the main thread by
            # _on_preview_voices so this worker doesn't read
            # self.output_ctrl directly.
            preview_dir = Path(output_dir)
            preview_dir.mkdir(parents=True, exist_ok=True)
            map_path = _legacy.migrate_sidecar(preview_dir / f".ficary-voices-{story.id}.json")

            voices, mapper = tts.detect_voices(story, map_path=map_path)
            self._log(
                f"Detected {len(voices)} character(s). "
                f"Voice map: {map_path.name}"
            )
        except Exception as exc:
            self._log(f"Preview failed: {exc}")
            return

        wx.CallAfter(self._open_voice_dialog, voices, mapper, tts.NARRATOR_VOICE)

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

    def _on_update(self, event, *, refetch_all: bool = False):
        if self._global_busy:
            # A search or batch flow is running; Update would race
            # against the shared progress pane controls. Per-site
            # queue activity doesn't block Update — same-site updates
            # just queue behind the running job on that site.
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

        mode = " (fresh re-download)" if refetch_all else ""
        self._log(
            f"Updating{mode}: {url} (existing file has {existing} chapters)"
        )
        update_path = Path(path)
        # Snapshot params AFTER the SetSelection above so the snapshot
        # reflects the new format pinned from the file's suffix.
        params = self._snapshot_download_params()
        self._enqueue_site_job(
            url,
            lambda: self._run_download(
                url,
                skip_chapters=existing,
                is_update=True,
                update_path=update_path,
                refetch_all=refetch_all,
                params=params,
            ),
        )

    def _on_update_refetch_all(self, event):
        """Update handler that forces a full upstream re-fetch.

        Merge-in-place is the default for updates — it reads existing
        chapters back out of the local file instead of re-downloading
        them. This variant bypasses the shortcut for the (rare) case
        where an author silently revised old chapters and the user
        wants the refreshed text.
        """
        self._on_update(event, refetch_all=True)

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

    def _run_series_merge_download(
        self, series_url, *, series_name=None, part_urls=None,
        params: Optional[_DownloadParams] = None, manage_busy: bool = True,
    ):
        if params is None:
            params = self._snapshot_download_params()
        try:
            from .ao3 import AO3Scraper
            from .erotica import LiteroticaScraper
            from .merge import merge_stories

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
            failed = []
            for i, work_url in enumerate(work_urls, 1):
                self._log(f"\n[{i}/{len(work_urls)}] {work_url}")
                try:
                    work_scraper = self._scraper_for(work_url)
                    stories.append(
                        work_scraper.download(work_url, progress_callback=progress)
                    )
                except Exception as exc:
                    self._log(f"  Error: {exc}")
                    failed.append(work_url)

            if failed:
                self._log(
                    f"\n{len(failed)} of {len(work_urls)} part(s) failed and "
                    "were left out of the book:"
                )
                for u in failed:
                    self._log(f"  Failed: {u}")

            if not stories:
                self._log("Nothing downloaded.")
                return

            merged = merge_stories(series_name, series_url, stories)
            self._log(
                f"\nMerged {len(stories)} works / {len(merged.chapters)} sections"
            )
            path = self._export_story(merged, params)
            self._log(f"Saved: {path}")
        except Exception as exc:
            self._log(f"Series download failed: {exc}")
        finally:
            if manage_busy:
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
        # Wrap the whole read: on Linux/Wayland a flaky clipboard
        # manager can raise OSError mid-Open(); on macOS an in-flight
        # foreign copy can leave Open() returning False with a pending
        # request that the next read trips on. Without try/except an
        # exception here propagates up through the timer event handler
        # and silently kills the clipboard watch.
        text = ""
        try:
            if wx.TheClipboard.Open():
                try:
                    if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_TEXT)):
                        data = wx.TextDataObject()
                        wx.TheClipboard.GetData(data)
                        text = data.GetText().strip()
                finally:
                    wx.TheClipboard.Close()
        except Exception:
            logger.debug("clipboard read failed", exc_info=True)
            return ""
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

        if self._is_batch_url(url):
            # Batch URLs still pop a picker on the main thread; the
            # clipboard watcher firing one mid-poll would be jarring.
            # Don't mark the URL as seen yet — if we drop it because
            # the app is busy, a subsequent clipboard hit (or the user
            # re-copying after the download finishes) needs to be able
            # to re-trigger it. Marking pre-busy permanently blacklisted
            # the URL for the rest of the watch session.
            if self._global_busy:
                self._log(f"Skipped (busy): {url}")
                return
            self._watch_seen.add(url)
            self._log(f"Clipboard detected: {url}")
            self.url_ctrl.SetValue(url)
            self._set_busy(True, kind="download")
            params = self._snapshot_download_params()
            threading.Thread(
                target=self._run_download, args=(url,),
                kwargs={"params": params}, daemon=True,
            ).start()
            return

        self._watch_seen.add(url)

        self._log(f"Clipboard detected: {url}")
        self.url_ctrl.SetValue(url)
        params = self._snapshot_download_params()
        self._enqueue_site_job(
            url,
            lambda u=url, p=params: self._run_download(u, params=p),
        )

    # ── Download worker ──────────────────────────────────────

    def _scraper_for(self, url, *, use_fichub=False, webnovel_cookie="", ao3_cookie=""):
        from .sites import detect_scraper
        cls = detect_scraper(url)
        # use_fichub / *_cookie are per-site constructor kwargs; only forward
        # each to the scraper that accepts it so other site scrapers don't
        # choke on an unexpected kwarg.
        if use_fichub:
            from .scraper import FFNScraper
            if cls is FFNScraper:
                return cls(use_fichub=True)
        if webnovel_cookie:
            from .webnovel import WebnovelScraper
            if cls is WebnovelScraper:
                return cls(session_cookie=webnovel_cookie)
        if ao3_cookie:
            from .ao3 import AO3Scraper
            if cls is AO3Scraper:
                return cls(session_cookie=ao3_cookie)
        return cls()

    def _snapshot_download_params(self) -> _DownloadParams:
        """MAIN THREAD ONLY. Bundle every wx-widget setting a worker
        thread might need into an immutable :class:`_DownloadParams`.

        Call this just before queueing/spawning a download worker so
        the worker reads from the snapshot — never from
        ``self.<x>_ctrl`` directly. Batches snapshot once and reuse
        the same params for every item in the batch so changing the
        format dropdown mid-batch doesn't retroactively change which
        format old queued items export as.
        """
        fmt = self.format_ctrl.GetString(self.format_ctrl.GetSelection())
        strip_notes = self.strip_notes_ctrl.GetValue()
        llm_strip_notes = (
            strip_notes and self.llm_strip_notes_ctrl.GetValue()
        )
        return _DownloadParams(
            fmt=fmt,
            raw_output_dir=(self.output_ctrl.GetValue() or "").strip(),
            filename_template=self.name_ctrl.GetValue(),
            hr_as_stars=self.hr_stars_ctrl.GetValue(),
            strip_notes=strip_notes,
            llm_strip_notes=llm_strip_notes,
            llm_render_config=(
                self._llm_config_for_render() if llm_strip_notes else None
            ),
            audio_backend=(
                self._selected_attribution_backend() if fmt == "audio" else None
            ),
            audio_size=(
                self._selected_size() if fmt == "audio" else None
            ),
            speech_rate=(
                self.speech_rate_ctrl.GetValue() if fmt == "audio" else None
            ),
            enabled_tts_providers=(
                tuple(self._enabled_tts_providers()) if fmt == "audio" else ()
            ),
            use_fichub=self.fichub_ctrl.GetValue(),
            merge_series=self.merge_series_ctrl.GetValue(),
            webnovel_cookie=self.webnovel_cookie_ctrl.GetValue().strip(),
            ao3_cookie=self.ao3_cookie_ctrl.GetValue().strip(),
        )

    def _resolve_output_dir(self, story, params: _DownloadParams) -> str:
        """Pick the save folder for ``story``, auto-routing into the
        library's fandom subfolder when the user's Save-to folder
        matches the configured library root.

        Reads from a :class:`_DownloadParams` snapshot so worker
        threads don't touch wx widgets — see ``_snapshot_download_params``.

        Mirrors the CLI's ``_apply_library_autosort`` +
        ``_library_subdir_for`` behaviour so library-wide downloads
        sort the same way whether you start them from the GUI or the
        command line. If a fandom subfolder is missing, the user is
        asked once per-session before we create it — the answer is
        cached on ``_fandom_folder_decisions`` so subsequent downloads
        of the same fandom don't re-prompt.
        """
        from . import cli, prefs as _p

        raw_output = params.raw_output_dir
        if not raw_output:
            return raw_output
        base = Path(raw_output).expanduser()

        library_raw = (self.prefs.get(_p.KEY_LIBRARY_PATH, "") or "").strip()
        if not library_raw:
            return str(base)
        library_root = Path(library_raw).expanduser()
        try:
            same_root = base.resolve() == library_root.resolve()
        except OSError:
            same_root = str(base) == str(library_root)
        # Save-to is inside the library tree (root or a subfolder) when
        # we should let auto-sort steer adult/original adapter
        # downloads into their dedicated bucket. A save-to *outside*
        # the library root is the user pointing at a staging dir
        # explicitly and gets respected verbatim.
        try:
            base_resolved = base.resolve()
            library_resolved = library_root.resolve()
            inside_library = (
                base_resolved == library_resolved
                or library_resolved in base_resolved.parents
            )
        except OSError:
            inside_library = same_root
        if not inside_library:
            return str(base)

        from types import SimpleNamespace
        args_like = SimpleNamespace(
            output=str(library_root),
            _library_autosort=True,
            _library_template=(
                self.prefs.get(_p.KEY_LIBRARY_PATH_TEMPLATE)
                or "{fandom}/{title} - {author}.{ext}"
            ),
            _library_misc=(
                self.prefs.get(_p.KEY_LIBRARY_MISC_FOLDER) or "Misc"
            ),
            # Mirror the CLI's autosort namespace so adult and
            # original-fiction adapter downloads route to the user's
            # configured bucket names rather than the hardcoded
            # defaults baked into cli._library_subdir_for.
            _library_adult=(
                self.prefs.get(_p.KEY_LIBRARY_ADULT_FOLDER) or "Adult"
            ),
            _library_original=(
                self.prefs.get(_p.KEY_LIBRARY_ORIGINAL_FOLDER)
                or "Original Works"
            ),
            format=params.fmt,
        )
        subdir = cli._library_subdir_for(story, args_like)

        # Adult/original adapters with an inside-library save-to get
        # routed into their dedicated bucket regardless of whether the
        # user picked the library root or a subfolder. The save-target
        # gate used to require an exact root match, which meant any
        # remembered subfolder (e.g. last-used "Harry Potter/") silently
        # bypassed adult routing and put erotica downloads into the
        # fandom folder — the documented misfiling complaint. Adult
        # intent is unambiguous when the URL is an adult-site URL, so
        # we just do the right thing without prompting.
        if not same_root and subdir is not None:
            from .library.identifier import adapter_for_url
            from .library.template import (
                ADULT_FICTION_ADAPTERS, ORIGINAL_FICTION_ADAPTERS,
            )
            adapter = adapter_for_url(story.url or "")
            if adapter in ADULT_FICTION_ADAPTERS:
                bucket = (
                    self.prefs.get(_p.KEY_LIBRARY_ADULT_FOLDER) or "Adult"
                )
                target = library_root / bucket
                target.mkdir(parents=True, exist_ok=True)
                return str(target)
            if adapter in ORIGINAL_FICTION_ADAPTERS:
                bucket = (
                    self.prefs.get(_p.KEY_LIBRARY_ORIGINAL_FOLDER)
                    or "Original Works"
                )
                target = library_root / bucket
                target.mkdir(parents=True, exist_ok=True)
                return str(target)
            # Non-adult adapter inside a library subfolder: respect
            # whatever the user picked, don't second-guess.
            return str(base)
        if subdir is None or str(subdir) in ("", "."):
            return str(library_root)

        target = library_root / subdir
        if target.is_dir():
            return str(target)

        # First-time fandom: ask the user before creating the folder.
        # Cache their answer (per-fandom, per-session) so we don't
        # re-ask on the second Harry Potter fic of the session.
        fandom_key = str(subdir)
        decision = self._fandom_folder_decisions.get(fandom_key)
        if decision is None:
            decision = self._ask_create_fandom_folder(fandom_key, target)
            self._fandom_folder_decisions[fandom_key] = decision
        if decision:
            try:
                target.mkdir(parents=True, exist_ok=True)
                return str(target)
            except OSError as exc:
                self._log(
                    f"Could not create {target}: {exc}. "
                    "Saving into library root instead."
                )
        return str(library_root)

    def _ask_create_fandom_folder(self, fandom_key: str, target) -> bool:
        """Prompt the user to create a missing fandom subfolder.

        Runs on the main thread — every download worker funnels through
        ``_export_story`` via ``wx.CallAfter``-scheduled callbacks, but
        the export itself happens from the worker, so we marshal the
        message box explicitly. Returns True if the user accepted.
        """
        import threading as _th

        answer = {"value": False}
        done = _th.Event()

        def prompt():
            choice = wx.MessageBox(
                f"Create folder '{fandom_key}' for this fandom?\n\n"
                f"Full path:\n{target}",
                "New fandom folder",
                wx.YES_NO | wx.ICON_QUESTION,
                self,
            )
            answer["value"] = choice == wx.YES
            done.set()

        if wx.IsMainThread():
            prompt()
        else:
            with self._pending_worker_dialogs_lock:
                self._pending_worker_dialogs.add(done)
            try:
                wx.CallAfter(prompt)
                # Two minutes is well past any human "click yes/no"
                # reaction time. ``_on_close`` also sets ``done`` for
                # every registered event so a Quit-during-download
                # wakes us instantly — the timeout is just the ultimate
                # backstop for the (now extremely rare) case where the
                # frame is gone but no close handler fired.
                if not done.wait(timeout=120):
                    logger.debug(
                        "Fandom-folder prompt timed out (frame likely "
                        "destroyed); treating as 'no'."
                    )
            finally:
                with self._pending_worker_dialogs_lock:
                    self._pending_worker_dialogs.discard(done)
        return answer["value"]

    def _maybe_offer_library_as_default(self) -> None:
        """One-time offer: if the user has scanned a library but hasn't
        set it as their default download folder, ask once.

        Silent no-op in every other case: already asked, already set,
        no library scanned, no scanned root with stories. The pref
        flag is flipped regardless of the user's answer so we don't
        nag on every launch.
        """
        from . import prefs as _p

        if self.prefs.get_bool(_p.KEY_LIBRARY_DEFAULT_PROMPTED):
            return
        if (self.prefs.get(_p.KEY_LIBRARY_PATH, "") or "").strip():
            return
        try:
            from .library.index import LibraryIndex
        except Exception:
            return
        try:
            idx = LibraryIndex.load()
            roots = idx.library_roots()
        except Exception:
            return
        # Pick the root with the most tracked stories — that's
        # overwhelmingly the user's real library even if they poked
        # one-off scans elsewhere.
        best_root: str | None = None
        best_count = 0
        for root_str in roots:
            try:
                count = sum(1 for _ in idx.stories_in(Path(root_str)))
            except Exception:
                count = 0
            if count > best_count:
                best_count = count
                best_root = root_str
        if not best_root or best_count == 0:
            return

        choice = wx.MessageBox(
            (
                f"You have a scanned library at:\n\n{best_root}\n\n"
                f"({best_count} tracked stor"
                f"{'y' if best_count == 1 else 'ies'})\n\n"
                "Use this folder as your default save location so new "
                "downloads sort into the right fandom folder "
                "automatically?"
            ),
            "ficary — Set library as default?",
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        )
        self.prefs.set_bool(_p.KEY_LIBRARY_DEFAULT_PROMPTED, True)
        if choice == wx.YES:
            self.prefs.set(_p.KEY_LIBRARY_PATH, best_root)
            self.output_ctrl.SetValue(best_root)
            self.prefs.set(_p.KEY_OUTPUT_DIR, best_root)
            self._log(f"Library root set: {best_root}")

    def _export_story(self, story, params: _DownloadParams):
        """Run the configured exporter for ``story`` using the snapshot
        in ``params``.

        Worker-thread safe: every value that used to be read live from
        ``self.<x>_ctrl`` now comes from the immutable snapshot. Audio
        attribution backend/size + LLM A/N config are pre-resolved on
        the main thread inside ``_snapshot_download_params`` so this
        path never has to call ``self._selected_attribution_backend``
        or ``self._llm_config_for_render`` off-main.
        """
        output_dir = self._resolve_output_dir(story, params)

        if params.fmt == "audio":
            from .tts import generate_audiobook

            def audio_progress(current, total, title):
                self._log(f"  Synthesizing [{current}/{total}] {title}")

            backend = params.audio_backend or "builtin"
            size = params.audio_size
            rate = params.speech_rate if params.speech_rate is not None else 0
            # Reuse the same LLM config the A/N strip path uses when the
            # selected attribution backend is "llm". The two snapshot
            # slots are kept distinct because A/N strip can be on
            # without LLM attribution being on.
            llm_config = (
                params.llm_render_config if backend == "llm" else None
            )
            size_note = f", size={size}" if size else ""
            llm_note = (
                f", llm={llm_config['provider']}/{llm_config['model']}"
                if llm_config else ""
            )
            self._log(
                f"\nGenerating audiobook (attribution={backend}{size_note}{llm_note}, "
                f"rate={rate:+d}%)..."
            )
            return generate_audiobook(
                story, output_dir,
                progress_callback=audio_progress,
                speech_rate=rate,
                attribution_backend=backend,
                attribution_model_size=size,
                attribution_llm_config=llm_config,
                enabled_tts_providers=list(params.enabled_tts_providers),
                strip_notes=params.strip_notes,
                hr_as_stars=params.hr_as_stars,
            )

        from .exporters import EXPORTERS
        exporter = EXPORTERS[params.fmt]
        an_llm_config = params.llm_render_config if params.llm_strip_notes else None
        if an_llm_config:
            self._log(
                f"  LLM A/N strip: {an_llm_config['provider']}/"
                f"{an_llm_config['model']} (one call per chapter)"
            )
        return exporter(
            story, output_dir, template=params.filename_template,
            hr_as_stars=params.hr_as_stars, strip_notes=params.strip_notes,
            llm_config=an_llm_config,
            progress=self._log,
        )

    def _run_download(
        self, url, skip_chapters=0, is_update=False,
        update_path=None, refetch_all=False,
        params: Optional[_DownloadParams] = None,
    ):
        # ``params`` MUST be snapshotted on the main thread by the
        # caller. If None (legacy/test/internal callers that haven't
        # threaded params through yet), snapshot here as a fallback —
        # but flag it for cleanup so we eventually remove the off-main
        # widget reads completely.
        if params is None:
            logger.debug(
                "_run_download called without params; snapshotting on "
                "current thread (legacy path)."
            )
            params = self._snapshot_download_params()
        try:
            from .ao3 import AO3Scraper
            from .erotica import LiteroticaScraper
            from .updater import ChaptersNotReadableError, read_chapters

            # FicHub fast-path only on fresh downloads — updates and
            # fresh-copies re-pulls must read from the source. The
            # scraper itself also guards on skip_chapters, but gating
            # here keeps the picker/series branches below on plain
            # scrapers too.
            scraper = self._scraper_for(
                url, use_fichub=(params.use_fichub and not is_update),
                webnovel_cookie=params.webnovel_cookie,
                ao3_cookie=params.ao3_cookie,
            )

            if not is_update and AO3Scraper.is_bookmarks_url(url):
                self._run_picker_download(
                    url, AO3Scraper(), kind="bookmarks", params=params,
                )
                return

            if not is_update and scraper.is_author_url(url):
                self._run_picker_download(
                    url, scraper, kind="author", params=params,
                )
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
                if params.merge_series:
                    # Runs synchronously on this (raw) thread; let
                    # _run_download's own finally clear busy so it isn't
                    # cleared twice.
                    self._run_series_merge_download(
                        url, params=params, manage_busy=False
                    )
                else:
                    self._run_series_download(url, scraper, params=params)
                return

            scraper.parse_story_id(url)

            def progress(current, total, title, cached):
                tag = " (cached)" if cached else ""
                self._log(f"  [{current}/{total}] {title}{tag}")

            # Fresh-copies updates always re-fetch the whole story, so
            # skipping the existing chapters on the initial download is
            # a wasted round-trip — we'd pull just the new chapters,
            # then immediately fetch every chapter from 1 again. Fetch
            # the full story directly and skip the merge helper below.
            if is_update and refetch_all:
                self._log("Fresh-copies mode — re-downloading every chapter.")
                initial_skip = 0
            else:
                initial_skip = skip_chapters
            story = scraper.download(
                url, progress_callback=progress, skip_chapters=initial_skip,
            )

            if is_update and not refetch_all and len(story.chapters) == 0:
                self._log("Up to date. No new chapters.")
                return

            if is_update and not refetch_all:
                new_count = len(story.chapters)
                # Merge-in-place: read existing chapters from disk and
                # concatenate with the just-downloaded new ones. Avoids
                # re-downloading every chapter from upstream just to
                # re-export, which used to silently take minutes per
                # story on libraries without a populated local cache.
                merged = None
                if update_path is not None:
                    try:
                        merged = read_chapters(update_path)
                    except ChaptersNotReadableError as exc:
                        self._log(
                            f"Couldn't read existing chapters ({exc}); "
                            "re-downloading full story..."
                        )

                if merged is not None:
                    self._log(
                        f"Found {new_count} new chapter(s). Merging "
                        f"with {len(merged)} existing."
                    )
                    story.chapters = sorted(
                        merged + list(story.chapters), key=lambda c: c.number,
                    )
                else:
                    self._log(
                        f"Found {new_count} new chapters. Re-exporting..."
                    )
                    story = scraper.download(
                        url, progress_callback=progress, skip_chapters=0,
                    )

            self._log(f"\n  Title:    {story.title}")
            self._log(f"  Author:   {story.author}")
            self._log(f"  Chapters: {len(story.chapters)}")

            path = self._export_story(story, params)
            self._log(f"\nDone! Saved to: {path}")

        except Exception as e:
            self._log(f"\nError: {e}")
        finally:
            # Legacy raw-thread callers (batch/series/author dispatch)
            # set ``_global_busy`` before spawning the thread and rely
            # on this clear-on-exit. Per-site queue workers run on
            # ``dlq-*`` threads and manage busy state via the queue
            # listener, so clearing ``_global_busy`` here would
            # clobber an unrelated in-flight search or batch.
            #
            # ``_picker_transferred_busy`` consumes-and-clears here:
            # the picker handler spawned a ``_run_picked_batch`` thread
            # that now owns the busy state, and that thread's own
            # ``finally`` clears it on completion. Clearing busy here
            # would race the user clicking Download again before the
            # batch starts.
            transferred = self._picker_transferred_busy
            self._picker_transferred_busy = False
            if (
                not transferred
                and not threading.current_thread().name.startswith(
                    WORKER_THREAD_PREFIX
                )
            ):
                self._set_busy(False)

    def _run_series_download(self, url, scraper, *, params: Optional[_DownloadParams] = None):
        self._log(f"Fetching series: {url}")
        series_name, work_urls = scraper.scrape_series_works(url)
        if not work_urls:
            self._log("No works found in this series.")
            return
        self._log(f"Series: {series_name}")
        self._log(f"Found {len(work_urls)} works. Downloading in series order...")
        self._batch_download(
            work_urls, scraper, summary_label="Series", params=params,
        )

    def _run_author_download(self, url, scraper, *, params: Optional[_DownloadParams] = None):
        self._log(f"Fetching author page: {url}")
        author_name, story_urls = scraper.scrape_author_stories(url)
        if not story_urls:
            self._log("No stories found on the author page.")
            return
        self._log(f"Author: {author_name}")
        self._log(f"Found {len(story_urls)} stories. Downloading all...")
        self._batch_download(
            story_urls, scraper, summary_label="Author batch", params=params,
        )

    def _run_picker_download(self, url, scraper, *, kind, params: Optional[_DownloadParams] = None):
        """Fetch a work list (author page or AO3 bookmarks) and open the
        picker so the user can choose which works to download before we
        start pulling chapters.

        Blocks the worker thread on a ``threading.Event`` until the
        modal picker resolves. Without that block, the worker's outer
        ``finally`` clause cleared ``_global_busy`` while the picker
        was still on screen, briefly leaving the app in an "idle"
        state — clipboard watcher and double-clicks could fire a
        second download into the gap.
        """
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

        picker_done = threading.Event()
        with self._pending_worker_dialogs_lock:
            self._pending_worker_dialogs.add(picker_done)

        def _handle_selection(selected_urls):
            spawned = False
            try:
                if not selected_urls:
                    self._log("(No selections — nothing downloaded.)")
                    return
                self._log(f"Downloading {len(selected_urls)} selected...")
                # Stay global-busy through the batch — the worker
                # thread we spawn here will clear it on exit.
                self._set_busy(True, kind="download")
                # Re-snapshot params on the main thread here (we ARE
                # on it inside _handle_selection) rather than capturing
                # the picker-open-time snapshot, since the user could
                # have flipped settings while the picker was up.
                batch_params = params or self._snapshot_download_params()
                threading.Thread(
                    target=self._run_picked_batch,
                    args=(selected_urls, kind),
                    kwargs={"params": batch_params},
                    daemon=True,
                ).start()
                spawned = True
            finally:
                # Signal the outer ``_run_download`` finally to leave
                # ``_global_busy`` alone when a batch follow-up is now
                # carrying it. Has to land BEFORE ``picker_done.set()``
                # so the worker thread observes the write under the
                # Event's happens-before relationship.
                if spawned:
                    self._picker_transferred_busy = True
                picker_done.set()

        wx.CallAfter(self._open_picker, title, works, _handle_selection)
        # Block here so the worker's outer ``finally`` doesn't clear
        # ``_global_busy`` while the picker is still on screen. The
        # event is also registered in ``_pending_worker_dialogs`` so
        # ``_on_close`` can wake us instantly on app shutdown.
        picker_done.wait()
        with self._pending_worker_dialogs_lock:
            self._pending_worker_dialogs.discard(picker_done)

    def _open_picker(self, title, works, on_ok):
        # ``on_ok`` is called UNCONDITIONALLY exactly once. The worker
        # thread that triggered this dialog is blocked on
        # ``picker_done.wait()`` inside ``_run_picker_download`` and
        # ``on_ok`` is what sets that event — if dialog construction or
        # ``ShowModal`` raises and we skip ``on_ok``, the worker leaks
        # forever (no timeout on the wait). Even on a hard error the
        # worker has to be released with an empty selection so its
        # outer finally runs.
        picked_urls: list[str] = []
        try:
            dlg = StoryPickerDialog(self, title, works, prefs=self.prefs)
            try:
                if dlg.ShowModal() == wx.ID_OK:
                    picked_urls = dlg.picked_urls()
            finally:
                dlg.Destroy()
        except Exception as exc:
            logger.exception("Story picker dialog failed: %s", exc)
            self._log(f"Could not show picker: {exc}")
        finally:
            on_ok(picked_urls)

    def _run_picked_batch(self, urls, kind, *, params: Optional[_DownloadParams] = None):
        if params is None:
            # Should never happen on the production path (the picker
            # handler snapshots before spawning), but tests/synthetic
            # callers without a main-thread snapshot still get a
            # working fallback.
            params = self._snapshot_download_params()
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
                    path = self._export_story(story, params)
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

    def _batch_download(
        self, story_urls, scraper, summary_label="Batch",
        *, params: Optional[_DownloadParams] = None,
    ):
        if params is None:
            params = self._snapshot_download_params()

        def progress(current, total, title, cached):
            tag = " (cached)" if cached else ""
            self._log(f"    [{current}/{total}] {title}{tag}")

        succeeded = 0
        failed = []
        for i, story_url in enumerate(story_urls, 1):
            self._log(f"\n[{i}/{len(story_urls)}] {story_url}")
            try:
                story = scraper.download(story_url, progress_callback=progress)
                path = self._export_story(story, params)
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
        #
        # Literotica used to have its own entry on Ctrl+4 but is now
        # part of the unified Erotic Story Search — the standalone
        # frame was a narrower version of the fan-out, and carrying
        # both caused user confusion about which one to open when
        # looking for a Literotica tag. The accelerators shift up by
        # one so the menu stays contiguous; Wattpad moves to Ctrl+4,
        # Erotic Story Search takes Ctrl+5.
        ("Ctrl+1", "ffn", _ffn_search_spec, "Search &FFN..."),
        ("Ctrl+2", "ao3", _ao3_search_spec, "Search &AO3..."),
        ("Ctrl+3", "royalroad", _royalroad_search_spec, "Search &Royal Road..."),
        ("Ctrl+4", "wattpad", _wattpad_search_spec, "Search &Wattpad..."),
        (
            "Ctrl+5", "erotica", _erotica_search_spec,
            "&Erotic Story Search (all sites, incl. Literotica)...",
        ),
    )

    def _build_menu_bar(self):
        bar = wx.MenuBar()

        file_menu = wx.Menu()
        update_item = file_menu.Append(
            wx.ID_ANY, "&Update File...\tCtrl+U",
        )
        self.Bind(wx.EVT_MENU, self._on_update, update_item)
        update_fresh_item = file_menu.Append(
            wx.ID_ANY,
            "Update File with &Fresh Copy...\tCtrl+Shift+U",
        )
        self.Bind(
            wx.EVT_MENU, self._on_update_refetch_all, update_fresh_item,
        )
        file_menu.AppendSeparator()
        add_from_list_item = file_menu.Append(
            wx.ID_ANY, "Add from &URL list...\tCtrl+Shift+L",
        )
        self.Bind(
            wx.EVT_MENU, self._on_add_from_url_list, add_from_list_item,
        )
        file_menu.AppendSeparator()
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
        edit_menu.AppendSeparator()
        features_item = edit_menu.Append(
            wx.ID_ANY, "Optional &Features...",
        )
        self.Bind(wx.EVT_MENU, self._on_optional_features_menu, features_item)
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

        reader_menu = wx.Menu()
        reader_item = reader_menu.Append(
            wx.ID_ANY, "&Open reader...\tCtrl+R",
        )
        self.Bind(wx.EVT_MENU, self._on_reader_menu, reader_item)
        soundscape_item = reader_menu.Append(
            wx.ID_ANY, "&Soundscape editor...",
        )
        self.Bind(wx.EVT_MENU, self._on_soundscape_editor, soundscape_item)
        bar.Append(reader_menu, "&Reader")

        watchlist_menu = wx.Menu()
        watchlist_item = watchlist_menu.Append(
            wx.ID_ANY, "&Manage watchlist...\tCtrl+W",
        )
        self.Bind(wx.EVT_MENU, self._on_watchlist_menu, watchlist_item)
        bar.Append(watchlist_menu, "&Watchlist")

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
        about_item = help_menu.Append(wx.ID_ABOUT, "&About Ficary")
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

    def _on_optional_features_menu(self, event):
        dlg = OptionalFeaturesDialog(self)
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
        self.llm_strip_notes_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_LLM_STRIP_NOTES)
        )
        self.fichub_ctrl.SetValue(self.prefs.get_bool(_p.KEY_FICHUB))
        self.merge_series_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_MERGE_SERIES)
        )

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
        """Open the library-management window (non-modal).

        Modeless so users can kick off a scan or update run and flip
        back to the main window to download something else in the
        meantime. Lazy import keeps gui.py's startup cost unaffected
        for users who never touch the library features.
        """
        if self._library_frame is not None:
            try:
                self._library_frame.Raise()
                self._library_frame.SetFocus()
                return
            except RuntimeError:
                # Frame was destroyed without going through our
                # closed-notify callback — reset and open a fresh one.
                self._library_frame = None

        from .library.gui import LibraryFrame

        frame = LibraryFrame(self, self.prefs)
        self._library_frame = frame
        frame.Show()

    def _notify_library_frame_closed(self):
        """Called by LibraryFrame._on_close so the menu reopens cleanly."""
        self._library_frame = None

    def _on_reader_menu(self, event):
        """Open a downloaded story (EPUB/HTML) in the in-app reader."""
        with wx.FileDialog(
            self, "Open a downloaded story to read",
            wildcard="Stories (*.epub;*.html;*.htm)|*.epub;*.html;*.htm",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        self._open_reader_for_file(path)

    def _open_reader_for_file(self, path, *, url="", title="", author=""):
        """Build a StorySource from an exported file and show the reader."""
        from .reader.source import StorySource, ReaderSourceError
        try:
            source = StorySource.from_file(path, url=url, title=title, author=author)
        except (ReaderSourceError, OSError) as exc:
            wx.MessageBox(str(exc), "Reader", wx.OK | wx.ICON_ERROR, self)
            return
        self._show_reader(source)

    def _show_reader(self, source):
        """Show the reader for a story, replacing any currently-open one."""
        if self._reader_frame is not None:
            try:
                self._reader_frame.Close()
            except RuntimeError:
                pass
            self._reader_frame = None
        from .reader.gui import ReaderFrame
        frame = ReaderFrame(self, self.prefs, source)
        self._reader_frame = frame
        frame.Show()

    def _notify_reader_frame_closed(self):
        self._reader_frame = None

    def _on_soundscape_editor(self, event):
        """Open the soundscape editor (manage ambient audio definitions)."""
        from .soundscape.editor import SoundscapeEditorDialog
        with SoundscapeEditorDialog(self) as dlg:
            dlg.ShowModal()

    def _on_watchlist_menu(self, event):
        """Open the watchlist manager (non-modal). Reuses the same
        frame on re-invocation so Ctrl+W doesn't spawn duplicates.
        """
        if self._watchlist_frame is not None:
            try:
                self._watchlist_frame.Raise()
                self._watchlist_frame.SetFocus()
                return
            except RuntimeError:
                # The frame was destroyed without going through our
                # closed-notify callback — reset and open a new one.
                self._watchlist_frame = None

        from .gui_watchlist import WatchlistFrame

        frame = WatchlistFrame(self)
        self._watchlist_frame = frame
        frame.Show()

    def _notify_watchlist_frame_closed(self):
        """Called by WatchlistFrame._on_close so Ctrl+W reopens cleanly."""
        self._watchlist_frame = None

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
                    "You have the latest version of ficary.",
                    "Check for Updates",
                    wx.OK | wx.ICON_INFORMATION, self,
                )
                return
            # User asked explicitly — clear any previously-skipped
            # version and any in-flight snooze so the prompt actually
            # shows up regardless of how recently they hit "Remind Me
            # Later" or "Skip This Version".
            from . import prefs as _p
            self.prefs.set(_p.KEY_SKIPPED_VERSION, "")
            self.prefs.set(_p.KEY_UPDATE_SNOOZED_UNTIL, 0)
            wx.CallAfter(self._prompt_update, info)

        threading.Thread(target=worker, daemon=True).start()

    def _on_about(self, event):
        import wx.adv
        from . import __version__
        info = wx.adv.AboutDialogInfo()
        info.SetName("Ficary")
        info.SetVersion(__version__)
        info.SetDescription(
            "Cross-platform fanfiction downloader.\n\n"
            "Supports FanFiction.Net, Archive of Our Own, FicWad, "
            "Royal Road, MediaMiner, Literotica, and Wattpad. "
            "Exports to EPUB, HTML, TXT, and character-voiced audiobooks."
        )
        info.SetWebSite("https://github.com/matalvernaz/ficary")
        info.SetCopyright("(c) Matthew Alvernaz")
        wx.adv.AboutBox(info, self)

    def _on_open_manual(self, event):
        """Open the project README (the user-facing manual) in the browser."""
        webbrowser.open("https://github.com/matalvernaz/ficary#readme")


def main():
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
