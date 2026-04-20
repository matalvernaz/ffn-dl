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
        from .tts import FEMALE_VOICES, MALE_VOICES, NEUTRAL_VOICES

        if entry["gender"] == "female":
            candidates = FEMALE_VOICES
        elif entry["gender"] == "male":
            candidates = MALE_VOICES
        else:
            candidates = NEUTRAL_VOICES

        dlg = wx.SingleChoiceDialog(
            self,
            f"Pick a voice for {entry['name']}:",
            "Change voice",
            candidates,
        )
        try:
            current = candidates.index(entry["voice"])
            dlg.SetSelection(current)
        except ValueError:
            pass
        if dlg.ShowModal() == wx.ID_OK:
            new_voice = dlg.GetStringSelection()
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
        # wx.CheckListBox's MSAA check-state reporting is unreliable with
        # NVDA on Windows — prepend "[x] " / "[ ] " to every label so the
        # state is read out as part of the item text. EVT_CHECKLISTBOX
        # refreshes the prefix on toggle; EVT_LISTBOX updates the summary
        # pane as the user arrows through.
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

    @staticmethod
    def _as_int(value):
        if value is None:
            return 0
        s = str(value).replace(",", "").strip()
        m = re.match(r"\d+", s)
        return int(m.group(0)) if m else 0

    def _label(self, w, checked=False):
        # Leading "[x] " / "[ ] " so NVDA reads the state as part of the
        # item; the native MSAA state is unreliable in CheckListBox.
        prefix = "[x] " if checked else "[ ] "
        parts = [prefix, w.get("title", "") or "(untitled)"]
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

    Same NVDA trick as StoryPickerDialog — every row label is rewritten
    with a literal `[x] ` / `[ ] ` prefix on toggle so the check state
    is part of the readable item text. The dialog returns the ordered
    list of picked *labels* (not slugs); callers can resolve labels to
    whatever canonical form they store.
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

    def _label_text(self, idx, checked):
        prefix = "[x] " if checked else "[ ] "
        return prefix + self._labels[idx]

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
