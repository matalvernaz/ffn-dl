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
# Per-tab JSON blobs: {"query": "...", "filters": {key: value, ...}}
KEY_SEARCH_STATE_FFN = "search_state_ffn"
KEY_SEARCH_STATE_AO3 = "search_state_ao3"
KEY_SEARCH_STATE_ROYALROAD = "search_state_royalroad"
KEY_SEARCH_STATE_LITEROTICA = "search_state_literotica"

DEFAULTS = {
    KEY_NAME_TEMPLATE: "{title} - {author}",
    KEY_FORMAT: "epub",
    KEY_CHECK_UPDATES: True,
    KEY_HR_AS_STARS: False,
    KEY_STRIP_NOTES: False,
    KEY_SPEECH_RATE: "0",
    KEY_ATTRIBUTION_BACKEND: "builtin",
    KEY_ATTRIBUTION_MODEL_SIZE: "",
}


class Prefs:
    """Thin wrapper over wx.Config with string and bool accessors."""

    def __init__(self):
        import wx
        from . import portable

        # Portable frozen build: keep settings.ini next to the exe
        # (or in the writable-fallback dir). Pip-installed / dev mode
        # uses the platform default so users keep their existing prefs.
        if portable.is_frozen():
            self._cfg = wx.FileConfig(
                appName="ffn-dl",
                localFilename=str(portable.settings_file()),
                style=wx.CONFIG_USE_LOCAL_FILE,
            )
        else:
            self._cfg = wx.Config("ffn-dl")

    def get(self, key: str, default=None):
        val = self._cfg.Read(key, "")
        return val if val else (default if default is not None else DEFAULTS.get(key))

    def set(self, key: str, value) -> None:
        self._cfg.Write(key, "" if value is None else str(value))
        self._cfg.Flush()

    def get_bool(self, key: str, default: bool = None) -> bool:
        if default is None:
            default = DEFAULTS.get(key, False)
        return self._cfg.ReadBool(key, default)

    def set_bool(self, key: str, value: bool) -> None:
        self._cfg.WriteBool(key, bool(value))
        self._cfg.Flush()

    def flush(self) -> None:
        """Force any in-memory wx.Config buffer to disk/registry now.

        Every `set`/`set_bool` already flushes, but we call this
        explicitly before spawning a child process in the auto-update
        restart path so the child can't race ahead and read stale
        values that we just wrote.
        """
        try:
            self._cfg.Flush()
        except Exception:
            pass
