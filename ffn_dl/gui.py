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

_SEARCH_COLUMNS = [
    ("Title", 260),
    ("Author", 120),
    ("Fandom", 160),
    ("Words", 70),
    ("Ch", 40),
    ("Rating", 60),
    ("Status", 90),
]


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="ffn-dl - Fanfiction Downloader",
            size=(760, 680),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self._downloading = False
        self._watching = False
        self._watch_seen = set()
        self._last_clip = ""
        self._search_results = []
        self._build_ui()
        self.Centre()

    def _build_ui(self):
        root = wx.Panel(self)
        root_sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # ── Notebook with Download / Search tabs ─────────────
        self.notebook = wx.Notebook(root)
        self.notebook.SetName("Mode tabs")

        self._build_download_tab(self.notebook)
        self._build_search_tab(self.notebook)

        root_sizer.Add(self.notebook, 0, wx.EXPAND | wx.ALL, pad)

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

    def _build_search_tab(self, notebook):
        panel = wx.Panel(notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # Query row
        q_row = wx.BoxSizer(wx.HORIZONTAL)
        q_row.Add(wx.StaticText(panel, label="&Query:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.search_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.search_ctrl.SetName("Search query")
        self.search_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        q_row.Add(self.search_ctrl, 1, wx.RIGHT, 4)

        self.search_btn = wx.Button(panel, label="S&earch")
        self.search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        q_row.Add(self.search_btn, 0)

        sizer.Add(q_row, 0, wx.EXPAND | wx.ALL, pad)

        # Results list
        sizer.Add(wx.StaticText(panel, label="&Results:"), 0, wx.LEFT | wx.TOP, pad)
        self.results_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.results_ctrl.SetName("Search results")
        for i, (label, width) in enumerate(_SEARCH_COLUMNS):
            self.results_ctrl.InsertColumn(i, label, width=width)
        self.results_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_result_select)
        self.results_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_result_activate)
        sizer.Add(self.results_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        # Summary
        sizer.Add(wx.StaticText(panel, label="S&ummary:"), 0, wx.LEFT | wx.TOP, pad)
        self.summary_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 80),
        )
        self.summary_ctrl.SetName("Story summary")
        sizer.Add(self.summary_ctrl, 0, wx.EXPAND | wx.ALL, pad)

        # Download selected
        self.search_dl_btn = wx.Button(panel, label="Do&wnload Selected")
        self.search_dl_btn.Bind(wx.EVT_BUTTON, self._on_search_download)
        self.search_dl_btn.Disable()
        sizer.Add(self.search_dl_btn, 0, wx.ALL, pad)

        panel.SetSizer(sizer)
        notebook.AddPage(panel, "Search")

    # ── Helpers ───────────────────────────────────────────────

    def _log(self, msg):
        wx.CallAfter(self.log_ctrl.AppendText, msg + "\n")

    def _set_busy(self, busy):
        def _update():
            self._downloading = busy
            self.dl_btn.Enable(not busy)
            self.update_btn.Enable(not busy)
            self.search_btn.Enable(not busy)
            # Search-download button only re-enables if something is selected
            has_selection = self.results_ctrl.GetFirstSelected() != -1
            self.search_dl_btn.Enable(not busy and has_selection)
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

    # ── Search ───────────────────────────────────────────────

    def _on_search(self, event):
        query = self.search_ctrl.GetValue().strip()
        if not query:
            self._log("Error: Please enter a search query.")
            return
        if self._downloading:
            return
        self._set_busy(True)
        self.results_ctrl.DeleteAllItems()
        self.summary_ctrl.SetValue("")
        self._search_results = []
        self._log(f"Searching fanfiction.net for: {query}")
        threading.Thread(
            target=self._run_search, args=(query,), daemon=True,
        ).start()

    def _run_search(self, query):
        try:
            from .search import search_ffn
            results = search_ffn(query)
        except Exception as e:
            self._log(f"Search error: {e}")
            self._set_busy(False)
            return

        wx.CallAfter(self._populate_results, results)
        self._set_busy(False)

    def _populate_results(self, results):
        self._search_results = results
        self.results_ctrl.DeleteAllItems()
        if not results:
            self._log("No results found.")
            return

        for r in results:
            row = self.results_ctrl.InsertItem(
                self.results_ctrl.GetItemCount(), r["title"]
            )
            self.results_ctrl.SetItem(row, 1, r["author"])
            self.results_ctrl.SetItem(row, 2, r["fandom"])
            self.results_ctrl.SetItem(row, 3, str(r["words"]))
            self.results_ctrl.SetItem(row, 4, str(r["chapters"]))
            self.results_ctrl.SetItem(row, 5, r["rating"])
            self.results_ctrl.SetItem(row, 6, r["status"])

        self._log(f"Found {len(results)} results.")
        # Move focus to the results list so keyboard users land on the list
        self.results_ctrl.SetFocus()
        self.results_ctrl.Focus(0)
        self.results_ctrl.Select(0)

    def _on_result_select(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self._search_results):
            r = self._search_results[idx]
            self.summary_ctrl.SetValue(r.get("summary", "") or "(no summary)")
            self.search_dl_btn.Enable(not self._downloading)
        event.Skip()

    def _on_result_activate(self, event):
        # Enter / double-click on a result → download it
        self._on_search_download(event)

    def _on_search_download(self, event):
        idx = self.results_ctrl.GetFirstSelected()
        if idx < 0 or idx >= len(self._search_results):
            return
        url = self._search_results[idx]["url"]
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

    def _scraper_for(self, url):
        from .ficwad import FicWadScraper
        from .scraper import FFNScraper

        if "ficwad.com" in url.lower():
            return FicWadScraper()
        return FFNScraper()

    def _export_story(self, story):
        fmt = self.format_ctrl.GetString(self.format_ctrl.GetSelection())
        output_dir = self.output_ctrl.GetValue()
        template = self.name_ctrl.GetValue()

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
        return exporter(story, output_dir, template=template)

    def _run_download(self, url, skip_chapters=0, is_update=False):
        try:
            scraper = self._scraper_for(url)

            if not is_update and scraper.is_author_url(url):
                self._run_author_download(url, scraper)
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

    def _run_author_download(self, url, scraper):
        self._log(f"Fetching author page: {url}")
        author_name, story_urls = scraper.scrape_author_stories(url)
        if not story_urls:
            self._log("No stories found on the author page.")
            return
        self._log(f"Author: {author_name}")
        self._log(f"Found {len(story_urls)} stories. Downloading all...")

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
            f"\nAuthor batch complete: {succeeded} succeeded, "
            f"{len(failed)} failed out of {len(story_urls)}."
        )
        for u in failed:
            self._log(f"  Failed: {u}")


def main():
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
