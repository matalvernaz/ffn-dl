"""Stand-alone wxPython dialogs used by the main GUI.

Split out of ``gui.py`` because the four dialogs here are leaf
widgets — they're opened by ``MainFrame`` and ``SearchFrame`` but
don't reach back into either — and bundling them with the rest of
the frame code was pushing ``gui.py`` past 3000 lines. Keeping them
in their own module makes the boundary obvious: no imports from
``gui.py`` into here, only the other direction.

All four dialogs follow the same NVDA-friendly pattern documented in
the project's accessibility notes: any state that MSAA reports
unreliably (CheckListBox check-state, in particular) is mirrored
into the visible label text as a ``[x] `` / ``[ ] `` prefix so
screen readers speak the state as part of the row.
"""

import re

import wx
from pathlib import Path


class VoicePreviewDialog(wx.Dialog):
    """Show detected characters, their assigned voices, and let users play
    a short sample or swap the voice before committing to an audiobook
    generation run. Changes are persisted to the same voice-map JSON the
    audiobook generator reads from, so saving and generating afterwards
    uses the edited mapping.
    """

    SAMPLE_TEXT = (
        "Hello. My name is {name}. I am a character in this story, "
        "and this is how I will sound in the audiobook."
    )

    def __init__(self, parent, voices, mapper, narrator_voice):
        super().__init__(
            parent, title="Preview character voices",
            size=(720, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._voices = voices  # list of {name, gender, voice, count}
        self._mapper = mapper
        self._narrator_voice = narrator_voice
        self._player = None
        self._tmp_dir = None
        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_ui(self):
        from . import tts
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Select a character and click Play Sample to hear "
                    "their assigned voice. Change Voice swaps to a "
                    "different option for that character."
                ),
            ),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Detected characters and their voices")
        for i, (label, width) in enumerate([
            ("Character", 180), ("Gender", 70), ("Lines", 60), ("Voice", 300),
        ]):
            self.list_ctrl.InsertColumn(i, label, width=width)
        self._refresh_rows()
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_play)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.play_btn = wx.Button(panel, label="&Play Sample")
        self.play_btn.Bind(wx.EVT_BUTTON, self._on_play)
        btn_row.Add(self.play_btn, 0, wx.RIGHT, 8)

        self.change_btn = wx.Button(panel, label="&Change Voice...")
        self.change_btn.Bind(wx.EVT_BUTTON, self._on_change_voice)
        btn_row.Add(self.change_btn, 0, wx.RIGHT, 8)

        self.narrator_btn = wx.Button(panel, label="Play &Narrator")
        self.narrator_btn.Bind(wx.EVT_BUTTON, self._on_play_narrator)
        btn_row.Add(self.narrator_btn, 0)

        btn_row.AddStretchSpacer(1)
        ok_btn = wx.Button(panel, id=wx.ID_OK, label="&OK")
        ok_btn.SetDefault()
        btn_row.Add(ok_btn, 0)

        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(sizer)

        # Pre-create a temp dir for sample files, reusing across plays
        import tempfile
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="ffn-preview-"))

    def _refresh_rows(self):
        self.list_ctrl.DeleteAllItems()
        for entry in self._voices:
            row = self.list_ctrl.GetItemCount()
            self.list_ctrl.InsertItem(row, entry["name"])
            self.list_ctrl.SetItem(row, 1, entry["gender"])
            self.list_ctrl.SetItem(row, 2, str(entry.get("count", "")))
            self.list_ctrl.SetItem(row, 3, entry["voice"])
        if self._voices:
            self.list_ctrl.Focus(0)
            self.list_ctrl.Select(0)

    def _selected_index(self):
        idx = self.list_ctrl.GetFirstSelected()
        return idx if 0 <= idx < len(self._voices) else -1

    def _stop_player(self):
        if self._player and self._player.poll() is None:
            try:
                self._player.terminate()
            except Exception:
                pass
        self._player = None

    def _play_voice(self, voice, name):
        from . import tts
        import threading
        self._stop_player()
        sample = self.SAMPLE_TEXT.format(name=name)
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] or "sample"
        out_path = self._tmp_dir / f"{safe_name}-{voice}.mp3"

        def worker():
            try:
                if not out_path.exists() or out_path.stat().st_size == 0:
                    tts.synthesize_sample(voice, sample, out_path)
                self._player = tts.play_audio_file(out_path)
            except Exception as exc:
                wx.CallAfter(
                    wx.MessageBox,
                    f"Could not play sample: {exc}",
                    "Preview error", wx.OK | wx.ICON_ERROR, self,
                )

        threading.Thread(target=worker, daemon=True).start()

    def _on_play(self, event):
        idx = self._selected_index()
        if idx < 0:
            return
        entry = self._voices[idx]
        self._play_voice(entry["voice"], entry["name"])

    def _on_play_narrator(self, event):
        self._play_voice(self._narrator_voice, "Narrator")

    def _on_change_voice(self, event):
        idx = self._selected_index()
        if idx < 0:
            return
        entry = self._voices[idx]
        from . import tts_providers

        target_gender = entry["gender"].lower()
        catalog = tts_providers.all_voices()
        candidates = [
            v for v in catalog
            if (target_gender in ("male", "female")
                and v.gender.lower() == target_gender)
            or target_gender not in ("male", "female")
        ]
        if not candidates:
            candidates = catalog
        # Display label (provider · locale · name) keeps the dialog
        # readable with both edge and piper voices side-by-side, while
        # the voice id we save is the namespaced form.
        labels = [
            f"{v.provider} · {v.locale} · {v.display}" for v in candidates
        ]
        ids = [v.id for v in candidates]

        dlg = wx.SingleChoiceDialog(
            self,
            f"Pick a voice for {entry['name']}:",
            "Change voice",
            labels,
        )
        try:
            current = ids.index(entry["voice"])
            dlg.SetSelection(current)
        except ValueError:
            pass
        if dlg.ShowModal() == wx.ID_OK:
            sel = dlg.GetSelection()
            if 0 <= sel < len(ids):
                new_voice = ids[sel]
                if new_voice and new_voice != entry["voice"]:
                    entry["voice"] = new_voice
                    self._mapper.mapping[entry["name"]] = new_voice
                    self._mapper.save()
                    self._refresh_rows()
                    self.list_ctrl.Focus(idx)
                    self.list_ctrl.Select(idx)
        dlg.Destroy()

    def _on_close(self, event):
        self._stop_player()
        import shutil as _shutil
        if self._tmp_dir and self._tmp_dir.exists():
            _shutil.rmtree(self._tmp_dir, ignore_errors=True)
        event.Skip()


class StoryPickerDialog(wx.Dialog):
    """Multi-select picker for an author's works or a bookmarks list.

    Uses a CheckListBox with per-item formatted labels — that gives NVDA
    a single readable string per row, plus native space-to-toggle.
    """

    _SORT_OPTIONS = [
        ("Default order", None),
        ("Title (A-Z)", "title_asc"),
        ("Title (Z-A)", "title_desc"),
        ("Word count (most first)", "words_desc"),
        ("Word count (least first)", "words_asc"),
        ("Chapter count (most first)", "chapters_desc"),
        ("Last updated (newest first)", "updated_desc"),
        ("Last updated (oldest first)", "updated_asc"),
        ("Section (own first)", "section"),
    ]

    def __init__(self, parent, title, works, prefs=None):
        super().__init__(
            parent, title=title,
            size=(720, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._works = list(works)
        self._order = list(range(len(self._works)))
        self._prefs = prefs
        self._sort_key = self._load_saved_sort_key()
        self._section_filter = "all"
        self._picked = []
        self._apply_sort()
        self._build_ui()

    def _load_saved_sort_key(self):
        if self._prefs is None:
            return None
        from .prefs import KEY_STORY_PICKER_SORT
        saved = self._prefs.get(KEY_STORY_PICKER_SORT, "")
        if not saved:
            return None
        valid_keys = {key for _, key in self._SORT_OPTIONS if key is not None}
        return saved if saved in valid_keys else None

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(
            wx.StaticText(panel, label="Sor&t by:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.sort_ctrl = wx.Choice(
            panel, choices=[label for label, _ in self._SORT_OPTIONS],
        )
        initial_idx = next(
            (i for i, (_, k) in enumerate(self._SORT_OPTIONS) if k == self._sort_key),
            0,
        )
        self.sort_ctrl.SetSelection(initial_idx)
        self.sort_ctrl.SetName("Sort order")
        self.sort_ctrl.Bind(wx.EVT_CHOICE, self._on_sort_change)
        controls.Add(self.sort_ctrl, 0, wx.RIGHT, 16)

        has_sections = any(
            w.get("section") in ("favorites", "bookmarks")
            for w in self._works
        )
        if has_sections:
            controls.Add(
                wx.StaticText(panel, label="Sho&w:"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
            )
            self.filter_ctrl = wx.Choice(
                panel, choices=["All", "Own only", "Favorites only"],
            )
            self.filter_ctrl.SetSelection(0)
            self.filter_ctrl.SetName("Section filter")
            self.filter_ctrl.Bind(wx.EVT_CHOICE, self._on_filter_change)
            controls.Add(self.filter_ctrl, 0, wx.RIGHT, 16)
        else:
            self.filter_ctrl = None

        select_all = wx.Button(panel, label="&Select All")
        select_all.Bind(wx.EVT_BUTTON, lambda e: self._set_all(True))
        controls.Add(select_all, 0, wx.RIGHT, 4)
        select_none = wx.Button(panel, label="Select &None")
        select_none.Bind(wx.EVT_BUTTON, lambda e: self._set_all(False))
        controls.Add(select_none, 0)

        sizer.Add(controls, 0, wx.EXPAND | wx.ALL, 8)

        self.list_ctrl = wx.CheckListBox(panel, choices=[])
        self.list_ctrl.SetName("Stories to download")
        # NVDA reads ``CheckListBox`` checkbox state natively on
        # current wxPython, so we no longer prepend a "[x] " / "[ ] "
        # text mirror — duplicating it made the screen reader say
        # "checked, x, Title" on every row. EVT_CHECKLISTBOX still
        # fires to update the summary pane; EVT_LISTBOX tracks
        # arrow-key navigation.
        self.list_ctrl.Bind(wx.EVT_CHECKLISTBOX, self._on_item_toggled)
        self.list_ctrl.Bind(wx.EVT_LISTBOX, self._on_item_focus_changed)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Summary pane: mirrors the selected row's summary so keyboard
        # users don't have to abandon the dialog to see what a story is.
        sizer.Add(
            wx.StaticText(panel, label="S&ummary:"),
            0, wx.LEFT | wx.RIGHT, 8,
        )
        self.summary_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 80),
        )
        self.summary_ctrl.SetName("Story summary")
        sizer.Add(self.summary_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        hint = wx.StaticText(
            panel,
            label=(
                "Use the arrow keys to move, space to tick or untick, "
                "and press Download to fetch every ticked story."
            ),
        )
        sizer.Add(hint, 0, wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        dl_btn = wx.Button(panel, id=wx.ID_OK, label="&Download Selected")
        dl_btn.SetDefault()
        dl_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_row.Add(dl_btn, 0, wx.RIGHT, 8)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Cancel")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)
        self._refresh()
        # Focus the list, not the Sort combo. The list IS the work
        # the user opened the dialog to do; tabbing into Sort is
        # always a detour. ``CallAfter`` defers the focus call past
        # any wx-internal "give the dialog its default focus" logic
        # that would otherwise run after our SetFocus.
        wx.CallAfter(self.list_ctrl.SetFocus)

    @staticmethod
    def _as_int(value):
        if value is None:
            return 0
        s = str(value).replace(",", "").strip()
        m = re.match(r"\d+", s)
        return int(m.group(0)) if m else 0

    def _label(self, w, checked=False):
        # No "[x] " / "[ ] " text prefix — NVDA reads the native
        # CheckListBox state on its own; doubling it here made the
        # screen reader announce "checked, x, Title" on every row.
        # ``checked`` is still accepted as a parameter so callers
        # don't need a refactor; we just don't render it.
        parts = [w.get("title", "") or "(untitled)"]
        meta = []
        if w.get("author"):
            meta.append(f"by {w['author']}")
        if w.get("words"):
            meta.append(f"{w['words']} words")
        if w.get("chapters"):
            meta.append(f"{w['chapters']} ch")
        if w.get("rating"):
            meta.append(f"Rated {w['rating']}")
        if w.get("status"):
            meta.append(w["status"])
        if w.get("updated"):
            meta.append(f"upd {w['updated']}")
        if w.get("section") == "favorites":
            meta.append("[Favorite]")
        elif w.get("section") == "bookmarks":
            meta.append("[Bookmark]")
        if meta:
            parts.append(" — " + " · ".join(meta))
        return "".join(parts)

    def _visible_indices(self):
        idxs = []
        for i in self._order:
            w = self._works[i]
            if self._section_filter == "own" and w.get("section") != "own":
                continue
            if self._section_filter == "favorites" and w.get("section") not in (
                "favorites", "bookmarks",
            ):
                continue
            idxs.append(i)
        return idxs

    def _refresh(self):
        idxs = self._visible_indices()
        # Preserve ticks across re-sort/filter by URL
        ticked_urls = {
            self._works[self._visible_map[j]]["url"]
            for j in self.list_ctrl.GetCheckedItems()
        } if getattr(self, "_visible_map", None) else set()
        labels = [
            self._label(
                self._works[i],
                checked=self._works[i].get("url") in ticked_urls,
            )
            for i in idxs
        ]
        self.list_ctrl.Set(labels)
        self._visible_map = idxs
        restored = [
            j for j, i in enumerate(idxs)
            if self._works[i].get("url") in ticked_urls
        ]
        if restored:
            self.list_ctrl.SetCheckedItems(restored)
        # Refresh the summary pane for whatever row is currently focused.
        self._update_summary()

    def _update_label_at(self, row):
        if not (0 <= row < len(self._visible_map)):
            return
        w = self._works[self._visible_map[row]]
        checked = self.list_ctrl.IsChecked(row)
        self.list_ctrl.SetString(row, self._label(w, checked=checked))

    def _update_summary(self):
        if not hasattr(self, "summary_ctrl"):
            return
        row = self.list_ctrl.GetSelection()
        if row == wx.NOT_FOUND or not (0 <= row < len(self._visible_map)):
            self.summary_ctrl.SetValue("")
            return
        w = self._works[self._visible_map[row]]
        summary = w.get("summary") or ""
        if not summary:
            summary = "(no summary)"
        self.summary_ctrl.SetValue(summary)

    def _on_item_toggled(self, event):
        self._update_label_at(event.GetSelection())
        event.Skip()

    def _on_item_focus_changed(self, event):
        self._update_summary()
        event.Skip()

    def _on_sort_change(self, event):
        idx = self.sort_ctrl.GetSelection()
        _, key = self._SORT_OPTIONS[idx] if 0 <= idx < len(self._SORT_OPTIONS) else (None, None)
        self._sort_key = key
        if self._prefs is not None:
            from .prefs import KEY_STORY_PICKER_SORT
            self._prefs.set(KEY_STORY_PICKER_SORT, key or "")
        self._apply_sort()
        self._refresh()

    def _on_filter_change(self, event):
        sel = self.filter_ctrl.GetSelection()
        self._section_filter = {0: "all", 1: "own", 2: "favorites"}.get(sel, "all")
        self._refresh()

    def _apply_sort(self):
        works = self._works
        default = list(range(len(works)))

        def words(i):
            return self._as_int(works[i].get("words"))

        def chapters(i):
            return self._as_int(works[i].get("chapters"))

        key = self._sort_key
        if key is None:
            self._order = default
        elif key == "title_asc":
            self._order = sorted(default, key=lambda i: (works[i].get("title") or "").lower())
        elif key == "title_desc":
            self._order = sorted(default, key=lambda i: (works[i].get("title") or "").lower(), reverse=True)
        elif key == "words_desc":
            self._order = sorted(default, key=words, reverse=True)
        elif key == "words_asc":
            self._order = sorted(default, key=words)
        elif key == "chapters_desc":
            self._order = sorted(default, key=chapters, reverse=True)
        elif key == "updated_desc":
            self._order = sorted(default, key=lambda i: works[i].get("updated") or "", reverse=True)
        elif key == "updated_asc":
            self._order = sorted(default, key=lambda i: works[i].get("updated") or "")
        elif key == "section":
            self._order = sorted(default, key=lambda i: (works[i].get("section") != "own", (works[i].get("title") or "").lower()))

    def _set_all(self, checked):
        indices = list(range(self.list_ctrl.GetCount()))
        if checked:
            self.list_ctrl.SetCheckedItems(indices)
        else:
            self.list_ctrl.SetCheckedItems([])
        # Rewrite every label so the "[x] / [ ]" prefix reflects the new state.
        for row in indices:
            self._update_label_at(row)

    def _on_ok(self, event):
        ticked = self.list_ctrl.GetCheckedItems()
        self._picked = [
            self._works[self._visible_map[j]]["url"] for j in ticked
        ]
        self.EndModal(wx.ID_OK)

    def picked_urls(self):
        return list(self._picked)


class MultiPickerDialog(wx.Dialog):
    """Tick-list picker for categorical filters (Royal Road genres, tags,
    content warnings, etc.).

    NVDA reads ``CheckListBox`` checkbox state natively on current
    wxPython, so the dialog uses plain labels — no "[x] " / "[ ] "
    text prefix. The dialog returns the ordered list of picked
    *labels* (not slugs); callers can resolve labels to whatever
    canonical form they store.
    """

    def __init__(self, parent, title, options, initial=()):
        super().__init__(
            parent, title=title,
            size=(420, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        # `options` is the ordered list of labels; `initial` the subset
        # that should start ticked. We compare case-insensitively so a
        # saved "litrpg" still ticks "LitRPG" on the next launch.
        self._labels = list(options)
        initial_lower = {str(x).strip().lower() for x in initial}
        self._initial_checks = [
            lbl.lower() in initial_lower for lbl in self._labels
        ]
        self._picked = []
        self._build_ui()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        controls.Add(
            wx.StaticText(panel, label="Fi&lter:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.filter_ctrl = wx.TextCtrl(panel)
        self.filter_ctrl.SetName("Filter options")
        self.filter_ctrl.Bind(wx.EVT_TEXT, self._on_filter_text)
        controls.Add(self.filter_ctrl, 1, wx.RIGHT, 8)
        select_all = wx.Button(panel, label="&Select All")
        select_all.Bind(wx.EVT_BUTTON, lambda e: self._set_visible_all(True))
        controls.Add(select_all, 0, wx.RIGHT, 4)
        select_none = wx.Button(panel, label="Select &None")
        select_none.Bind(wx.EVT_BUTTON, lambda e: self._set_visible_all(False))
        controls.Add(select_none, 0)
        sizer.Add(controls, 0, wx.EXPAND | wx.ALL, 8)

        self.list_ctrl = wx.CheckListBox(panel, choices=[])
        self.list_ctrl.SetName("Options")
        self.list_ctrl.Bind(wx.EVT_CHECKLISTBOX, self._on_item_toggled)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        hint = wx.StaticText(
            panel,
            label=(
                "Arrow keys to move, space to tick or untick. "
                "Type in the filter field to narrow the list."
            ),
        )
        sizer.Add(hint, 0, wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        ok_btn = wx.Button(panel, id=wx.ID_OK, label="&OK")
        ok_btn.SetDefault()
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_row.Add(ok_btn, 0, wx.RIGHT, 8)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Cancel")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

        # _checks tracks the authoritative checked state for every label
        # (index parallel to self._labels). _visible_map maps the list
        # control's visible rows → indices into self._labels.
        self._checks = list(self._initial_checks)
        self._visible_map = list(range(len(self._labels)))
        self._refresh()
        # Land on the list rather than the filter textbox. Same
        # reasoning as StoryPickerDialog: the list is the dialog's
        # primary surface, and the filter is a tool for narrowing
        # it — reachable by Tab when the user wants it.
        wx.CallAfter(self.list_ctrl.SetFocus)

    def _label_text(self, idx, checked):
        # Native CheckListBox state announcement is reliable now;
        # see the StoryPickerDialog ``_label`` comment for context.
        # ``checked`` kept in the signature for symmetry — toggling
        # no longer rewrites the label text, only the native state.
        return self._labels[idx]

    def _refresh(self):
        self.list_ctrl.Set([
            self._label_text(i, self._checks[i])
            for i in self._visible_map
        ])
        self.list_ctrl.SetCheckedItems([
            row for row, i in enumerate(self._visible_map)
            if self._checks[i]
        ])

    def _on_filter_text(self, event):
        needle = self.filter_ctrl.GetValue().strip().lower()
        if not needle:
            self._visible_map = list(range(len(self._labels)))
        else:
            self._visible_map = [
                i for i, lbl in enumerate(self._labels)
                if needle in lbl.lower()
            ]
        self._refresh()
        event.Skip()

    def _on_item_toggled(self, event):
        row = event.GetSelection()
        if 0 <= row < len(self._visible_map):
            i = self._visible_map[row]
            self._checks[i] = self.list_ctrl.IsChecked(row)
            self.list_ctrl.SetString(
                row, self._label_text(i, self._checks[i]),
            )
        event.Skip()

    def _set_visible_all(self, checked):
        for row, i in enumerate(self._visible_map):
            self._checks[i] = checked
        self._refresh()

    def _on_ok(self, event):
        self._picked = [
            self._labels[i] for i, ok in enumerate(self._checks) if ok
        ]
        self.EndModal(wx.ID_OK)

    def picked_labels(self):
        return list(self._picked)


class SeriesPartsDialog(wx.Dialog):
    """Show the parts of a series and let the user pick one to download on
    its own. Returns wx.ID_OK if a part was picked; retrieve it via
    `picked_url()`.
    """

    def __init__(self, parent, series_name, parts):
        super().__init__(
            parent, title=f"Parts of {series_name}",
            size=(560, 400),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._parts = parts
        self._picked = None

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    f"{len(parts)} part(s) of {series_name} loaded from "
                    "search. Pick one to download on its own, or close "
                    "this dialog and click Download Selected to merge the "
                    "full series into a single file."
                ),
            ),
            0, wx.ALL, 8,
        )

        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.list_ctrl.SetName("Series parts")
        for i, (label, width) in enumerate([
            ("Part", 260), ("Author", 140), ("Words", 80), ("Rating", 80),
        ]):
            self.list_ctrl.InsertColumn(i, label, width=width)
        for p in parts:
            row = self.list_ctrl.InsertItem(
                self.list_ctrl.GetItemCount(), p.get("title", "") or "",
            )
            self.list_ctrl.SetItem(row, 1, p.get("author", "") or "")
            self.list_ctrl.SetItem(row, 2, str(p.get("words", "") or ""))
            self.list_ctrl.SetItem(row, 3, p.get("rating", "") or "")
        if parts:
            self.list_ctrl.Focus(0)
            self.list_ctrl.Select(0)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer(1)
        dl_btn = wx.Button(panel, id=wx.ID_OK, label="&Download Part")
        dl_btn.SetDefault()
        dl_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_row.Add(dl_btn, 0, wx.RIGHT, 8)
        cancel_btn = wx.Button(panel, id=wx.ID_CANCEL, label="&Close")
        btn_row.Add(cancel_btn, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

    def _on_activate(self, event):
        self._on_ok(event)

    def _on_ok(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if 0 <= idx < len(self._parts):
            self._picked = self._parts[idx].get("url")
        self.EndModal(wx.ID_OK)

    def picked_url(self):
        return self._picked


class OptionalFeaturesDialog(wx.Dialog):
    """Install / reinstall the optional PyPI extras declared in
    ``pyproject.toml``.

    One row per feature with the current install status and an action
    button. The action button spawns the installer on a worker thread
    and streams pip output into the dialog's log pane so the user
    sees progress instead of a frozen UI. MSAA-reliable state:
    every row's status text is live (``wx.StaticText``) and gets
    updated in-place so NVDA re-announces it when it changes.
    """

    _INSTALLED_LABEL = "Installed"
    _MISSING_LABEL = "Not installed"

    def __init__(self, parent):
        super().__init__(
            parent, title="Optional features",
            size=(720, 520),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        # Imported lazily so headless test environments can import
        # the dialogs module without pulling in the registry.
        from . import optional_features as _feat
        self._feat = _feat
        self._buttons: dict[str, wx.Button] = {}
        self._status_labels: dict[str, wx.StaticText] = {}
        self._active_installs: set[str] = set()
        self._build_ui()

    def _build_ui(self):
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(
            panel,
            label=(
                "ffn-dl ships a minimal core install. Optional features "
                "live behind separate extras; install the ones you need "
                "below. Each install runs pip in the background — the "
                "log at the bottom streams its output."
            ),
        )
        header.Wrap(680)
        outer.Add(header, 0, wx.ALL, 8)

        grid = wx.FlexGridSizer(rows=0, cols=3, hgap=10, vgap=10)
        grid.AddGrowableCol(0)

        for feature in self._feat.available():
            info = self._feat.FEATURES[feature]
            title = wx.StaticText(
                panel,
                label=f"{info['display']} ({info['size_hint']})",
            )
            # Bold-ish via font weight so the dialog scan-reads with
            # NVDA's "skim" (Ctrl+Down) — the display string is the
            # first token SR picks up per row.
            font = title.GetFont()
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            title.SetFont(font)

            status = wx.StaticText(panel, label=self._status_text(feature))
            status.SetName(f"{info['display']} status")
            self._status_labels[feature] = status

            btn = wx.Button(panel, label=self._button_label(feature))
            btn.SetName(f"Install {info['display']}")
            btn.Bind(
                wx.EVT_BUTTON,
                lambda evt, f=feature: self._on_install(f),
            )
            self._buttons[feature] = btn

            grid.Add(title, 1, wx.EXPAND | wx.ALIGN_CENTER_VERTICAL)
            grid.Add(status, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(btn, 0, wx.ALIGN_CENTER_VERTICAL)

            # Full description spans all three columns as a second
            # row. Not using TextCtrl because we want the label to
            # participate in MSAA tree traversal cleanly.
            desc = wx.StaticText(panel, label=info["description"])
            desc.Wrap(640)
            grid.Add(desc, 1, wx.EXPAND | wx.LEFT | wx.BOTTOM, 2)
            grid.Add((0, 0))  # spacer
            grid.Add((0, 0))  # spacer

        outer.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        outer.Add(
            wx.StaticText(panel, label="&Installer log:"),
            0, wx.LEFT | wx.RIGHT | wx.TOP, 8,
        )
        self.log_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self.log_ctrl.SetName("Installer log")
        outer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        btn_row = wx.StdDialogButtonSizer()
        close_btn = wx.Button(panel, wx.ID_CLOSE, "&Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CLOSE))
        btn_row.AddButton(close_btn)
        btn_row.Realize()
        outer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 8)
        self.SetEscapeId(wx.ID_CLOSE)

        panel.SetSizer(outer)

    # ── Per-feature state helpers ───────────────────────────────

    def _status_text(self, feature: str) -> str:
        reason = self._feat.install_unsupported_reason(feature)
        if reason:
            # Surface the refusal inline so the user understands why
            # the button is disabled.
            return "(unsupported on this build)"
        if self._feat.is_installed(feature):
            return self._INSTALLED_LABEL
        return self._MISSING_LABEL

    def _button_label(self, feature: str) -> str:
        """Per-button label that includes the feature name.

        Four identical "Reinstall..." buttons render as four
        indistinguishable rows in a screen reader — users hear
        "Reinstall... button" four times and have to remember which
        row the focus is on. Baking the feature's display name into
        the label means every button self-describes: "Reinstall EPUB
        export", "Install cf-solve", etc. The accelerator ampersand
        stays on the action verb so Alt+I / Alt+R still work.
        """
        info = self._feat.FEATURES[feature]
        display = info.get("display") or feature
        if self._feat.install_unsupported_reason(feature):
            return f"Unsupported: {display}"
        if self._feat.is_installed(feature):
            return f"&Reinstall {display}..."
        return f"&Install {display}..."

    def _refresh_feature_row(self, feature: str) -> None:
        self._status_labels[feature].SetLabel(self._status_text(feature))
        btn = self._buttons[feature]
        btn.SetLabel(self._button_label(feature))
        btn.Enable(
            self._feat.install_unsupported_reason(feature) is None
            and feature not in self._active_installs
        )

    # ── Install flow ────────────────────────────────────────────

    def _on_install(self, feature: str) -> None:
        info = self._feat.FEATURES[feature]
        pip_hint = self._feat.pip_hint(feature)
        confirm = (
            f"Install '{info['display']}'?\n\n"
            f"{info['description']}\n\n"
            f"Size: {info['size_hint']}\n"
            f"Equivalent command-line: {pip_hint}"
        )
        if wx.MessageBox(
            confirm, "Confirm install", wx.YES_NO | wx.ICON_QUESTION,
        ) != wx.YES:
            return

        import threading

        self._active_installs.add(feature)
        self._status_labels[feature].SetLabel("(installing...)")
        self._buttons[feature].Enable(False)
        self._append_log(f"\nInstalling {info['display']}...")

        def run():
            ok = self._feat.install(feature, log_callback=self._log_from_thread)
            wx.CallAfter(self._after_install, feature, ok)

        threading.Thread(target=run, daemon=True).start()

    def _log_from_thread(self, line: str) -> None:
        wx.CallAfter(self._append_log, line)

    def _append_log(self, line: str) -> None:
        self.log_ctrl.AppendText(line.rstrip() + "\n")

    def _after_install(self, feature: str, ok: bool) -> None:
        self._active_installs.discard(feature)
        self._refresh_feature_row(feature)
        info = self._feat.FEATURES[feature]
        if ok:
            self._append_log(f"\nInstalled {info['display']} successfully.")
            # Frozen builds need a restart for .pth-style packages
            # (torch, playwright) to import cleanly on the running
            # interpreter.
            import sys as _sys
            if getattr(_sys, "frozen", False):
                wx.MessageBox(
                    f"{info['display']} was installed successfully.\n\n"
                    "Please restart ffn-dl so the new package is "
                    "available in the running app.",
                    "Restart required",
                    wx.OK | wx.ICON_INFORMATION,
                )
        else:
            self._append_log(
                f"\nInstall of {info['display']} failed — see log above."
            )


class TtsProvidersDialog(wx.Dialog):
    """Manage which TTS providers contribute voices to the audiobook
    generator's pool, and install / download Piper assets on demand.

    The dialog lists every registered provider with its install state
    and a toggle. Saving writes a comma-separated list of enabled
    provider names back to ``KEY_TTS_PROVIDERS`` (empty string == all
    installed providers, the implicit default). For Piper the dialog
    additionally exposes an Install Binary button (one-shot download
    of the upstream release) and a Download All Voices button (kicks
    off the lazy fetch for every voice in the manifest).
    """

    def __init__(self, parent, prefs, log_callback=None):
        super().__init__(
            parent, title="TTS providers", size=(640, 420),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._prefs = prefs
        self._log = log_callback or (lambda _msg: None)

        from . import prefs as _p
        self._p = _p
        from . import tts_providers
        self._tts_providers = tts_providers

        root = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 8

        sizer.Add(
            wx.StaticText(
                root,
                label=(
                    "Pick which TTS providers contribute voices to the "
                    "audiobook generator. The voice pool VoiceMapper picks "
                    "from is the union of every enabled provider, filtered "
                    "by each character's accent and gender."
                ),
            ),
            0, wx.ALL, pad,
        )

        # Per-provider rows. wx.CheckListBox is fine here — we don't
        # need the screen-reader [x]/[ ] prefix workaround because the
        # state never changes outside the dialog and we'll re-read on
        # save anyway.
        self._provider_names = self._tts_providers.all_provider_names()
        self.list_ctrl = wx.CheckListBox(
            root,
            choices=[self._row_label(n) for n in self._provider_names],
        )
        self.list_ctrl.SetName("Enabled TTS providers")
        self.list_ctrl.Bind(wx.EVT_LISTBOX, self._on_select)
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        self.detail = wx.StaticText(root, label="")
        self.detail.Wrap(580)
        sizer.Add(self.detail, 0, wx.ALL, pad)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.install_btn = wx.Button(root, label="&Install Piper binary")
        self.install_btn.Bind(wx.EVT_BUTTON, self._on_install_piper)
        btns.Add(self.install_btn, 0, wx.RIGHT, pad)

        self.download_btn = wx.Button(root, label="Download &all Piper voices")
        self.download_btn.Bind(wx.EVT_BUTTON, self._on_download_voices)
        btns.Add(self.download_btn, 0, wx.RIGHT, pad)
        btns.AddStretchSpacer(1)

        save = wx.Button(root, wx.ID_OK, "&Save")
        save.SetDefault()
        save.Bind(wx.EVT_BUTTON, self._on_save)
        cancel = wx.Button(root, wx.ID_CANCEL, "Cancel")
        btns.Add(save, 0, wx.RIGHT, 4)
        btns.Add(cancel, 0)
        sizer.Add(btns, 0, wx.EXPAND | wx.ALL, pad)

        root.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(root, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._load_state()

    def _row_label(self, name: str) -> str:
        provider = self._tts_providers.get_provider(name)
        if provider is None:
            return f"{name} (unavailable)"
        if provider.is_installed():
            return f"{name} (installed)"
        return f"{name} (not installed)"

    def _refresh_rows(self):
        for i, name in enumerate(self._provider_names):
            self.list_ctrl.SetString(i, self._row_label(name))

    def _load_state(self):
        raw = (self._prefs.get(self._p.KEY_TTS_PROVIDERS) or "").strip()
        enabled = (
            [n.strip().lower() for n in raw.split(",") if n.strip()]
            if raw else self._tts_providers.installed_provider_names()
        )
        for i, name in enumerate(self._provider_names):
            self.list_ctrl.Check(i, name in enabled)
        if self._provider_names:
            self.list_ctrl.SetSelection(0)
            self._refresh_detail(0)

    def _on_select(self, event):
        self._refresh_detail(self.list_ctrl.GetSelection())

    def _refresh_detail(self, idx):
        if idx < 0 or idx >= len(self._provider_names):
            self.detail.SetLabel("")
            return
        name = self._provider_names[idx]
        provider = self._tts_providers.get_provider(name)
        if provider is None:
            self.detail.SetLabel(f"{name}: provider failed to load.")
            return
        try:
            voices = provider.list_voices() if provider.is_installed() else []
        except Exception as exc:  # noqa: BLE001
            voices = []
            err = f" (catalog error: {exc})"
        else:
            err = ""
        if name == "edge":
            text = (
                f"Edge TTS — Microsoft Edge Neural Voices via edge-tts. "
                f"{len(voices)} voices available."
                + err
            )
        elif name == "piper":
            from .tts_providers import piper as _piper

            installed = _piper.piper_executable() is not None
            downloaded = sum(
                1 for v in voices
                if _piper.voice_is_downloaded(v.short_name)
            )
            text = (
                f"Piper TTS — local ONNX inference. "
                f"Binary: {'installed' if installed else 'not installed'}. "
                f"Catalog: {len(voices)} voices, {downloaded} downloaded."
                + err
            )
        else:
            text = f"{name}: {len(voices)} voices."
        self.detail.SetLabel(text)
        self.detail.Wrap(580)
        self.Layout()

    def _on_install_piper(self, event):
        from .tts_providers import piper as _piper

        self._log("TTS providers: installing Piper binary...")
        ok = _piper.install_piper_binary(log_callback=self._log)
        if ok:
            wx.MessageBox(
                "Piper binary installed.",
                "TTS providers", wx.OK | wx.ICON_INFORMATION, self,
            )
        else:
            wx.MessageBox(
                "Could not install Piper. See the main log for details "
                "(menu: View → Status log).",
                "TTS providers", wx.OK | wx.ICON_WARNING, self,
            )
        self._refresh_rows()
        self._refresh_detail(self.list_ctrl.GetSelection())

    def _on_download_voices(self, event):
        from .tts_providers import piper as _piper

        provider = self._tts_providers.get_provider("piper")
        if provider is None:
            return
        voices = [
            v for v in provider.list_voices()
            if not _piper.voice_is_downloaded(v.short_name)
        ]
        if not voices:
            wx.MessageBox(
                "Every Piper voice is already downloaded.",
                "TTS providers", wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        confirm = wx.MessageBox(
            f"Download {len(voices)} Piper voices "
            f"(roughly {len(voices) * 35} MB total)? They land under "
            "the portable folder's piper_models/ directory.",
            "Confirm download", wx.YES_NO | wx.ICON_QUESTION, self,
        )
        if confirm != wx.YES:
            return
        for v in voices:
            self._log(f"Piper: downloading {v.short_name}...")
            _piper.download_voice(v.short_name, log_callback=self._log)
        self._refresh_detail(self.list_ctrl.GetSelection())

    def _on_save(self, event):
        enabled = [
            name for i, name in enumerate(self._provider_names)
            if self.list_ctrl.IsChecked(i)
        ]
        # Empty selection collapses to "" so the audiobook code falls
        # back to "all installed providers" — never empty == "no TTS".
        installed = self._tts_providers.installed_provider_names()
        if set(enabled) == set(installed):
            value = ""
        else:
            value = ",".join(enabled)
        self._prefs.set(self._p.KEY_TTS_PROVIDERS, value)
        self.EndModal(wx.ID_OK)


class LlmSettingsDialog(wx.Dialog):
    """Edit the four LLM-attribution prefs (provider / model / API key
    / endpoint) and save them.

    Shown from the Audio toolbar when the LLM backend is selected. The
    fields are intentionally free-form so the user can pick any model
    their chosen provider serves — we don't try to keep a curated list,
    because new model names ship every couple of weeks.
    """

    _PROVIDER_KEYS = ["ollama", "openai", "anthropic", "openai-compatible"]
    _PROVIDER_LABELS = {
        "ollama": "Ollama (local, no API key)",
        "openai": "OpenAI (api.openai.com)",
        "anthropic": "Anthropic (api.anthropic.com)",
        "openai-compatible": "OpenAI-compatible (Groq, OpenRouter, vLLM, ...)",
    }
    _DEFAULT_ENDPOINTS = {
        "ollama": "http://localhost:11434",
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "openai-compatible": "",
    }

    # Curated starter list of models per provider. Picked for the
    # tasks ffn-dl uses an LLM for (author's-note classification +
    # audiobook speaker attribution): both want fast, instruction-
    # tuned models with reliable JSON output. Ollama list spans 2GB
    # to 9GB so the user can match their hardware. The dropdown is
    # editable, so this is just a starting suggestion — users can
    # always type a different name. The list is augmented at runtime
    # by whatever models the probe finds (already installed for
    # Ollama; available on the account for cloud providers).
    _CURATED_MODELS = {
        "ollama": [
            "llama3.1:8b",       # 4.7 GB — solid all-rounder, the default
            "llama3.2:3b",       # 2.0 GB — faster, smaller machines
            "qwen2.5:7b",        # 4.4 GB — strong JSON output
            "phi3.5:3.8b",       # 2.2 GB — fastest, good on CPU
            "mistral:7b",        # 4.1 GB — classic baseline
            "gemma2:9b",         # 5.4 GB — Google's tune
        ],
        "openai": [
            "gpt-4o-mini",       # cheap + fast, the default for ffn-dl tasks
            "gpt-4o",
            "gpt-4-turbo",
        ],
        "anthropic": [
            # Claude 4.x ID conventions are inconsistent: Opus 4.7 and
            # Sonnet 4.6 use unversioned IDs that resolve to the
            # current dated build, but Haiku 4.5's canonical ID still
            # carries the date suffix. Picking the wrong shape gets a
            # 404 from /v1/messages — use what the API actually
            # accepts for each tier.
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        ],
        "openai-compatible": [],  # too vendor-specific to curate
    }

    def __init__(self, parent, prefs):
        super().__init__(
            parent, title="LLM attribution settings", size=(620, 560),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._prefs = prefs
        self._busy = False
        # Worker threads that finish after the user closed the dialog
        # would call ``wx.CallAfter`` on destroyed widgets — guard
        # every callback with this flag so the post-close race is a
        # no-op instead of a crash.
        self._alive = True
        self.Bind(wx.EVT_CLOSE, self._on_close)

        from . import prefs as _p
        self._p = _p

        root = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 8

        intro = wx.StaticText(
            root,
            label=(
                "Send each chapter to a Large Language Model and ask it "
                "to label each line of dialogue. Pick Ollama for a local "
                "model (no API key) or one of the cloud providers (key "
                "required)."
            ),
        )
        intro.Wrap(580)
        sizer.Add(intro, 0, wx.ALL, pad)

        grid = wx.FlexGridSizer(rows=4, cols=2, hgap=8, vgap=6)
        grid.AddGrowableCol(1, 1)

        grid.Add(
            wx.StaticText(root, label="&Provider:"),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        labels = [self._PROVIDER_LABELS[k] for k in self._PROVIDER_KEYS]
        self.provider_ctrl = wx.Choice(root, choices=labels)
        self.provider_ctrl.SetName("Provider")
        self.provider_ctrl.Bind(wx.EVT_CHOICE, self._on_provider_change)
        grid.Add(self.provider_ctrl, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(root, label="&Model:"),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        # Editable combo: curated list per provider so users have a
        # sensible starting set without having to know any model
        # names off the top of their head, but still typeable so a
        # custom Ollama tag or a brand-new OpenAI release works
        # without an ffn-dl update. Probe results merge in as the
        # user clicks Test connection so the dropdown gradually
        # reflects what's actually available in their environment.
        self.model_ctrl = wx.ComboBox(root, style=wx.CB_DROPDOWN)
        self.model_ctrl.SetName("Model name")
        # Pull-model button enables/disables based on whether a name
        # is present, so refresh on every keystroke. EVT_TEXT fires
        # for both typing and ``SetValue``; EVT_COMBOBOX fires when
        # the user picks from the dropdown.
        self.model_ctrl.Bind(wx.EVT_TEXT, self._on_model_change)
        self.model_ctrl.Bind(wx.EVT_COMBOBOX, self._on_model_change)
        grid.Add(self.model_ctrl, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(root, label="&API key:"),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        self.api_key_ctrl = wx.TextCtrl(root, style=wx.TE_PASSWORD)
        self.api_key_ctrl.SetName("API key")
        grid.Add(self.api_key_ctrl, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(root, label="&Endpoint:"),
            0, wx.ALIGN_CENTER_VERTICAL,
        )
        self.endpoint_ctrl = wx.TextCtrl(root)
        self.endpoint_ctrl.SetName("Endpoint URL")
        self.endpoint_ctrl.SetHint("(blank = provider default)")
        grid.Add(self.endpoint_ctrl, 1, wx.EXPAND)

        sizer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad)

        self.hint = wx.StaticText(root, label="")
        self.hint.Wrap(580)
        sizer.Add(self.hint, 0, wx.ALL, pad)

        # Action row: test the configured endpoint, install Ollama
        # locally, or open the download page. The install / download
        # pair is gated on provider==ollama in :meth:`_refresh_actions`
        # so the buttons don't surface for users who picked a cloud
        # provider.
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.test_btn = wx.Button(root, label="&Test connection")
        self.test_btn.SetName("Test connection")
        self.test_btn.Bind(wx.EVT_BUTTON, self._on_test_connection)
        actions.Add(self.test_btn, 0, wx.RIGHT, pad)

        self.install_btn = wx.Button(root, label="&Install Ollama")
        self.install_btn.SetName("Install Ollama via winget")
        self.install_btn.Bind(wx.EVT_BUTTON, self._on_install_ollama)
        actions.Add(self.install_btn, 0, wx.RIGHT, pad)

        self.pull_btn = wx.Button(root, label="&Pull model")
        self.pull_btn.SetName("Pull the configured Ollama model")
        self.pull_btn.SetToolTip(
            "Download the model named in the Model field above into "
            "Ollama. Requires the daemon to be running — click Test "
            "connection first if you're not sure."
        )
        self.pull_btn.Bind(wx.EVT_BUTTON, self._on_pull_model)
        actions.Add(self.pull_btn, 0, wx.RIGHT, pad)

        self.download_btn = wx.Button(root, label="&Download Ollama…")
        self.download_btn.SetName("Open Ollama download page")
        self.download_btn.Bind(wx.EVT_BUTTON, self._on_download_ollama)
        actions.Add(self.download_btn, 0)

        sizer.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.TOP, pad)

        # Read-only multi-line log so screen readers can scrub through
        # the "test connection" result or the streaming winget output.
        # A wx.StaticText only announces on focus; a TextCtrl in
        # READONLY mode is reliably picked up by NVDA.
        log_label = wx.StaticText(root, label="Status:")
        sizer.Add(log_label, 0, wx.LEFT | wx.TOP, pad)
        self.status_ctrl = wx.TextCtrl(
            root,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
            size=(-1, 110),
        )
        self.status_ctrl.SetName("LLM action log")
        sizer.Add(self.status_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        btns = wx.StdDialogButtonSizer()
        save = wx.Button(root, wx.ID_OK, "&Save")
        save.SetDefault()
        cancel = wx.Button(root, wx.ID_CANCEL, "Cancel")
        btns.AddButton(save)
        btns.AddButton(cancel)
        btns.Realize()
        sizer.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, pad)

        save.Bind(wx.EVT_BUTTON, self._on_save)

        root.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(root, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._load_prefs()
        self._refresh_actions()

    def _load_prefs(self):
        provider = self._prefs.get(self._p.KEY_LLM_PROVIDER) or "ollama"
        try:
            idx = self._PROVIDER_KEYS.index(provider)
        except ValueError:
            idx = 0
        self.provider_ctrl.SetSelection(idx)
        saved_model = self._prefs.get(self._p.KEY_LLM_MODEL) or ""
        self._populate_model_choices(provider, current=saved_model)
        self.api_key_ctrl.SetValue(self._prefs.get(self._p.KEY_LLM_API_KEY) or "")
        self.endpoint_ctrl.SetValue(self._prefs.get(self._p.KEY_LLM_ENDPOINT) or "")
        self._refresh_hint()

    def _populate_model_choices(
        self,
        provider: str,
        *,
        current: str | None = None,
        extra: list[str] | None = None,
    ) -> None:
        """Refresh the Model combo's dropdown for ``provider``.

        Delegates the merge/sort to
        :func:`attribution.compute_model_choices` so the data shaping
        is unit-testable without a running wx instance, then pushes
        the result into the wx widget."""
        from . import attribution

        if current is None:
            current = self.model_ctrl.GetValue().strip()

        choices = attribution.compute_model_choices(
            curated=self._CURATED_MODELS.get(provider, []),
            extra=extra or [],
            current=current,
        )
        self.model_ctrl.Set(choices)
        # ``Set`` clears the textfield as a side effect on some wx
        # ports, so explicitly restore what the user had typed.
        self.model_ctrl.SetValue(current)

    def _selected_provider(self):
        idx = self.provider_ctrl.GetSelection()
        if idx < 0 or idx >= len(self._PROVIDER_KEYS):
            return self._PROVIDER_KEYS[0]
        return self._PROVIDER_KEYS[idx]

    def _on_provider_change(self, event):
        # Repopulate the model dropdown for the new provider's
        # curated set. Cloud → Ollama or vice-versa swaps the entire
        # list of suggestions; we keep whatever the user already
        # typed so a "saved before, switching providers to compare"
        # workflow doesn't lose state.
        self._populate_model_choices(self._selected_provider())
        self._refresh_hint()
        self._refresh_actions()

    def _on_model_change(self, event):
        # The Pull button is gated on a non-empty model name; this
        # handler is what flips it as the user types.
        self._refresh_actions()
        event.Skip()

    def _refresh_hint(self):
        provider = self._selected_provider()
        default_ep = self._DEFAULT_ENDPOINTS.get(provider, "")
        if provider == "ollama":
            text = (
                "Default endpoint: " + default_ep + " — leave Endpoint "
                "blank to use it. Pick any model already pulled into "
                "Ollama (e.g. 'llama3.1:8b', 'qwen2.5:14b'). API key is "
                "ignored."
            )
        elif provider == "openai":
            text = (
                "Default endpoint: " + default_ep + ". Set Model to a "
                "valid OpenAI model id (e.g. 'gpt-4o-mini'). API key "
                "is required."
            )
        elif provider == "anthropic":
            text = (
                "Default endpoint: " + default_ep + ". Set Model to a "
                "Claude model id (e.g. 'claude-haiku-4-5', "
                "'claude-sonnet-4-6'). API key is required."
            )
        else:
            text = (
                "OpenAI-compatible — point Endpoint at the provider's "
                "base URL (e.g. 'https://api.groq.com/openai/v1' or "
                "'https://openrouter.ai/api/v1'). Set Model to whatever "
                "the provider exposes. API key usually required."
            )
        self.hint.SetLabel(text)
        self.hint.Wrap(520)
        self.Layout()

    def _on_save(self, event):
        provider = self._selected_provider()
        model = self.model_ctrl.GetValue().strip()
        api_key = self.api_key_ctrl.GetValue().strip()
        endpoint = self.endpoint_ctrl.GetValue().strip()
        if not model:
            wx.MessageBox(
                "Please enter a model name before saving.",
                "Model required", wx.OK | wx.ICON_WARNING, self,
            )
            return
        if provider != "ollama" and not api_key:
            choice = wx.MessageBox(
                f"The {provider} provider needs an API key. Save without "
                "one anyway?",
                "API key missing",
                wx.YES_NO | wx.ICON_WARNING, self,
            )
            if choice != wx.YES:
                return
        self._prefs.set(self._p.KEY_LLM_PROVIDER, provider)
        self._prefs.set(self._p.KEY_LLM_MODEL, model)
        self._prefs.set(self._p.KEY_LLM_API_KEY, api_key)
        self._prefs.set(self._p.KEY_LLM_ENDPOINT, endpoint)
        self.EndModal(wx.ID_OK)

    # ── Actions: test / install / download ──────────────────────

    def _refresh_actions(self) -> None:
        """Show install/pull/download only for Ollama, and grey out
        the install button on platforms without winget so non-Windows
        users get a coherent UI instead of a broken click."""
        from . import ollama_install

        is_ollama = self._selected_provider() == "ollama"
        self.install_btn.Show(is_ollama)
        self.pull_btn.Show(is_ollama)
        self.download_btn.Show(is_ollama)

        if is_ollama:
            unavailable = ollama_install.winget_unavailable_reason()
            if unavailable:
                self.install_btn.Enable(False)
                self.install_btn.SetToolTip(unavailable)
            else:
                self.install_btn.Enable(not self._busy)
                self.install_btn.SetToolTip(
                    "Run `winget install Ollama.Ollama` to download and "
                    "install Ollama from Microsoft's package manager."
                )
            # Pull is disabled while busy and when the Model field is
            # empty — pulling "" would hand Ollama a 400.
            has_model = bool(self.model_ctrl.GetValue().strip())
            self.pull_btn.Enable(has_model and not self._busy)
        self.Layout()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.test_btn.Enable(not busy)
        self.download_btn.Enable(not busy)
        self.pull_btn.Enable(not busy)
        self._refresh_actions()

    def _append_status(self, line: str) -> None:
        """Append a line to the read-only status log. Always called
        on the GUI thread; worker threads marshal here via
        ``wx.CallAfter``."""
        if not self._alive:
            return
        self.status_ctrl.AppendText(line.rstrip() + "\n")

    def _on_close(self, event):
        """User closed the dialog — flip the alive flag so any worker
        callbacks that haven't fired yet skip their GUI work instead
        of touching destroyed widgets."""
        self._alive = False
        event.Skip()

    def _on_test_connection(self, event):
        from . import attribution

        provider = self._selected_provider()
        endpoint = self.endpoint_ctrl.GetValue().strip()
        api_key = self.api_key_ctrl.GetValue().strip()
        self._append_status(f"Testing {provider} endpoint...")
        self._set_busy(True)

        def worker():
            try:
                result = attribution.probe_llm_endpoint(
                    provider=provider,
                    endpoint=endpoint,
                    api_key=api_key,
                )
                wx.CallAfter(self._on_test_done, result)
            except Exception as exc:  # noqa: BLE001 — surface any bug
                wx.CallAfter(
                    self._on_test_done,
                    attribution.LLMProbeResult(
                        ok=False,
                        detail=f"Internal error during probe: {exc}",
                    ),
                )

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, result) -> None:
        if not self._alive:
            return
        prefix = "OK" if result.ok else "FAIL"
        self._append_status(f"  {prefix}: {result.detail}")
        # Successful probe → fold the discovered model names into the
        # Model dropdown so the user can pick from what's actually
        # installed/available without retyping. "Available" means:
        # already-pulled for Ollama, on-account for cloud providers.
        if result.ok and result.models:
            self._populate_model_choices(
                self._selected_provider(),
                extra=result.models,
            )
        self._set_busy(False)

    def _on_install_ollama(self, event):
        from . import ollama_install

        confirm = wx.MessageBox(
            "Run `winget install Ollama.Ollama`?\n\n"
            "Windows may show a User Account Control prompt to "
            "authorise the install. After it finishes, click "
            "'Test connection' to verify Ollama is running.",
            "Install Ollama",
            wx.YES_NO | wx.ICON_QUESTION, self,
        )
        if confirm != wx.YES:
            return

        self._append_status("Installing Ollama via winget...")
        self._set_busy(True)

        def worker():
            ok = ollama_install.install_ollama_via_winget(
                log_callback=lambda line: wx.CallAfter(
                    self._append_status, line,
                ),
            )
            wx.CallAfter(self._on_install_done, ok)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_install_done(self, ok: bool) -> None:
        if not self._alive:
            return
        if ok:
            self._append_status(
                "Install finished. Click 'Test connection' to confirm "
                "Ollama is running, then `ollama pull <model>` from a "
                "terminal to download a model."
            )
        else:
            self._append_status(
                "Install did not complete successfully. See the log "
                "above for details, or use Download Ollama to get the "
                "installer manually."
            )
        self._set_busy(False)

    def _on_download_ollama(self, event):
        from . import ollama_install

        wx.LaunchDefaultBrowser(ollama_install.OLLAMA_DOWNLOAD_URL)
        self._append_status(
            f"Opened {ollama_install.OLLAMA_DOWNLOAD_URL} in your browser."
        )

    def _on_pull_model(self, event):
        from . import ollama_install

        model = self.model_ctrl.GetValue().strip()
        if not model:
            return
        endpoint = self.endpoint_ctrl.GetValue().strip()

        self._append_status(f"Pull requested: {model}")
        self._set_busy(True)

        def worker():
            ok = ollama_install.pull_ollama_model(
                endpoint=endpoint,
                model=model,
                progress_callback=lambda line: wx.CallAfter(
                    self._append_status, line,
                ),
            )
            wx.CallAfter(self._on_pull_done, ok, model)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_pull_done(self, ok: bool, model: str) -> None:
        if not self._alive:
            return
        if ok:
            self._append_status(
                f"  '{model}' is now installed. Save the dialog to use "
                "it as your LLM model."
            )
            # Make sure the freshly-pulled model is in the dropdown
            # for the next time the user opens it (it would already be
            # there if they typed/picked it, but this also catches the
            # case of a pull invoked via some future automation path).
            self._populate_model_choices(
                self._selected_provider(), extra=[model],
            )
        else:
            self._append_status(
                f"  Pull of '{model}' did not complete. Check the model "
                "name (browse https://ollama.com/library) and that the "
                "Ollama daemon is running."
            )
        self._set_busy(False)
