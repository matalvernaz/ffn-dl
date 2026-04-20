"""Persistent GUI preferences.

Frozen Windows builds store preferences as ``settings.ini`` next to
ffn-dl.exe (portable — no registry dependency, moves with the folder).
Non-frozen installs use ``wx.Config`` with its platform default
(dotfile on POSIX, registry on Windows) so pip-installed ffn-dl
behaves the same as it always has. Either way the accessor methods
below stay identical.
"""

KEY_NAME_TEMPLATE = "name_template"
KEY_FORMAT = "format"
KEY_OUTPUT_DIR = "output_dir"
KEY_CHECK_UPDATES = "check_updates"
KEY_SKIPPED_VERSION = "skipped_update_version"
KEY_HR_AS_STARS = "hr_as_stars"
KEY_STRIP_NOTES = "strip_notes"
KEY_SPEECH_RATE = "speech_rate"
KEY_ATTRIBUTION_BACKEND = "attribution_backend"
KEY_ATTRIBUTION_MODEL_SIZE = "attribution_model_size"
KEY_LOG_LEVEL = "log_level"
KEY_LOG_TO_FILE = "log_to_file"
# Prompt before closing the main window while a long-running job
# (download, audiobook build, search, etc.) is still active. The
# prompt's "Don't ask again" checkbox flips this pref off.
KEY_CONFIRM_CANCEL_ON_CLOSE = "confirm_cancel_on_close"
KEY_STORY_PICKER_SORT = "story_picker_sort"
# Library manager — auto-sort downloads into category subdirs and
# re-check existing files (including foreign ones from FanFicFare /
# FicHub) for updates.
KEY_LIBRARY_PATH = "library_path"
KEY_LIBRARY_PATH_TEMPLATE = "library_path_template"
KEY_LIBRARY_INDEX_PATH = "library_index_path"  # blank → program config dir
KEY_LIBRARY_MISC_FOLDER = "library_misc_folder"
KEY_LIBRARY_AMBIGUOUS_PROMPT = "library_ambiguous_prompt"
KEY_LIBRARY_REORGANIZE_CONFIRM_EACH = "library_reorganize_confirm_each"
# Per-tab JSON blobs: {"query": "...", "filters": {key: value, ...}}
KEY_SEARCH_STATE_FFN = "search_state_ffn"
KEY_SEARCH_STATE_AO3 = "search_state_ao3"
KEY_SEARCH_STATE_ROYALROAD = "search_state_royalroad"
KEY_SEARCH_STATE_LITEROTICA = "search_state_literotica"
KEY_SEARCH_STATE_WATTPAD = "search_state_wattpad"
# Watchlist notification channels — see ffn_dl.notifications for semantics.
# Pushover creds are a per-user + per-application pair; Discord is a single
# webhook URL; email uses the same SMTP config as --send-to-kindle and only
# needs the recipient address stored here.
KEY_PUSHOVER_TOKEN = "pushover_token"
KEY_PUSHOVER_USER = "pushover_user"
KEY_DISCORD_WEBHOOK = "discord_webhook"
KEY_NOTIFY_EMAIL = "notify_email"
# Watchlist background polling — GUI only; the CLI uses `--watch-run` on
# demand. `KEY_WATCH_POLL_INTERVAL_S` is clamped at load time to the
# floor defined in watchlist.MIN_POLL_INTERVAL_S so a corrupt config
# can't make the app hammer sites.
KEY_WATCH_AUTOPOLL = "watch_autopoll"
KEY_WATCH_POLL_INTERVAL_S = "watch_poll_interval_s"

# Default GUI polling interval for the watchlist background thread, in
# seconds. One hour balances freshness against site politeness — FFN's
# 6s/request floor means even a 50-watch list fits comfortably inside
# an hour, and every other supported site is faster.
DEFAULT_WATCH_POLL_INTERVAL_S = 60 * 60

DEFAULTS = {
    KEY_NAME_TEMPLATE: "{title} - {author}",
    KEY_FORMAT: "epub",
    KEY_CHECK_UPDATES: True,
    KEY_HR_AS_STARS: False,
    KEY_STRIP_NOTES: False,
    KEY_SPEECH_RATE: "0",
    KEY_ATTRIBUTION_BACKEND: "builtin",
    KEY_ATTRIBUTION_MODEL_SIZE: "",
    KEY_LOG_LEVEL: "INFO",
    KEY_LOG_TO_FILE: False,
    KEY_CONFIRM_CANCEL_ON_CLOSE: True,
    KEY_LIBRARY_PATH_TEMPLATE: "{fandom}/{title} - {author}.{ext}",
    KEY_LIBRARY_MISC_FOLDER: "Misc",
    KEY_LIBRARY_AMBIGUOUS_PROMPT: True,
    KEY_LIBRARY_REORGANIZE_CONFIRM_EACH: True,
    KEY_WATCH_AUTOPOLL: False,
    KEY_WATCH_POLL_INTERVAL_S: DEFAULT_WATCH_POLL_INTERVAL_S,
}


class Prefs:
    """Thin wrapper over wx.Config with string and bool accessors."""

    def __init__(self):
        from . import portable

        # Portable frozen build: keep settings.ini next to the exe
        # (or in the writable-fallback dir). Pip-installed / dev mode
        # uses the platform default so users keep their existing prefs.
        #
        # CLI-only installs may not have wxPython — the tool still works
        # with a read-only fallback that returns DEFAULTS and quietly
        # swallows set()/set_bool() calls. The GUI install path always
        # has wx, so users never hit this branch in practice.
        self._cfg = None
        try:
            import wx
        except ImportError:
            return

        if portable.is_frozen():
            self._cfg = wx.FileConfig(
                appName="ffn-dl",
                localFilename=str(portable.settings_file()),
                style=wx.CONFIG_USE_LOCAL_FILE,
            )
        else:
            self._cfg = wx.Config("ffn-dl")

    def get(self, key: str, default=None):
        if self._cfg is None:
            return default if default is not None else DEFAULTS.get(key)
        val = self._cfg.Read(key, "")
        return val if val else (default if default is not None else DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        if self._cfg is None:
            return
        self._cfg.Write(key, "" if value is None else str(value))
        self._cfg.Flush()

    def get_bool(self, key: str, default: bool = None) -> bool:
        if default is None:
            default = DEFAULTS.get(key, False)
        if self._cfg is None:
            return default
        return self._cfg.ReadBool(key, default)

    def set_bool(self, key: str, value: bool) -> None:
        if self._cfg is None:
            return
        self._cfg.WriteBool(key, bool(value))
        self._cfg.Flush()

    def flush(self) -> None:
        """Force any in-memory wx.Config buffer to disk/registry now.

        Every `set`/`set_bool` already flushes, but we call this
        explicitly before spawning a child process in the auto-update
        restart path so the child can't race ahead and read stale
        values that we just wrote.
        """
        if self._cfg is None:
            return
        try:
            self._cfg.Flush()
        except Exception:
            pass
