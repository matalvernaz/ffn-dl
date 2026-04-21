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

import logging
import threading
from pathlib import Path

import wx

logger = logging.getLogger(__name__)

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


class LibraryFrame(wx.Frame):
    """Hub for library settings + scan/reorganize actions.

    Modeless so a scan or update run can be launched and the main
    window stays interactive — the user can start downloading a new
    story while the library check grinds on in the background.
    """

    def __init__(self, parent: wx.Window, prefs):
        super().__init__(
            parent,
            title="Library",
            size=(640, 460),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self._prefs = prefs
        # _alive guards worker-thread callbacks — they fire through
        # wx.CallAfter and can land after the window is destroyed
        # (user closed it mid-scan). EVT_CLOSE flips the flag before
        # wx tears down the widgets.
        self._alive = True
        # Cancel plumbing for the long Check-for-Updates run. The flag
        # stays None except while a worker is alive; the worker owns
        # the corresponding ``threading.Event`` and polls it between
        # probes and between downloads.
        self._update_cancel_event: threading.Event | None = None
        self._update_running = False
        self.Bind(wx.EVT_CLOSE, self._on_close_event)
        self._build_ui()
        self._load_prefs()
        # Escape closes the window (mirrors the old dialog affordance).
        self._install_escape_accel()

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

        # Update-mode modifiers live next to the button so keyboard
        # users can tab from Update → Force recheck → Fresh copies and
        # pick any combination (force + fresh, force alone, fresh alone,
        # or neither). Resets on dialog close — "fresh copies" is a
        # slow operation and a sticky toggle is a silent footgun.
        self.force_recheck_chk = wx.CheckBox(
            panel, label="&Force recheck (bypass TTL)",
        )
        self.force_recheck_chk.SetToolTip(
            "Ignore the recent-check TTL and probe every indexed story "
            "against upstream even if it was just checked."
        )
        btn_row.Add(
            self.force_recheck_chk, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )

        self.refetch_all_chk = wx.CheckBox(
            panel, label="Fres&h copies (re-download all chapters)",
        )
        self.refetch_all_chk.SetToolTip(
            "Re-download every chapter from upstream instead of merging "
            "new chapters with the ones already on disk. Slower, but "
            "catches silent author edits to old chapters."
        )
        btn_row.Add(
            self.refetch_all_chk, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )

        self.review_btn = wx.Button(panel, label="Review &Ambiguous...")
        self.review_btn.Bind(wx.EVT_BUTTON, self._on_review)
        btn_row.Add(self.review_btn, 0, wx.RIGHT, 6)

        # Cancel button sits next to Check for Updates so the stop
        # action is adjacent to the start action. Disabled unless a
        # run is in flight.
        self.cancel_btn = wx.Button(panel, label="Ca&ncel Update")
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel_update)
        self.cancel_btn.Disable()
        btn_row.Add(self.cancel_btn, 0, wx.RIGHT, 6)

        btn_row.AddStretchSpacer(1)

        close_btn = wx.Button(panel, id=wx.ID_CLOSE, label="&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btn_row.Add(close_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

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
        self.force_recheck_chk.Enable(not busy)
        self.refetch_all_chk.Enable(not busy)
        self.review_btn.Enable(not busy)
        # Cancel is only useful while an update run is live; scan and
        # reorganize are short-lived phase-2-only operations so they
        # don't wire into cancel_event at all.
        self.cancel_btn.Enable(busy and self._update_running)

    def _install_escape_accel(self) -> None:
        """Make Escape close the frame, same affordance the old dialog had."""
        close_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), id=int(close_id))
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_ESCAPE, int(close_id)),
        ]))

    def _on_cancel_update(self, event: wx.Event) -> None:
        if self._update_cancel_event is None or not self._update_running:
            return
        self._update_cancel_event.set()
        self._append_status("Cancel requested — finishing current step...")
        self.cancel_btn.Disable()

    def _post_status(self, line: str) -> None:
        """Thread-safe status-pane append. Used as the progress callback
        for long-running worker-thread operations.

        Lines emitted on a per-site download-queue worker pick up a
        ``[<site>] `` prefix so two sites running concurrently during
        a library update-all Phase 3 don't interleave into an
        unreadable mash.
        """
        if not self._alive:
            return
        from ..download_queue import site_from_thread_name
        site = site_from_thread_name(threading.current_thread().name)
        if site and line and line.strip():
            line = f"[{site}] {line}"
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

    def _on_check_updates(
        self,
        event: wx.Event,
        *,
        force: bool | None = None,
        refetch_all: bool | None = None,
    ) -> None:
        """Start a library-update run.

        ``force`` and ``refetch_all`` default to reading the adjacent
        checkboxes so users tick the combination they want before
        pressing Update. The explicit kwargs are still honoured so
        programmatic callers (tests, future toolbar entries) can
        override the checkbox state without flipping UI widgets.
        """
        root = self._current_path()
        if root is None:
            return
        self._save_prefs()
        if force is None:
            force = bool(self.force_recheck_chk.GetValue())
        if refetch_all is None:
            refetch_all = bool(self.refetch_all_chk.GetValue())

        # Describe exactly what combination is about to run so the
        # status log reflects the toggle state (easier to diagnose
        # "why is it slow" / "why did it re-probe").
        mode_bits: list[str] = []
        if force:
            mode_bits.append("ignoring recent-probe TTL")
        if refetch_all:
            mode_bits.append("re-downloading every chapter")
        if mode_bits:
            self._append_status(
                f"Updating {root} ({', '.join(mode_bits)})..."
            )
        else:
            self._append_status(f"Checking {root} for updates...")
        # Fresh cancel event per run — creating it up here so the
        # worker closure captures the exact instance the Cancel button
        # toggles.
        self._update_cancel_event = threading.Event()
        self._update_running = True
        self._set_busy(True)
        cancel_event = self._update_cancel_event

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
                    refetch_all=refetch_all,
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

                # Surface the TTL decision up front so users who click
                # Check for Updates twice in a row can see whether the
                # recent-probe skip is firing (N stories in TTL) or
                # whether they've hit a case where no entries have a
                # ``last_probed`` stamp yet (fresh index / pre-TTL
                # upgrade) and everything ends up queued.
                if recheck_interval > 0:
                    ttl_hours = recheck_interval / 3600
                    self._post_status(
                        f"TTL {ttl_hours:.1f}h: {len(skipped)} recently-"
                        f"probed story(ies) skipped, {len(probe_queue)} "
                        f"to probe. "
                        f"(Click Force Full Recheck to ignore the TTL.)"
                    )

                # Incremental ``last_probed`` stamping: the previous
                # "stamp everything at the end" pattern lost all work
                # whenever a user closed the app mid-probe — an 80-minute
                # FFN scan with 800+ stories is very easy to abandon
                # before completion, and every abandoned run made the
                # next one re-probe the entries we already checked.
                # Buffer stamps in memory and flush to disk every N
                # probes, plus once at the end. At worst we lose the
                # last-batch window on a crash.
                STAMP_FLUSH_EVERY = 25
                stamp_lock = threading.Lock()
                pending_stamps: dict[str, int | None] = {}

                def _flush_stamps_locked():
                    """Caller must hold ``stamp_lock``. Reloads the
                    on-disk index, stamps all pending URLs (with their
                    remote chapter counts so the next refresh can
                    resume interrupted pending downloads), saves, and
                    clears the buffer. Reloading per-flush makes the
                    stamp survive a concurrent rescan — we merge into
                    whatever the current disk state is rather than
                    overwrite with our stale in-memory copy."""
                    if not pending_stamps:
                        return
                    try:
                        idx = LibraryIndex.load()
                        idx.mark_probed(root, dict(pending_stamps))
                    except Exception as exc:
                        logger.exception(
                            "probe-stamp flush failed (pending=%d)",
                            len(pending_stamps),
                        )
                        self._post_status(
                            f"Warning: probe-stamp flush failed: {exc}"
                        )
                    pending_stamps.clear()

                def on_probe_complete(
                    url: str, remote_count: int | None = None,
                ) -> None:
                    """Called from a probe-worker thread once the
                    remote chapter count for ``url`` has been
                    retrieved. ``remote_count`` is the fresh upstream
                    count (or ``None`` if the probe answered with
                    "story gone"). Stored alongside ``last_probed``
                    so a later refresh after this process dies can
                    see remote > local and resume the download
                    without re-probing. Thread-safe via ``stamp_lock``.
                    """
                    with stamp_lock:
                        pending_stamps[url] = remote_count
                        if len(pending_stamps) >= STAMP_FLUSH_EVERY:
                            _flush_stamps_locked()

                cli._run_update_queue(
                    probe_queue, args, args.probe_workers,
                    skipped_count=len(skipped),
                    label="Library update",
                    progress=self._post_status,
                    on_probe_complete=on_probe_complete,
                    cancel_event=cancel_event,
                )

                # Final flush picks up the trailing <25 stamps that
                # never hit the batch threshold.
                with stamp_lock:
                    _flush_stamps_locked()

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
        self._update_running = False
        self._update_cancel_event = None
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
        # Mid-run close: confirm so the user isn't surprised when a
        # library update they kicked off evaporates. Veto the close if
        # they back out. If they say yes, flip the cancel flag so the
        # worker stops promptly; the close proceeds and the worker
        # threads finish out against a dead window (all their
        # callbacks are _alive-guarded).
        if self._update_running and self._update_cancel_event is not None:
            can_veto = event.CanVeto()
            choice = wx.MessageBox(
                "An update check is still running. Cancel it and close?",
                "Library — Update in progress",
                wx.YES_NO | wx.ICON_QUESTION, self,
            )
            if choice != wx.YES:
                if can_veto:
                    event.Veto()
                    return
            else:
                self._update_cancel_event.set()
        # Flip the alive flag before wx starts tearing down widgets so
        # any worker callback queued through wx.CallAfter sees a dead
        # window and bails instead of touching destroyed controls.
        self._alive = False
        self._save_prefs()
        # Let the main frame drop its reference so the menu item can
        # reopen cleanly next time.
        parent = self.GetParent()
        notify = getattr(parent, "_notify_library_frame_closed", None)
        if callable(notify):
            try:
                notify()
            except Exception:
                pass
        event.Skip()


# Backward-compat alias: older code (and tests) imported ``LibraryDialog``.
# The class is now a Frame but we keep the old name as a pointer so any
# lingering references keep resolving.
LibraryDialog = LibraryFrame


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


