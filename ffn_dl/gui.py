"""Accessible wxPython GUI for ffn-dl.

Uses native Win32 controls via wxPython so NVDA, JAWS, and other
screen readers can read every widget natively.
"""

import re
import threading
import wx
from pathlib import Path


_FFN_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:fanfiction\.net/s/\d+|ficwad\.com/story/\d+)"
)


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="ffn-dl - Fanfiction Downloader",
            size=(620, 560),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self._downloading = False
        self._watching = False
        self._watch_seen = set()
        self._last_clip = ""
        self._build_ui()
        self.Centre()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # ── URL ──────────────────────────────────────────────
        sizer.Add(wx.StaticText(panel, label="Story &URL or ID:"), 0, wx.LEFT | wx.TOP, pad)
        self.url_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.url_ctrl.SetName("Story URL or ID")
        self.url_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_download)
        sizer.Add(self.url_ctrl, 0, wx.EXPAND | wx.ALL, pad)

        # ── Options row ──────────────────────────────────────
        opts = wx.BoxSizer(wx.HORIZONTAL)

        opts.Add(wx.StaticText(panel, label="&Format:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.format_ctrl = wx.Choice(
            panel, choices=["epub", "html", "txt", "audio"]
        )
        self.format_ctrl.SetSelection(0)
        self.format_ctrl.SetName("Format")
        opts.Add(self.format_ctrl, 0, wx.RIGHT, 16)

        opts.Add(wx.StaticText(panel, label="File&name template:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.name_ctrl = wx.TextCtrl(panel, value="{title} - {author}", size=(200, -1))
        self.name_ctrl.SetName("Filename template")
        opts.Add(self.name_ctrl, 1)

        sizer.Add(opts, 0, wx.EXPAND | wx.ALL, pad)

        # ── Output folder ────────────────────────────────────
        out_sizer = wx.BoxSizer(wx.HORIZONTAL)

        out_sizer.Add(wx.StaticText(panel, label="&Save to:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        default_dir = str(Path.home() / "Downloads")
        self.output_ctrl = wx.TextCtrl(panel, value=default_dir)
        self.output_ctrl.SetName("Save to folder")
        out_sizer.Add(self.output_ctrl, 1, wx.RIGHT, 4)

        browse_btn = wx.Button(panel, label="&Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        out_sizer.Add(browse_btn, 0)

        sizer.Add(out_sizer, 0, wx.EXPAND | wx.ALL, pad)

        # ── Buttons ──────────────────────────────────────────
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

        # ── Status log ───────────────────────────────────────
        sizer.Add(wx.StaticText(panel, label="S&tatus:"), 0, wx.LEFT | wx.TOP, pad)
        self.log_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.log_ctrl.SetName("Status log")
        sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        panel.SetSizer(sizer)

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

    # ── Helpers ───────────────────────────────────────────────

    def _log(self, msg):
        wx.CallAfter(self.log_ctrl.AppendText, msg + "\n")

    def _set_busy(self, busy):
        def _update():
            self._downloading = busy
            self.dl_btn.Enable(not busy)
            self.update_btn.Enable(not busy)
        wx.CallAfter(_update)

    # ── Browse ───────────────────────────────────────────────

    def _on_browse(self, event):
        dlg = wx.DirDialog(
            self, "Choose output folder",
            defaultPath=self.output_ctrl.GetValue(),
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.output_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

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

    # ── Update ───────────────────────────────────────────────

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

    # ── Clipboard watch ──────────────────────────────────────

    def _on_watch_toggle(self, event):
        if self.watch_btn.GetValue():
            self._watching = True
            self._watch_seen.clear()
            # Snapshot current clipboard so we don't immediately trigger
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

    def _run_download(self, url, skip_chapters=0, is_update=False):
        try:
            from .ficwad import FicWadScraper
            from .scraper import FFNScraper

            if "ficwad.com" in url.lower():
                scraper = FicWadScraper()
            else:
                scraper = FFNScraper()

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

            fmt = self.format_ctrl.GetString(self.format_ctrl.GetSelection())
            output_dir = self.output_ctrl.GetValue()
            template = self.name_ctrl.GetValue()

            if fmt == "audio":
                from .tts import generate_audiobook

                def audio_progress(current, total, title):
                    self._log(f"  Synthesizing [{current}/{total}] {title}")

                self._log("\nGenerating audiobook...")
                path = generate_audiobook(
                    story, output_dir, progress_callback=audio_progress
                )
            else:
                from .exporters import EXPORTERS
                exporter = EXPORTERS[fmt]
                path = exporter(story, output_dir, template=template)

            self._log(f"\nDone! Saved to: {path}")

        except Exception as e:
            self._log(f"\nError: {e}")
        finally:
            self._set_busy(False)


def main():
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
