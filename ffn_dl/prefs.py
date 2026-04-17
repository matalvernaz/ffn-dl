"""Persistent GUI preferences backed by wx.Config.

wx.Config writes to the registry on Windows and to a dotfile on Linux/Mac,
so preferences land in the right place per platform without extra work.
Keep keys and defaults here so both read and write sites stay in sync.
"""

KEY_NAME_TEMPLATE = "name_template"
KEY_FORMAT = "format"
KEY_OUTPUT_DIR = "output_dir"
KEY_CHECK_UPDATES = "check_updates"
KEY_SKIPPED_VERSION = "skipped_update_version"

DEFAULTS = {
    KEY_NAME_TEMPLATE: "{title} - {author}",
    KEY_FORMAT: "epub",
    KEY_CHECK_UPDATES: True,
}


class Prefs:
    """Thin wrapper over wx.Config with string and bool accessors."""

    def __init__(self):
        import wx
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
