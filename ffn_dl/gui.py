"""Accessible tkinter GUI for ffn-dl."""

import threading
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ffn-dl - Fanfiction Downloader")
        self.geometry("600x500")
        self.minsize(500, 400)
        self._build_ui()
        self._downloading = False

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # ── URL ──────────────────────────────────────────────
        ttk.Label(main, text="Story URL or ID:").pack(anchor=tk.W, **pad)
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(main, textvariable=self.url_var, width=70)
        url_entry.pack(fill=tk.X, **pad)
        url_entry.focus_set()

        # ── Options row ──────────────────────────────────────
        opts = ttk.Frame(main)
        opts.pack(fill=tk.X, **pad)

        ttk.Label(opts, text="Format:").pack(side=tk.LEFT)
        self.format_var = tk.StringVar(value="epub")
        fmt = ttk.Combobox(
            opts,
            textvariable=self.format_var,
            values=["epub", "html", "txt", "audio"],
            state="readonly",
            width=8,
        )
        fmt.pack(side=tk.LEFT, padx=(4, 16))

        ttk.Label(opts, text="Filename:").pack(side=tk.LEFT)
        self.name_var = tk.StringVar(value="{title} - {author}")
        ttk.Entry(opts, textvariable=self.name_var, width=25).pack(
            side=tk.LEFT, padx=4
        )

        # ── Output folder ────────────────────────────────────
        out_frame = ttk.Frame(main)
        out_frame.pack(fill=tk.X, **pad)

        ttk.Label(out_frame, text="Save to:").pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        ttk.Entry(out_frame, textvariable=self.output_var, width=50).pack(
            side=tk.LEFT, padx=4, fill=tk.X, expand=True
        )
        ttk.Button(out_frame, text="Browse...", command=self._browse_output).pack(
            side=tk.LEFT, padx=4
        )

        # ── Buttons ──────────────────────────────────────────
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, **pad)

        self.dl_btn = ttk.Button(
            btn_frame, text="Download", command=self._on_download
        )
        self.dl_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.update_btn = ttk.Button(
            btn_frame, text="Update Existing File...", command=self._on_update
        )
        self.update_btn.pack(side=tk.LEFT)

        # ── Status log ───────────────────────────────────────
        ttk.Label(main, text="Status:").pack(anchor=tk.W, **pad)
        log_frame = ttk.Frame(main)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)

        self.log = tk.Text(log_frame, wrap=tk.WORD, height=12, state=tk.DISABLED)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log["yscrollcommand"] = scrollbar.set

        # Enter key triggers download
        self.bind("<Return>", lambda e: self._on_download())

    def _browse_output(self):
        path = filedialog.askdirectory(
            title="Choose output folder",
            initialdir=self.output_var.get(),
        )
        if path:
            self.output_var.set(path)

    def _log(self, msg):
        """Append a message to the status log (thread-safe)."""
        def _append():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)
        self.after(0, _append)

    def _set_busy(self, busy):
        def _update():
            state = "disabled" if busy else "normal"
            self.dl_btn.configure(state=state)
            self.update_btn.configure(state=state)
            self._downloading = busy
        self.after(0, _update)

    def _on_download(self):
        url = self.url_var.get().strip()
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

    def _on_update(self):
        if self._downloading:
            return
        path = filedialog.askopenfilename(
            title="Select file to update",
            filetypes=[
                ("All supported", "*.epub *.html *.txt"),
                ("EPUB", "*.epub"),
                ("HTML", "*.html"),
                ("Text", "*.txt"),
            ],
        )
        if not path:
            return

        from .updater import extract_source_url, count_chapters

        try:
            url = extract_source_url(path)
            existing = count_chapters(path)
        except (ValueError, FileNotFoundError) as e:
            self._log(f"Error: {e}")
            return

        # Infer format and output dir from the file
        suffix = Path(path).suffix.lower()
        fmt_map = {".epub": "epub", ".html": "html", ".txt": "txt"}
        self.format_var.set(fmt_map.get(suffix, "epub"))
        self.output_var.set(str(Path(path).parent))

        self._set_busy(True)
        self._log(f"Updating: {url} (existing file has {existing} chapters)")
        threading.Thread(
            target=self._run_download,
            args=(url,),
            kwargs={"skip_chapters": existing, "is_update": True},
            daemon=True,
        ).start()

    def _run_download(self, url, skip_chapters=0, is_update=False):
        """Run the download in a background thread."""
        try:
            from .ficwad import FicWadScraper
            from .scraper import FFNScraper

            # Pick scraper
            if "ficwad.com" in url.lower():
                scraper = FicWadScraper()
            else:
                scraper = FFNScraper()

            story_id = scraper.parse_story_id(url)

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
                self._log(
                    f"Found {len(story.chapters)} new chapters. Re-exporting..."
                )
                story = scraper.download(url, progress_callback=progress, skip_chapters=0)

            self._log(f"\n  Title:    {story.title}")
            self._log(f"  Author:   {story.author}")
            self._log(f"  Chapters: {len(story.chapters)}")

            fmt = self.format_var.get()
            output_dir = self.output_var.get()
            template = self.name_var.get()

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
    app = App()
    app.mainloop()
