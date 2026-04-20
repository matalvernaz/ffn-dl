"""Unified Preferences dialog.

Before this dialog existed, preferences were scattered across the main
download form (format, name template, output dir, HR-as-stars, strip
notes, speech rate, attribution backend/size), the View menu (log
level, save log to file), the File menu (warn before closing), and
the Library dialog. A handful of keys — ``check_updates``, the
Pushover/Discord/email notification credentials, the watchlist poll
interval — had no GUI at all and required editing ``settings.ini``
by hand.

This dialog consolidates those knobs into one tabbed window reachable
from ``Edit → Preferences`` (Ctrl+,). Values mirrored on the main form
(format, filename template, output dir, scene-break marker, strip
notes, speech rate, attribution) are written back to both the
persistent pref *and* the live form control so a change takes effect
immediately without requiring a restart.

The watchlist autopoll keys (``KEY_WATCH_AUTOPOLL`` /
``KEY_WATCH_POLL_INTERVAL_S``) are intentionally *not* surfaced here —
the GUI doesn't yet run a background watchlist thread, so toggling
them would be a no-op until that lands.
"""

from __future__ import annotations

import logging

import wx

from . import attribution as _attribution_module
from . import prefs as _p


logger = logging.getLogger(__name__)


_FORMAT_CHOICES = ["epub", "html", "txt", "audio"]
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]

# Presets shown in the watchlist poll-interval dropdown — even though
# the autopoll toggle itself isn't surfaced yet, keeping the constants
# here (dead code aside) would risk drifting from prefs.py. They stay
# in prefs.py and get referenced lazily only if/when the tab is added.

# (seconds, label) pairs. Dialog widgets are rebuilt each open so
# adding an interval here is enough — no persisted indices to migrate.
_PUSHOVER_HELP = (
    "Pushover delivers watchlist alerts to your phone. Create an "
    "application at https://pushover.net/apps/build to get an API "
    "token; your user key is shown on your Pushover dashboard. Leave "
    "both blank to disable."
)
_DISCORD_HELP = (
    "Discord webhook URL — server channel settings → Integrations → "
    "Webhooks → New Webhook. Leave blank to disable."
)
_EMAIL_HELP = (
    "Recipient address for watchlist email alerts. Uses the same SMTP "
    "credentials as 'Send to Kindle' (configured via CLI "
    "--send-to-kindle). Leave blank to disable."
)


class PreferencesDialog(wx.Dialog):
    """Tabbed preferences dialog. Opens non-modally friendly (standard
    modal dialog with OK/Cancel). The owning MainFrame is responsible
    for syncing live UI controls after OK — see ``apply_to_main_frame``.
    """

    def __init__(self, parent, prefs, main_frame=None):
        super().__init__(
            parent, title="Preferences",
            size=(640, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.prefs = prefs
        self.main_frame = main_frame

        self._build_ui()
        self._load_values()
        self.Centre()

    # ── UI construction ─────────────────────────────────────────

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = wx.Notebook(panel)
        self.notebook.AddPage(self._build_general_tab(), "&General")
        self.notebook.AddPage(self._build_downloads_tab(), "&Downloads")
        self.notebook.AddPage(self._build_audiobook_tab(), "&Audiobook")
        self.notebook.AddPage(self._build_notifications_tab(), "&Notifications")
        self.notebook.AddPage(self._build_logging_tab(), "&Logging")
        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, "&OK")
        ok_btn.SetDefault()
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        btn_row.AddButton(ok_btn)
        btn_row.AddButton(cancel_btn)
        btn_row.Realize()
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _make_labeled_row(self, parent, label, ctrl, *, help_text=None):
        """Label + control on one row; optional help text under it."""
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(
            wx.StaticText(parent, label=label),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        return row

    def _add_help_text(self, sizer, parent, text):
        """Wrapped small-print explanatory text below a field group."""
        st = wx.StaticText(parent, label=text)
        st.Wrap(560)
        font = st.GetFont()
        font.SetPointSize(max(8, font.GetPointSize() - 1))
        st.SetFont(font)
        sizer.Add(st, 0, wx.EXPAND | wx.ALL, 4)

    # ── Tabs ────────────────────────────────────────────────────

    def _build_general_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Output directory
        dir_row = wx.BoxSizer(wx.HORIZONTAL)
        dir_row.Add(
            wx.StaticText(panel, label="Default &output folder:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.output_dir_ctrl = wx.TextCtrl(panel)
        self.output_dir_ctrl.SetName("Default output folder")
        dir_row.Add(self.output_dir_ctrl, 1, wx.RIGHT, 4)
        browse_btn = wx.Button(panel, label="Bro&wse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_output)
        dir_row.Add(browse_btn, 0)
        sizer.Add(dir_row, 0, wx.EXPAND | wx.ALL, 6)

        # Filename template
        self.name_template_ctrl = wx.TextCtrl(panel)
        self.name_template_ctrl.SetName("Default filename template")
        sizer.Add(
            self._make_labeled_row(
                panel, "Default &filename template:", self.name_template_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )
        self._add_help_text(
            sizer, panel,
            "Placeholders: {title}, {author}, {fandom}. Extension is "
            "appended automatically based on the chosen format.",
        )

        sizer.AddSpacer(8)

        self.check_updates_ctrl = wx.CheckBox(
            panel, label="Check for &updates automatically on launch",
        )
        sizer.Add(self.check_updates_ctrl, 0, wx.ALL, 6)

        self.confirm_close_ctrl = wx.CheckBox(
            panel, label="&Warn before closing during an active download",
        )
        sizer.Add(self.confirm_close_ctrl, 0, wx.ALL, 6)

        panel.SetSizer(sizer)
        return panel

    def _build_downloads_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.format_ctrl = wx.Choice(panel, choices=_FORMAT_CHOICES)
        self.format_ctrl.SetName("Default format")
        sizer.Add(
            self._make_labeled_row(panel, "Default &format:", self.format_ctrl),
            0, wx.EXPAND | wx.ALL, 6,
        )

        sizer.AddSpacer(8)

        self.hr_stars_ctrl = wx.CheckBox(
            panel,
            label=(
                "Mark scene &breaks clearly by default "
                "(* * * in text, a silence pause in audiobooks)"
            ),
        )
        sizer.Add(self.hr_stars_ctrl, 0, wx.ALL, 6)

        self.strip_notes_ctrl = wx.CheckBox(
            panel, label="&Strip author's notes (A/N paragraphs) by default",
        )
        sizer.Add(self.strip_notes_ctrl, 0, wx.ALL, 6)

        self._add_help_text(
            sizer, panel,
            "These set the defaults that load on launch. You can still "
            "toggle them per-download on the main window.",
        )

        panel.SetSizer(sizer)
        return panel

    def _build_audiobook_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.speech_rate_ctrl = wx.SpinCtrl(
            panel, min=-50, max=100, initial=0, size=(90, -1),
        )
        self.speech_rate_ctrl.SetName("Default speech rate percent")
        sizer.Add(
            self._make_labeled_row(
                panel, "Default speech &rate (%):", self.speech_rate_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )
        self._add_help_text(
            sizer, panel,
            "Integer percent delta applied to every TTS call. "
            "-20 is 20% slower, +30 is 30% faster.",
        )

        sizer.AddSpacer(8)

        self._attribution_choices = list(_attribution_module.available())
        display_labels = [
            _attribution_module.BACKENDS[b]["display"]
            for b in self._attribution_choices
        ]
        self.attribution_ctrl = wx.Choice(panel, choices=display_labels)
        self.attribution_ctrl.SetName("Default attribution backend")
        sizer.Add(
            self._make_labeled_row(
                panel, "Default &attribution backend:", self.attribution_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )

        self.attribution_size_ctrl = wx.TextCtrl(panel)
        self.attribution_size_ctrl.SetName(
            "Default attribution model size (blank = default)"
        )
        sizer.Add(
            self._make_labeled_row(
                panel, "Default model &size (BookNLP only):",
                self.attribution_size_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )
        self._add_help_text(
            sizer, panel,
            "Leave blank to use the backend's default. BookNLP accepts "
            "'small' or 'big'; other backends ignore this field. "
            "When a backend isn't installed, audiobook renders fall "
            "back to the builtin attributor automatically.",
        )

        panel.SetSizer(sizer)
        return panel

    def _build_notifications_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._add_help_text(
            sizer, panel,
            "Credentials used by the watchlist to alert you about new "
            "chapters, new works from followed authors, and new "
            "matches for saved searches. Leave a channel blank to "
            "disable it.",
        )

        sizer.AddSpacer(6)
        sizer.Add(
            wx.StaticText(panel, label="Pushover"),
            0, wx.LEFT | wx.RIGHT, 6,
        )

        self.pushover_token_ctrl = wx.TextCtrl(panel)
        self.pushover_token_ctrl.SetName("Pushover API token")
        sizer.Add(
            self._make_labeled_row(
                panel, "API &token:", self.pushover_token_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )

        self.pushover_user_ctrl = wx.TextCtrl(panel)
        self.pushover_user_ctrl.SetName("Pushover user key")
        sizer.Add(
            self._make_labeled_row(
                panel, "User &key:", self.pushover_user_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )
        self._add_help_text(sizer, panel, _PUSHOVER_HELP)

        sizer.AddSpacer(8)
        sizer.Add(
            wx.StaticText(panel, label="Discord"),
            0, wx.LEFT | wx.RIGHT, 6,
        )
        self.discord_webhook_ctrl = wx.TextCtrl(panel)
        self.discord_webhook_ctrl.SetName("Discord webhook URL")
        sizer.Add(
            self._make_labeled_row(
                panel, "&Webhook URL:", self.discord_webhook_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )
        self._add_help_text(sizer, panel, _DISCORD_HELP)

        sizer.AddSpacer(8)
        sizer.Add(
            wx.StaticText(panel, label="Email"),
            0, wx.LEFT | wx.RIGHT, 6,
        )
        self.notify_email_ctrl = wx.TextCtrl(panel)
        self.notify_email_ctrl.SetName("Notification email address")
        sizer.Add(
            self._make_labeled_row(
                panel, "&Email address:", self.notify_email_ctrl,
            ),
            0, wx.EXPAND | wx.ALL, 6,
        )
        self._add_help_text(sizer, panel, _EMAIL_HELP)

        panel.SetSizer(sizer)
        return panel

    def _build_logging_tab(self):
        panel = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.log_level_ctrl = wx.Choice(panel, choices=_LOG_LEVELS)
        self.log_level_ctrl.SetName("Log level")
        sizer.Add(
            self._make_labeled_row(panel, "&Log level:", self.log_level_ctrl),
            0, wx.EXPAND | wx.ALL, 6,
        )

        self.log_to_file_ctrl = wx.CheckBox(
            panel, label="&Save log to file",
        )
        sizer.Add(self.log_to_file_ctrl, 0, wx.ALL, 6)
        self._add_help_text(
            sizer, panel,
            "Rotating file at <portable>/logs/ffn-dl.log (1 MB × 3 "
            "backups). Use 'Open log folder' from the View menu to "
            "reveal it.",
        )

        panel.SetSizer(sizer)
        return panel

    # ── Load / save ────────────────────────────────────────────

    def _load_values(self):
        """Populate every control from the current prefs snapshot."""
        # General
        self.output_dir_ctrl.SetValue(self.prefs.get(_p.KEY_OUTPUT_DIR) or "")
        self.name_template_ctrl.SetValue(
            self.prefs.get(_p.KEY_NAME_TEMPLATE) or ""
        )
        self.check_updates_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_CHECK_UPDATES)
        )
        self.confirm_close_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_CONFIRM_CANCEL_ON_CLOSE)
        )

        # Downloads
        fmt = (self.prefs.get(_p.KEY_FORMAT) or "epub").lower()
        if fmt in _FORMAT_CHOICES:
            self.format_ctrl.SetSelection(_FORMAT_CHOICES.index(fmt))
        else:
            self.format_ctrl.SetSelection(0)
        self.hr_stars_ctrl.SetValue(self.prefs.get_bool(_p.KEY_HR_AS_STARS))
        self.strip_notes_ctrl.SetValue(self.prefs.get_bool(_p.KEY_STRIP_NOTES))

        # Audiobook
        try:
            rate = int(self.prefs.get(_p.KEY_SPEECH_RATE) or "0")
        except (TypeError, ValueError):
            rate = 0
        self.speech_rate_ctrl.SetValue(max(-50, min(100, rate)))

        backend = (self.prefs.get(_p.KEY_ATTRIBUTION_BACKEND) or "builtin")
        if backend in self._attribution_choices:
            self.attribution_ctrl.SetSelection(
                self._attribution_choices.index(backend)
            )
        else:
            self.attribution_ctrl.SetSelection(0)
        self.attribution_size_ctrl.SetValue(
            self.prefs.get(_p.KEY_ATTRIBUTION_MODEL_SIZE) or ""
        )

        # Notifications
        self.pushover_token_ctrl.SetValue(
            self.prefs.get(_p.KEY_PUSHOVER_TOKEN) or ""
        )
        self.pushover_user_ctrl.SetValue(
            self.prefs.get(_p.KEY_PUSHOVER_USER) or ""
        )
        self.discord_webhook_ctrl.SetValue(
            self.prefs.get(_p.KEY_DISCORD_WEBHOOK) or ""
        )
        self.notify_email_ctrl.SetValue(
            self.prefs.get(_p.KEY_NOTIFY_EMAIL) or ""
        )

        # Logging
        level = (self.prefs.get(_p.KEY_LOG_LEVEL) or "INFO").upper()
        if level in _LOG_LEVELS:
            self.log_level_ctrl.SetSelection(_LOG_LEVELS.index(level))
        else:
            self.log_level_ctrl.SetSelection(_LOG_LEVELS.index("INFO"))
        self.log_to_file_ctrl.SetValue(
            self.prefs.get_bool(_p.KEY_LOG_TO_FILE)
        )

    def _on_browse_output(self, event):
        dlg = wx.DirDialog(
            self, "Choose default output folder",
            defaultPath=self.output_dir_ctrl.GetValue() or "",
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.output_dir_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _on_ok(self, event):
        self._save()
        event.Skip()

    def _save(self):
        """Write every control's value to prefs, then ask the owning
        frame to re-sync its live UI. A lot of these keys are mirrored
        on the main form; without the sync step, _save_prefs() on app
        close would overwrite the pref with the stale form value.
        """
        # General
        self.prefs.set(_p.KEY_OUTPUT_DIR, self.output_dir_ctrl.GetValue())
        self.prefs.set(_p.KEY_NAME_TEMPLATE, self.name_template_ctrl.GetValue())
        self.prefs.set_bool(
            _p.KEY_CHECK_UPDATES, self.check_updates_ctrl.GetValue(),
        )
        self.prefs.set_bool(
            _p.KEY_CONFIRM_CANCEL_ON_CLOSE, self.confirm_close_ctrl.GetValue(),
        )

        # Downloads
        fmt_idx = self.format_ctrl.GetSelection()
        if fmt_idx >= 0:
            self.prefs.set(_p.KEY_FORMAT, _FORMAT_CHOICES[fmt_idx])
        self.prefs.set_bool(_p.KEY_HR_AS_STARS, self.hr_stars_ctrl.GetValue())
        self.prefs.set_bool(
            _p.KEY_STRIP_NOTES, self.strip_notes_ctrl.GetValue(),
        )

        # Audiobook
        self.prefs.set(
            _p.KEY_SPEECH_RATE, str(self.speech_rate_ctrl.GetValue()),
        )
        b_idx = self.attribution_ctrl.GetSelection()
        if 0 <= b_idx < len(self._attribution_choices):
            self.prefs.set(
                _p.KEY_ATTRIBUTION_BACKEND, self._attribution_choices[b_idx],
            )
        self.prefs.set(
            _p.KEY_ATTRIBUTION_MODEL_SIZE,
            self.attribution_size_ctrl.GetValue().strip(),
        )

        # Notifications
        self.prefs.set(
            _p.KEY_PUSHOVER_TOKEN, self.pushover_token_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_PUSHOVER_USER, self.pushover_user_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_DISCORD_WEBHOOK,
            self.discord_webhook_ctrl.GetValue().strip(),
        )
        self.prefs.set(
            _p.KEY_NOTIFY_EMAIL, self.notify_email_ctrl.GetValue().strip(),
        )

        # Logging
        lvl_idx = self.log_level_ctrl.GetSelection()
        if lvl_idx >= 0:
            self.prefs.set(_p.KEY_LOG_LEVEL, _LOG_LEVELS[lvl_idx])
        self.prefs.set_bool(
            _p.KEY_LOG_TO_FILE, self.log_to_file_ctrl.GetValue(),
        )

        if self.main_frame is not None:
            try:
                self.main_frame.apply_preferences()
            except Exception:
                logger.exception("main_frame.apply_preferences() failed")
