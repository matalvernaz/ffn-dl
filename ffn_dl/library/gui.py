"""wxPython dialogs for the library manager.

Imported lazily from the main GUI so the rest of ``ffn_dl.library``
stays wx-free for CLI use. Two dialogs:

* ``LibraryDialog`` — hub for library settings (path, template,
  misc folder) and the Scan / Reorganize entry points.
* ``ReorganizePreviewDialog`` — CheckListBox-based dry-run review,
  each row toggleable before applying.

NVDA state reporting on ``wx.CheckListBox`` is unreliable, so every
row gets a ``[x] `` / ``[ ] `` prefix — same pattern as the
StoryPickerDialog. Long operations (scan, apply) run on a worker
thread and report back through ``wx.CallAfter``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import wx

from .. import prefs as _prefs
from .gui_logic import format_move_label
from .index import LibraryIndex
from .refresh import build_refresh_queue, default_refresh_args
from .reorganizer import MoveOp, apply as apply_moves, plan
from .review import promote_untrackable, untrackable_for_root
from .scanner import scan
from .template import DEFAULT_MISC_FOLDER, DEFAULT_TEMPLATE


_TEMPLATE_HINT = (
    "Placeholders: {fandom} {title} {author} {ext} {rating} {status}. "
    "Forward slashes separate path components."
)


class LibraryDialog(wx.Dialog):
    """Hub for library settings + scan/reorganize actions."""

    def __init__(self, parent: wx.Window, prefs):
        super().__init__(
            parent,
            title="Library",
            size=(640, 440),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._prefs = prefs
        # _alive guards worker-thread callbacks — they fire through
        # wx.CallAfter and can land after the dialog is destroyed
        # (user closed it mid-scan). EVT_CLOSE flips the flag before
        # wx tears down the widgets.
        self._alive = True
        self.Bind(wx.EVT_CLOSE, self._on_close_event)
        self._build_ui()
        self._load_prefs()

    # ── UI construction ────────────────────────────────────────

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Scan a library of story files from any source and "
                    "keep it sorted by category."
                ),
            ),
            0, wx.ALL, 8,
        )

        # ── Library path ────────────────────────────────
        path_row = wx.BoxSizer(wx.HORIZONTAL)
        path_row.Add(
            wx.StaticText(panel, label="Library &folder:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.path_ctrl = wx.TextCtrl(panel)
        self.path_ctrl.SetName("Library folder")
        path_row.Add(self.path_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        browse_btn = wx.Button(panel, label="&Browse...")
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)
        path_row.Add(browse_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(path_row, 0, wx.EXPAND | wx.ALL, 8)

        # ── Template ────────────────────────────────────
        tmpl_row = wx.BoxSizer(wx.HORIZONTAL)
        tmpl_row.Add(
            wx.StaticText(panel, label="Path &template:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.template_ctrl = wx.TextCtrl(panel)
        self.template_ctrl.SetName("Path template")
        tmpl_row.Add(self.template_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        reset_btn = wx.Button(panel, label="&Reset")
        reset_btn.Bind(
            wx.EVT_BUTTON,
            lambda e: self.template_ctrl.SetValue(DEFAULT_TEMPLATE),
        )
        tmpl_row.Add(reset_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(tmpl_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(
            wx.StaticText(panel, label=_TEMPLATE_HINT),
            0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8,
        )

        # ── Misc folder ─────────────────────────────────
        misc_row = wx.BoxSizer(wx.HORIZONTAL)
        misc_row.Add(
            wx.StaticText(panel, label="&Miscellaneous folder name:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.misc_ctrl = wx.TextCtrl(panel)
        self.misc_ctrl.SetName("Miscellaneous folder name")
        misc_row.Add(self.misc_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(misc_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Status pane ─────────────────────────────────
        sizer.Add(
            wx.StaticText(panel, label="S&tatus:"),
            0, wx.LEFT | wx.RIGHT, 8,
        )
        self.status_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 120),
        )
        self.status_ctrl.SetName("Library status")
        sizer.Add(self.status_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # ── Action buttons ──────────────────────────────
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.scan_btn = wx.Button(panel, label="&Scan Library")
        self.scan_btn.Bind(wx.EVT_BUTTON, self._on_scan)
        btn_row.Add(self.scan_btn, 0, wx.RIGHT, 6)

        self.reorg_btn = wx.Button(panel, label="&Reorganize...")
        self.reorg_btn.Bind(wx.EVT_BUTTON, self._on_reorganize)
        btn_row.Add(self.reorg_btn, 0, wx.RIGHT, 6)

        self.update_btn = wx.Button(panel, label="Check for &Updates")
        self.update_btn.Bind(wx.EVT_BUTTON, self._on_check_updates)
        btn_row.Add(self.update_btn, 0, wx.RIGHT, 6)

        self.force_update_btn = wx.Button(
            panel, label="&Force Full Recheck",
        )
        self.force_update_btn.SetToolTip(
            "Ignore the recent-check TTL and probe every indexed story."
        )
        self.force_update_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_check_updates(e, force=True),
        )
        btn_row.Add(self.force_update_btn, 0, wx.RIGHT, 6)

        self.review_btn = wx.Button(panel, label="Review &Ambiguous...")
        self.review_btn.Bind(wx.EVT_BUTTON, self._on_review)
        btn_row.Add(self.review_btn, 0, wx.RIGHT, 6)

        btn_row.AddStretchSpacer(1)

        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)

    # ── Preference plumbing ────────────────────────────────────

    def _load_prefs(self) -> None:
        self.path_ctrl.SetValue(self._prefs.get(_prefs.KEY_LIBRARY_PATH, "") or "")
        self.template_ctrl.SetValue(
            self._prefs.get(_prefs.KEY_LIBRARY_PATH_TEMPLATE) or DEFAULT_TEMPLATE
        )
        self.misc_ctrl.SetValue(
            self._prefs.get(_prefs.KEY_LIBRARY_MISC_FOLDER) or DEFAULT_MISC_FOLDER
        )

    def _save_prefs(self) -> None:
        self._prefs.set(_prefs.KEY_LIBRARY_PATH, self.path_ctrl.GetValue())
        self._prefs.set(
            _prefs.KEY_LIBRARY_PATH_TEMPLATE, self.template_ctrl.GetValue()
        )
        self._prefs.set(
            _prefs.KEY_LIBRARY_MISC_FOLDER, self.misc_ctrl.GetValue()
        )

    def _current_path(self) -> Path | None:
        raw = (self.path_ctrl.GetValue() or "").strip()
        if not raw:
            wx.MessageBox(
                "Choose a library folder first.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            return None
        root = Path(raw).expanduser()
        if not root.is_dir():
            wx.MessageBox(
                f"{root} is not a directory.",
                "Library", wx.OK | wx.ICON_ERROR, self,
            )
            return None
        return root

    # ── Event handlers ─────────────────────────────────────────

    def _on_browse(self, event: wx.Event) -> None:
        current = self.path_ctrl.GetValue() or str(Path.home())
        dlg = wx.DirDialog(self, "Choose library folder", defaultPath=current)
        if dlg.ShowModal() == wx.ID_OK:
            self.path_ctrl.SetValue(dlg.GetPath())
        dlg.Destroy()

    def _append_status(self, line: str) -> None:
        self.status_ctrl.AppendText(line + "\n")

    def _set_busy(self, busy: bool) -> None:
        self.scan_btn.Enable(not busy)
        self.reorg_btn.Enable(not busy)
        self.update_btn.Enable(not busy)
        self.force_update_btn.Enable(not busy)
        self.review_btn.Enable(not busy)

    def _post_status(self, line: str) -> None:
        """Thread-safe status-pane append. Used as the progress callback
        for long-running worker-thread operations."""
        if not self._alive:
            return
        wx.CallAfter(self._append_status_if_alive, line)

    def _append_status_if_alive(self, line: str) -> None:
        if self._alive:
            self._append_status(line)

    def _on_scan(self, event: wx.Event) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        self._append_status(f"Scanning {root}...")
        self._set_busy(True)

        def worker():
            try:
                result = scan(root, recursive=True)
            except Exception as exc:
                wx.CallAfter(self._scan_failed, exc)
                return
            wx.CallAfter(self._scan_finished, result)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_finished(self, result) -> None:
        if not self._alive:
            return
        self._append_status(
            f"Scanned {result.total_files} file(s): "
            f"{result.identified_via_url} tracked by URL, "
            f"{result.ambiguous} indexed-only, "
            f"{result.errors} error(s)."
        )
        if result.error_files:
            for path, msg in result.error_files[:5]:
                self._append_status(f"  error: {path.name}: {msg}")
            if len(result.error_files) > 5:
                self._append_status(
                    f"  ... and {len(result.error_files) - 5} more"
                )
        self._set_busy(False)

    def _scan_failed(self, exc: Exception) -> None:
        if not self._alive:
            return
        self._append_status(f"Scan failed: {exc}")
        self._set_busy(False)

    def _on_check_updates(self, event: wx.Event, *, force: bool = False) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        if force:
            self._append_status(
                f"Forcing full recheck of {root} (ignoring recent-probe TTL)..."
            )
        else:
            self._append_status(f"Checking {root} for updates...")
        self._set_busy(True)

        # Lazy-import cli inside the worker so the module-load graph
        # stays library-independent (cli imports library, not the
        # other way around).
        def worker():
            try:
                from .. import cli
                from .index import LibraryIndex
                from .refresh import DEFAULT_GUI_RECHECK_INTERVAL_S
                from .scanner import scan as rescan

                recheck_interval = (
                    0 if force else DEFAULT_GUI_RECHECK_INTERVAL_S
                )
                args = default_refresh_args(
                    recheck_interval_s=recheck_interval,
                    force_recheck=force,
                )
                probe_queue, skipped = build_refresh_queue(
                    root,
                    skip_complete=False,
                    recheck_interval_s=recheck_interval,
                    progress=self._post_status,
                )
                if not probe_queue and not skipped:
                    self._post_status(
                        f"No indexed stories for {root}. Run Scan Library first."
                    )
                    wx.CallAfter(self._update_finished)
                    return

                cli._run_update_queue(
                    probe_queue, args, args.probe_workers,
                    skipped_count=len(skipped),
                    label="Library update",
                    progress=self._post_status,
                )

                if probe_queue:
                    try:
                        idx = LibraryIndex.load()
                        idx.mark_probed(
                            root, [item["url"] for item in probe_queue],
                        )
                    except Exception as exc:
                        self._post_status(
                            f"Warning: could not record probe timestamps: {exc}"
                        )

                try:
                    rescan(root)
                except Exception as exc:
                    self._post_status(
                        f"Warning: post-update index refresh failed: {exc}"
                    )
            except Exception as exc:
                self._post_status(f"Update failed: {exc}")
            finally:
                wx.CallAfter(self._update_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _update_finished(self) -> None:
        if not self._alive:
            return
        self._set_busy(False)

    def _on_review(self, event: wx.Event) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        idx = LibraryIndex.load()
        untrackable = untrackable_for_root(idx, root)
        if not untrackable:
            wx.MessageBox(
                (
                    "No untrackable files in this library. "
                    "Run Scan Library first, or everything is already identified."
                ),
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        dlg = ReviewDialog(self, idx=idx, root=root, untrackable=untrackable)
        try:
            dlg.ShowModal()
            promoted = dlg.promoted_count
        finally:
            dlg.Destroy()
        if promoted:
            self._append_status(f"Review: promoted {promoted} file(s).")

    def _on_reorganize(self, event: wx.Event) -> None:
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()

        template = self.template_ctrl.GetValue() or DEFAULT_TEMPLATE
        misc = self.misc_ctrl.GetValue() or DEFAULT_MISC_FOLDER

        try:
            moves = plan(root, template=template, misc_folder=misc)
        except Exception as exc:
            wx.MessageBox(
                f"Could not plan reorganize:\n\n{exc}",
                "Library", wx.OK | wx.ICON_ERROR, self,
            )
            return

        if not moves:
            self._append_status("Library is already organized — no moves needed.")
            wx.MessageBox(
                "This library is already organized — no moves needed.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )
            return

        preview = ReorganizePreviewDialog(self, root=root, moves=moves)
        try:
            if preview.ShowModal() == wx.ID_OK:
                selected = preview.selected_indices()
                self._run_apply(root, moves, selected)
        finally:
            preview.Destroy()

    def _run_apply(
        self,
        root: Path,
        moves: list[MoveOp],
        selected_indices: set[int],
    ) -> None:
        self._append_status(
            f"Applying {len(selected_indices)} of {len(moves)} move(s)..."
        )
        self._set_busy(True)

        def worker():
            try:
                result = apply_moves(
                    root, moves, selected_indices=selected_indices
                )
            except Exception as exc:
                wx.CallAfter(self._apply_failed, exc)
                return
            wx.CallAfter(self._apply_finished, result)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_finished(self, result) -> None:
        if not self._alive:
            return
        self._append_status(
            f"Applied {result.applied}, skipped {result.skipped}, "
            f"errors {result.errors}."
        )
        for msg in result.messages[:5]:
            self._append_status(f"  {msg}")
        if len(result.messages) > 5:
            self._append_status(f"  ... and {len(result.messages) - 5} more")
        self._set_busy(False)

    def _apply_failed(self, exc: Exception) -> None:
        if not self._alive:
            return
        self._append_status(f"Reorganize failed: {exc}")
        self._set_busy(False)

    def _on_close_event(self, event: wx.Event) -> None:
        # Flip the alive flag before wx starts tearing down widgets so
        # any worker callback queued through wx.CallAfter sees a dead
        # dialog and bails instead of touching destroyed controls.
        self._alive = False
        self._save_prefs()
        # The dialog is opened via ShowModal() (see gui.MainFrame's
        # library menu handler), so the Close button / window X /
        # Escape key all need to end the modal loop explicitly —
        # ``event.Skip()`` on its own lets wx destroy the window but
        # leaves the caller's ShowModal() blocked, which is what made
        # the Close button feel inert. EndModal is a no-op for
        # modeless callers, so this path stays safe either way.
        if self.IsModal():
            self.EndModal(wx.ID_CLOSE)
        else:
            event.Skip()


class ReorganizePreviewDialog(wx.Dialog):
    """Dry-run list of proposed moves with per-row checkboxes."""

    def __init__(self, parent: wx.Window, root: Path, moves: list[MoveOp]):
        super().__init__(
            parent,
            title="Reorganize Library — Preview",
            size=(820, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._root = Path(root).expanduser().resolve()
        self._moves = list(moves)
        self._build_ui()
        self._refresh_labels()
        self._set_all(True)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    f"{len(self._moves)} move(s) planned. "
                    "Tick the ones you want to apply, then press Apply "
                    "Selected. Use space to toggle the focused row."
                ),
            ),
            0, wx.ALL, 8,
        )

        top_row = wx.BoxSizer(wx.HORIZONTAL)
        select_all = wx.Button(panel, label="Select &All")
        select_all.Bind(wx.EVT_BUTTON, lambda e: self._set_all(True))
        top_row.Add(select_all, 0, wx.RIGHT, 6)
        select_none = wx.Button(panel, label="Select &None")
        select_none.Bind(wx.EVT_BUTTON, lambda e: self._set_all(False))
        top_row.Add(select_none, 0)
        sizer.Add(top_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.list_ctrl = wx.CheckListBox(panel, choices=[])
        self.list_ctrl.SetName("Planned moves")
        # Prefix pattern matches StoryPickerDialog for NVDA state reporting.
        self.list_ctrl.Bind(wx.EVT_CHECKLISTBOX, self._on_item_toggled)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        apply_btn = wx.Button(panel, id=wx.ID_OK, label="&Apply Selected")
        apply_btn.SetDefault()
        btn_row.Add(apply_btn, 0, wx.RIGHT, 6)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Cancel")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CANCEL)

    # ── Label formatting ──────────────────────────────────────

    def _refresh_labels(self) -> None:
        checks = [self.list_ctrl.IsChecked(i) for i in range(self.list_ctrl.GetCount())]
        self.list_ctrl.Clear()
        labels = [
            format_move_label(
                op, self._root,
                checked=(checks[i] if i < len(checks) else True),
            )
            for i, op in enumerate(self._moves)
        ]
        self.list_ctrl.SetItems(labels)
        for i, op in enumerate(self._moves):
            checked = checks[i] if i < len(checks) else True
            self.list_ctrl.Check(i, checked)

    def _on_item_toggled(self, event: wx.Event) -> None:
        idx = event.GetSelection()
        checked = self.list_ctrl.IsChecked(idx)
        self.list_ctrl.SetString(
            idx, format_move_label(self._moves[idx], self._root, checked),
        )

    def _set_all(self, checked: bool) -> None:
        for i in range(len(self._moves)):
            self.list_ctrl.Check(i, checked)
            self.list_ctrl.SetString(
                i, format_move_label(self._moves[i], self._root, checked),
            )

    # ── Public ────────────────────────────────────────────────

    def selected_indices(self) -> set[int]:
        return {
            i for i in range(self.list_ctrl.GetCount())
            if self.list_ctrl.IsChecked(i)
        }


class ReviewDialog(wx.Dialog):
    """Per-file URL-entry flow for untrackable library entries.

    One file in focus at a time. User pastes a source URL for the
    selected row and clicks Promote; that file moves into the library
    index's stories list with MEDIUM confidence. Index is saved on
    every successful promotion so a mid-review crash doesn't lose
    accepted entries.
    """

    def __init__(
        self,
        parent: wx.Window,
        *,
        idx: LibraryIndex,
        root: Path,
        untrackable: list[dict],
    ):
        super().__init__(
            parent,
            title="Review Ambiguous Files",
            size=(740, 540),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._idx = idx
        self._root = Path(root).expanduser().resolve()
        self._pending = list(untrackable)
        self.promoted_count = 0
        self._build_ui()
        self._refresh_list()
        self._select_first_pending()

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Pick a file, paste its source URL, and press Promote. "
                    "The file moves to the library index's tracked list so "
                    "Check for Updates can pick it up."
                ),
            ),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Untrackable files")
        self.list_ctrl.InsertColumn(0, "File", width=280)
        self.list_ctrl.InsertColumn(1, "Title", width=200)
        self.list_ctrl.InsertColumn(2, "Author", width=140)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_select)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        url_row = wx.BoxSizer(wx.HORIZONTAL)
        url_row.Add(
            wx.StaticText(panel, label="Source &URL:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.url_ctrl = wx.TextCtrl(
            panel, style=wx.TE_PROCESS_ENTER,
        )
        self.url_ctrl.SetName("Source URL")
        self.url_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_promote)
        url_row.Add(self.url_ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.promote_btn = wx.Button(panel, label="&Promote")
        self.promote_btn.Bind(wx.EVT_BUTTON, self._on_promote)
        url_row.Add(self.promote_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.skip_btn = wx.Button(panel, label="&Skip")
        self.skip_btn.Bind(wx.EVT_BUTTON, self._on_skip)
        url_row.Add(self.skip_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(url_row, 0, wx.EXPAND | wx.ALL, 8)

        self.status_ctrl = wx.StaticText(panel, label="")
        self.status_ctrl.SetName("Review status")
        sizer.Add(self.status_ctrl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)

    def _refresh_list(self) -> None:
        self.list_ctrl.DeleteAllItems()
        for i, entry in enumerate(self._pending):
            row = self.list_ctrl.InsertItem(
                i, entry.get("relpath") or "(unknown path)"
            )
            self.list_ctrl.SetItem(row, 1, entry.get("title") or "")
            self.list_ctrl.SetItem(row, 2, entry.get("author") or "")

    def _select_first_pending(self) -> None:
        if self._pending:
            self.list_ctrl.Select(0)
            self.list_ctrl.Focus(0)
            self.url_ctrl.SetFocus()

    def _selected_index(self) -> int:
        return self.list_ctrl.GetFirstSelected()

    def _on_select(self, event: wx.Event) -> None:
        # Clear the URL field when the selection changes so a user
        # doesn't accidentally promote file N with the URL they typed
        # for N-1.
        self.url_ctrl.SetValue("")
        self.status_ctrl.SetLabel("")

    def _on_promote(self, event: wx.Event) -> None:
        i = self._selected_index()
        if i < 0 or i >= len(self._pending):
            return
        url = self.url_ctrl.GetValue().strip()
        if not url:
            self.status_ctrl.SetLabel("Type a URL first, or press Skip.")
            return
        entry = self._pending[i]
        result = promote_untrackable(
            self._idx, self._root, entry.get("relpath") or "", url, save=True,
        )
        if not result.ok:
            self.status_ctrl.SetLabel(f"Not promoted: {result.message}")
            return
        self.promoted_count += 1
        del self._pending[i]
        self._refresh_list()
        if self._pending:
            new_i = min(i, len(self._pending) - 1)
            self.list_ctrl.Select(new_i)
            self.list_ctrl.Focus(new_i)
        self.url_ctrl.SetValue("")
        self.status_ctrl.SetLabel(
            f"Promoted to {result.adapter}. "
            f"{len(self._pending)} file(s) remaining."
        )
        if not self._pending:
            wx.MessageBox(
                "No more untrackable files.",
                "Library", wx.OK | wx.ICON_INFORMATION, self,
            )

    def _on_skip(self, event: wx.Event) -> None:
        i = self._selected_index()
        if i < 0 or i >= len(self._pending):
            return
        if i + 1 < len(self._pending):
            self.list_ctrl.Select(i + 1)
            self.list_ctrl.Focus(i + 1)
        self.url_ctrl.SetValue("")
        self.status_ctrl.SetLabel("")


