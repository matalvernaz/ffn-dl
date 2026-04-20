"""Per-site search windows + their search specs.

Split out of ``gui.py`` to keep the search surface (the five site
specs and ``SearchFrame``) together and independent of the main
frame's download pipeline. ``SearchFrame`` calls back into
``MainFrame`` by attribute — the ``main_frame`` handle it's
constructed with — rather than importing the class, so this module
sits below ``gui.py`` in the dependency graph.

The ``_<site>_search_spec`` factories defer their ``.search``
imports to call time so opening the Search menu doesn't pay the
filter-constant import cost on launch for users who never search.
"""

import json
import threading

import wx

from .gui_dialogs import MultiPickerDialog, SeriesPartsDialog


_SEARCH_COLUMNS = [
    ("Title", 240),
    ("Site", 100),
    ("Author", 120),
    ("Fandom", 140),
    ("Words", 70),
    ("Ch", 40),
    ("Rating", 80),
    ("Status", 90),
]
# Column ordering: Site sits second so it's visible even in narrow
# windows and the reader can group results by archive at a glance.
# For per-site search frames (FFN, AO3, etc.) the Site cell stays
# blank since every row comes from the same archive.


def _ffn_search_spec():
    from .search import (
        FFN_CROSSOVER, FFN_GENRE, FFN_LANGUAGE, FFN_MATCH,
        FFN_RATING, FFN_SORT, FFN_STATUS, FFN_WORDS, search_ffn,
    )
    return {
        "label": "Search FFN",
        "search_fn": search_ffn,
        "filters": [
            ("&Rating:", "rating", list(FFN_RATING)),
            ("&Language:", "language", list(FFN_LANGUAGE)),
            ("S&tatus:", "status", list(FFN_STATUS)),
            ("&Genre:", "genre", list(FFN_GENRE)),
            ("Genre &2:", "genre2", list(FFN_GENRE)),
            ("&Words:", "min_words", list(FFN_WORDS)),
            ("&Crossover:", "crossover", list(FFN_CROSSOVER)),
            ("&Match in:", "match", list(FFN_MATCH)),
            ("Sor&t by:", "sort", list(FFN_SORT)),
        ],
    }


def _ao3_search_spec():
    from .search import (
        AO3_CATEGORY, AO3_COMPLETE, AO3_CROSSOVER, AO3_LANGUAGES,
        AO3_RATING, AO3_SORT, search_ao3,
    )
    return {
        "label": "Search AO3",
        "search_fn": search_ao3,
        "filters": [
            ("&Rating:", "rating", list(AO3_RATING)),
            ("Cate&gory:", "category", list(AO3_CATEGORY)),
            ("S&tatus:", "complete", list(AO3_COMPLETE)),
            ("&Crossover:", "crossover", list(AO3_CROSSOVER)),
            ("Lan&guage:", "language", list(AO3_LANGUAGES)),
            ("Sor&t by:", "sort", list(AO3_SORT)),
        ],
        "text_filters": [
            ("&Fandom:", "fandom"),
            ("&Character:", "character"),
            ("&Relationship:", "relationship"),
            ("Free&form tag:", "freeform"),
            ("&Word count:", "word_count"),
        ],
        "checkboxes": [
            ("&Single-chapter only", "single_chapter"),
        ],
    }


def _royalroad_search_spec():
    from .search import (
        RR_GENRES, RR_LISTS, RR_ORDER_BY, RR_STATUS, RR_TAGS, RR_TYPE,
        RR_WARNINGS, search_royalroad,
    )
    return {
        "label": "Search Royal Road",
        "search_fn": search_royalroad,
        "filters": [
            ("&Browse:", "list", list(RR_LISTS)),
            ("S&tatus:", "status", list(RR_STATUS)),
            ("&Type:", "type", list(RR_TYPE)),
            ("Sor&t by:", "order_by", list(RR_ORDER_BY)),
        ],
        "multi_pickers": [
            ("&Genres:", "genres", "Pick Royal Road genres", list(RR_GENRES)),
            ("Ta&gs:", "tags_picked", "Pick Royal Road tags", list(RR_TAGS)),
            (
                "War&nings:", "warnings",
                "Pick content warnings to require", list(RR_WARNINGS),
            ),
        ],
        "text_filters": [
            ("Min &words:", "min_words"),
            ("Ma&x words:", "max_words"),
            ("Min &pages:", "min_pages"),
            ("Min &rating:", "min_rating"),
        ],
    }


def _literotica_search_spec():
    from .search import LIT_CATEGORIES, search_literotica
    return {
        "label": "Search Literotica",
        "search_fn": search_literotica,
        "filters": [
            ("Categor&y:", "category", list(LIT_CATEGORIES)),
        ],
        "text_filters": [
            ("&Page:", "page"),
        ],
    }


def _wattpad_search_spec():
    from .search import WP_COMPLETED, WP_MATURE, search_wattpad
    return {
        "label": "Search Wattpad",
        "search_fn": search_wattpad,
        "filters": [
            ("&Mature:", "mature", list(WP_MATURE)),
            ("S&tatus:", "completed", list(WP_COMPLETED)),
        ],
    }


def _erotica_search_spec():
    """Unified "Erotic Story Search" — fans out across all 12
    erotica sites at once. Tag search is the primary input (multi-
    picker dialog) and sits immediately after the query box, per
    feedback that buried tag UX (as in the old Literotica-only
    search) makes this surface unusable.

    Tag options are annotated with their per-tag site-coverage count
    (e.g. "femdom [5 sites]") so users can tell well-covered kinks
    from niche ones before running a search that returns empty.
    """
    from .erotica.search import (
        EROTICA_SITE_SLUGS,
        EROTICA_TAG_VOCABULARY,
        search_erotica,
        tag_site_count,
    )

    min_words_choices = [
        "any", "1k+", "5k+", "10k+", "30k+", "50k+", "150k+",
    ]

    annotated_tags = [
        f"{tag} [{tag_site_count(tag)} sites]"
        for tag in EROTICA_TAG_VOCABULARY
    ]

    return {
        "label": "Erotic Story Search",
        "search_fn": search_erotica,
        "filters": [
            ("&Site:", "sites_choice", list(EROTICA_SITE_SLUGS)),
            ("Min &words:", "min_words", min_words_choices),
        ],
        # Tags are the primary input — first multi-picker so the
        # tab order lands users on tags directly after the query box.
        "multi_pickers": [
            (
                "Ta&gs:", "tags", "Pick erotica tags",
                annotated_tags,
            ),
        ],
        "text_filters": [
            ("&Category (Lush/Nifty):", "category"),
            ("&Fandom (AFF):", "fandom"),
        ],
    }


class SearchFrame(wx.Frame):
    """Non-modal per-site search window.

    Opened via the Search menu (Ctrl+1..5). Stays open alongside the
    main frame so the user can keep one window per site up at once and
    leave filter state in place while downloads run in the background.

    "Download Selected" / "Show Parts" push work back into the main
    frame's download pipeline, which owns the format, output folder,
    and audio settings.
    """

    _SITE_LABELS = {
        "ffn": "FFN",
        "ao3": "AO3",
        "royalroad": "Royal Road",
        "literotica": "Literotica",
        "wattpad": "Wattpad",
        "erotica": "Erotic Story Search",
    }

    _PREF_KEY_BY_SITE = {
        "ffn": "search_state_ffn",
        "ao3": "search_state_ao3",
        "royalroad": "search_state_royalroad",
        "literotica": "search_state_literotica",
        "wattpad": "search_state_wattpad",
        "erotica": "search_state_erotica",
    }

    def __init__(self, main_frame, site_key, spec):
        super().__init__(
            main_frame,
            title=spec["label"],
            size=(820, 640),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.main_frame = main_frame
        self.site_key = site_key
        self.spec = spec
        self.search_fn = spec["search_fn"]
        self.filter_ctrls = {}
        self.text_ctrls = {}
        self.checkbox_ctrls = {}
        self.results = []
        self._raw_results = []
        self.next_page = 1
        self.last_query = None
        # Erotica fan-out state: which sites have already yielded their
        # full tail so Load More skips them instead of polling for the
        # same rows over and over. Empty for every per-site frame.
        self._exhausted_sites: set = set()
        self.last_filters = {}

        self._build_ui()
        self._load_state()
        self.apply_busy(bool(self.main_frame._downloading))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()

    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad = 6

        # Query row
        q_row = wx.BoxSizer(wx.HORIZONTAL)
        q_row.Add(
            wx.StaticText(panel, label="&Query:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
        )
        self.query_ctrl = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.query_ctrl.SetName(f"{self.spec['label']} query")
        self.query_ctrl.Bind(wx.EVT_TEXT_ENTER, lambda e: self._on_search())
        q_row.Add(self.query_ctrl, 1, wx.RIGHT, 4)

        self.search_btn = wx.Button(panel, label="S&earch")
        self.search_btn.Bind(wx.EVT_BUTTON, lambda e: self._on_search())
        q_row.Add(self.search_btn, 0)
        sizer.Add(q_row, 0, wx.EXPAND | wx.ALL, pad)

        # Choice filters
        if self.spec.get("filters"):
            fgrid = wx.FlexGridSizer(rows=0, cols=8, hgap=4, vgap=4)
            for label, key, choices in self.spec["filters"]:
                fgrid.Add(
                    wx.StaticText(panel, label=label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.Choice(panel, choices=choices)
                ctrl.SetSelection(0)
                ctrl.SetName(label.replace("&", "").rstrip(":"))
                fgrid.Add(ctrl, 0, wx.RIGHT, 12)
                self.filter_ctrls[key] = ctrl
            sizer.Add(fgrid, 0, wx.EXPAND | wx.ALL, pad)

        # Free-text filters
        if self.spec.get("text_filters"):
            tgrid = wx.FlexGridSizer(rows=0, cols=4, hgap=4, vgap=4)
            for label, key in self.spec["text_filters"]:
                tgrid.Add(
                    wx.StaticText(panel, label=label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.TextCtrl(panel, size=(140, -1))
                ctrl.SetName(label.replace("&", "").rstrip(":"))
                tgrid.Add(ctrl, 0, wx.RIGHT, 12)
                self.text_ctrls[key] = ctrl
            sizer.Add(tgrid, 0, wx.EXPAND | wx.ALL, pad)

        # Multi-pickers (checkable-list dialogs for tags/genres/warnings)
        if self.spec.get("multi_pickers"):
            for mp_label, mp_key, mp_title, mp_options in self.spec["multi_pickers"]:
                row = wx.BoxSizer(wx.HORIZONTAL)
                row.Add(
                    wx.StaticText(panel, label=mp_label),
                    0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4,
                )
                ctrl = wx.TextCtrl(panel, size=(320, -1))
                ctrl.SetName(mp_label.replace("&", "").rstrip(":"))
                row.Add(ctrl, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
                btn = wx.Button(panel, label="Pic&k...")
                btn.Bind(
                    wx.EVT_BUTTON,
                    lambda evt, c=ctrl, t=mp_title, o=mp_options:
                        self._open_multi_picker(c, t, o),
                )
                row.Add(btn, 0)
                sizer.Add(row, 0, wx.EXPAND | wx.ALL, pad)
                self.text_ctrls[mp_key] = ctrl

        # Checkboxes
        if self.spec.get("checkboxes"):
            cb_row = wx.BoxSizer(wx.HORIZONTAL)
            for label, key in self.spec["checkboxes"]:
                ctrl = wx.CheckBox(panel, label=label)
                cb_row.Add(ctrl, 0, wx.RIGHT, 16)
                self.checkbox_ctrls[key] = ctrl
            sizer.Add(cb_row, 0, wx.EXPAND | wx.ALL, pad)

        # Results list
        sizer.Add(
            wx.StaticText(panel, label="&Results:"),
            0, wx.LEFT | wx.TOP, pad,
        )
        self.results_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN,
        )
        self.results_ctrl.SetName(f"{self.spec['label']} results")
        for i, (col_label, width) in enumerate(_SEARCH_COLUMNS):
            self.results_ctrl.InsertColumn(i, col_label, width=width)
        self.results_ctrl.Bind(
            wx.EVT_LIST_ITEM_SELECTED, self._on_result_select,
        )
        self.results_ctrl.Bind(
            wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_result_activated(),
        )
        sizer.Add(self.results_ctrl, 1, wx.EXPAND | wx.ALL, pad)

        # Summary
        sizer.Add(
            wx.StaticText(panel, label="S&ummary:"),
            0, wx.LEFT | wx.TOP, pad,
        )
        self.summary_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 70),
        )
        self.summary_ctrl.SetName(f"{self.spec['label']} summary")
        sizer.Add(self.summary_ctrl, 0, wx.EXPAND | wx.ALL, pad)

        dl_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_dl_btn = wx.Button(panel, label="Do&wnload Selected")
        self.search_dl_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_search_download(),
        )
        self.search_dl_btn.Disable()
        dl_row.Add(self.search_dl_btn, 0, wx.RIGHT, 8)

        self.show_parts_btn = wx.Button(panel, label="Show &Parts...")
        self.show_parts_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_show_parts(),
        )
        self.show_parts_btn.Disable()
        dl_row.Add(self.show_parts_btn, 0, wx.RIGHT, 8)

        self.load_more_btn = wx.Button(panel, label="Load &More")
        self.load_more_btn.Bind(
            wx.EVT_BUTTON, lambda e: self._on_load_more(),
        )
        self.load_more_btn.Disable()
        dl_row.Add(self.load_more_btn, 0)
        sizer.Add(dl_row, 0, wx.ALL, pad)

        panel.SetSizer(sizer)

    # ── Delegates ─────────────────────────────────────────────

    def _log(self, msg):
        self.main_frame._log(msg)

    # ── State persistence ─────────────────────────────────────

    def _load_state(self):
        raw = self.main_frame.prefs.get(self._PREF_KEY_BY_SITE[self.site_key])
        if not raw:
            return
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(state, dict):
            return
        # Ignore any legacy "query" a previous version wrote — query is
        # intentionally not persisted.
        for key, value in (state.get("filters") or {}).items():
            ctrl = self.filter_ctrls.get(key)
            if ctrl and isinstance(value, str) and value:
                ctrl.SetStringSelection(value)
        for key, value in (state.get("text") or {}).items():
            ctrl = self.text_ctrls.get(key)
            if ctrl and isinstance(value, str):
                ctrl.SetValue(value)
        for key, value in (state.get("checks") or {}).items():
            ctrl = self.checkbox_ctrls.get(key)
            if ctrl is not None:
                ctrl.SetValue(bool(value))

    def save_state(self):
        state = {
            "filters": {
                key: ctrl.GetStringSelection()
                for key, ctrl in self.filter_ctrls.items()
            },
            "text": {
                key: ctrl.GetValue()
                for key, ctrl in self.text_ctrls.items()
            },
            "checks": {
                key: bool(ctrl.GetValue())
                for key, ctrl in self.checkbox_ctrls.items()
            },
        }
        self.main_frame.prefs.set(
            self._PREF_KEY_BY_SITE[self.site_key], json.dumps(state),
        )

    # ── Busy state, driven from MainFrame._set_busy ──────────

    def apply_busy(self, busy):
        self.search_btn.Enable(not busy)
        has_selection = self.results_ctrl.GetFirstSelected() != -1
        selected_is_series = False
        if has_selection:
            idx = self.results_ctrl.GetFirstSelected()
            if 0 <= idx < len(self.results):
                selected_is_series = bool(
                    self.results[idx].get("is_series")
                )
        self.search_dl_btn.Enable(not busy and has_selection)
        self.show_parts_btn.Enable(
            not busy and has_selection and selected_is_series
        )
        self.load_more_btn.Enable(not busy and self.last_query is not None)

    # ── Multi-picker ──────────────────────────────────────────

    def _open_multi_picker(self, ctrl, title, options):
        current = [
            s.strip() for s in ctrl.GetValue().split(",") if s.strip()
        ]
        dlg = MultiPickerDialog(self, title, list(options), initial=current)
        try:
            if dlg.ShowModal() == wx.ID_OK:
                ctrl.SetValue(", ".join(dlg.picked_labels()))
        finally:
            dlg.Destroy()

    # ── Search ────────────────────────────────────────────────

    def _collect_filters(self):
        filters = {}
        for key, ctrl in self.filter_ctrls.items():
            idx = ctrl.GetSelection()
            if idx <= 0:
                # First entry is always "any"/"all"/"best match" — no filter
                continue
            filters[key] = ctrl.GetString(idx)
        for key, ctrl in self.text_ctrls.items():
            value = ctrl.GetValue().strip()
            if value:
                filters[key] = value
        for key, ctrl in self.checkbox_ctrls.items():
            if ctrl.GetValue():
                filters[key] = True
        return filters

    def _on_search(self):
        query = self.query_ctrl.GetValue().strip()
        if self.main_frame._downloading:
            return
        filters = self._collect_filters()
        # Most searches need a free-text query, but several site/filter
        # combinations are valid without one:
        #   • RR list browse (Rising Stars, Best Rated, …)
        #   • RR filter-only browse (tags, genres, warnings, numeric bounds)
        #   • Literotica category browse — the category slug IS the target.
        list_browse = (
            self.site_key == "royalroad"
            and filters.get("list")
            and filters["list"].strip().lower() != "search"
        )
        rr_filter_only = (
            self.site_key == "royalroad"
            and any(
                filters.get(k)
                for k in (
                    "tags", "tags_picked", "genres", "warnings",
                    "status", "type", "order_by",
                    "min_words", "max_words", "min_pages", "max_pages",
                    "min_rating",
                )
            )
        )
        lit_cat_browse = (
            self.site_key == "literotica" and filters.get("category")
        )
        if not query and not (list_browse or rr_filter_only or lit_cat_browse):
            self._log("Error: Please enter a search query.")
            return
        self.main_frame._set_busy(True, kind="search")
        self.results_ctrl.DeleteAllItems()
        self.summary_ctrl.SetValue("")
        self.results = []
        self._raw_results = []
        self.next_page = 1
        self._exhausted_sites = set()
        self.last_query = query
        self.last_filters = filters
        filter_str = (
            " [" + ", ".join(f"{k}={v}" for k, v in filters.items()) + "]"
            if filters else ""
        )
        site_label = self._SITE_LABELS.get(self.site_key, self.site_key)
        self._log(f"Searching {site_label} for: {query}{filter_str}")
        threading.Thread(
            target=self._run_search,
            args=(query, filters, 1, False),
            daemon=True,
        ).start()

    def _on_load_more(self):
        if self.main_frame._downloading or self.last_query is None:
            return
        self.main_frame._set_busy(True, kind="search")
        self._log(f"Loading page {self.next_page}...")
        threading.Thread(
            target=self._run_search,
            args=(self.last_query, self.last_filters, self.next_page, True),
            daemon=True,
        ).start()

    def _run_search(self, query, filters, page, append):
        from .search import fetch_until_limit
        try:
            # Erotica fan-out: bypass fetch_until_limit so the
            # ErotiCAResults object (with its ``site_stats`` attr)
            # survives instead of being flattened into a plain list.
            # One call per page maps naturally to how the fan-out
            # already pages per-site — we don't need fetch_until_limit's
            # cross-page accumulation behaviour.
            if self.site_key == "erotica":
                page_results = self.search_fn(
                    query,
                    page=page,
                    skip_sites=set(self._exhausted_sites),
                    **filters,
                )
                next_page = page + 1
            else:
                page_results, next_page = fetch_until_limit(
                    self.search_fn, query,
                    limit=25, start_page=page, **filters,
                )
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._log(f"Search error: {e}")
            self._log(tb.rstrip())
            wx.CallAfter(
                wx.MessageBox,
                f"Search failed:\n\n{e}",
                "Search Error",
                wx.OK | wx.ICON_ERROR, self,
            )
            self.main_frame._set_busy(False)
            return
        wx.CallAfter(self._populate_results, page_results, next_page, append)
        self.main_frame._set_busy(False)

    def _populate_results(self, new_results, next_page, append):
        from .search import collapse_ao3_series, collapse_literotica_series

        # Erotica fan-out ships a list subclass carrying per-site
        # stats + which archives are exhausted. Pull those off before
        # we flatten to a plain list for the rest of the pipeline.
        new_site_stats = getattr(new_results, "site_stats", None)
        new_exhausted = getattr(new_results, "exhausted_sites", None)
        if new_exhausted:
            self._exhausted_sites = set(self._exhausted_sites) | set(new_exhausted)

        # Keep the raw (uncollapsed) results across load-more so we can
        # re-run collapse on the full set — otherwise parts of the same
        # series that span page boundaries never find each other.
        if append:
            raw = list(self._raw_results or []) + list(new_results)
        else:
            raw = list(new_results)
        self._raw_results = raw

        if self.site_key == "ao3":
            processed = collapse_ao3_series(raw)
        elif self.site_key == "literotica":
            processed = collapse_literotica_series(raw)
        else:
            processed = list(raw)

        previous_count = len(self.results) if append else 0
        self.results = processed
        self.next_page = next_page

        ctrl = self.results_ctrl
        ctrl.Freeze()
        try:
            ctrl.DeleteAllItems()
            for r in self.results:
                row = ctrl.InsertItem(
                    ctrl.GetItemCount(), self._result_title(r),
                )
                # Column 1 = Site. For per-site frames (FFN, AO3, …)
                # the scraper populates ``site`` only on erotica
                # fan-out rows — elsewhere we fall back to the frame's
                # own site key so the column still tells the reader
                # which archive the row came from.
                site_cell = r.get("site") or self.site_key or ""
                ctrl.SetItem(row, 1, str(site_cell))
                ctrl.SetItem(row, 2, r.get("author", "") or "")
                ctrl.SetItem(row, 3, r.get("fandom", "") or "")
                ctrl.SetItem(row, 4, str(r.get("words", "")))
                ctrl.SetItem(row, 5, str(r.get("chapters", "")))
                ctrl.SetItem(row, 6, r.get("rating", "") or "")
                ctrl.SetItem(row, 7, r.get("status", "") or "")
        finally:
            ctrl.Thaw()

        # Load More disabled when:
        #   • no new rows came back (normal end-of-results for per-site
        #     frames), OR
        #   • every erotica site is exhausted (so the next fan-out
        #     would hit zero archives and return immediately).
        all_erotica_exhausted = (
            self.site_key == "erotica"
            and new_site_stats is not None
            and len(self._exhausted_sites) >= len(new_site_stats)
        )
        self.load_more_btn.Enable(
            bool(new_results)
            and not all_erotica_exhausted
            and not self.main_frame._downloading
        )
        if not self.results:
            self._log(
                "No results found." if not append else "No more results."
            )
            if new_site_stats:
                self._log_per_site_stats(new_site_stats)
            return

        if append:
            added = len(self.results) - previous_count
            focus_row = previous_count if added > 0 else 0
            self._log(
                f"Loaded more. Total {len(self.results)} rows "
                f"(+{max(added, 0)})."
                if added > 0 else "No more results."
            )
        else:
            focus_row = 0
            self._log(f"Found {len(self.results)} results.")

        if new_site_stats:
            self._log_per_site_stats(new_site_stats)

        ctrl.SetFocus()
        ctrl.Focus(focus_row)
        ctrl.Select(focus_row)

    def _log_per_site_stats(self, stats: dict) -> None:
        """Log a one-line summary of the fan-out: counts per archive
        and any failures. Keeps users informed that, e.g., Dark
        Wanderer is down today even though the rest of the results
        look fine."""
        ok_parts: list[str] = []
        failed: list[str] = []
        for site, info in sorted(stats.items()):
            if not info.get("ok", True):
                failed.append(
                    f"{site}: FAIL ({info.get('error') or 'error'})"
                )
            else:
                count = int(info.get("count", 0) or 0)
                marker = "·exhausted" if info.get("exhausted") else ""
                ok_parts.append(f"{site}: {count}{marker}")
        if ok_parts:
            self._log("  sites — " + ", ".join(ok_parts))
        if failed:
            self._log("  failures — " + "; ".join(failed))

    @staticmethod
    def _result_title(r):
        if r.get("is_series"):
            parts = len(r.get("series_parts") or [])
            return f"[Series · {parts} part(s)] {r['title']}"
        return r.get("title", "")

    def _on_result_select(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self.results):
            r = self.results[idx]
            summary = r.get("summary", "") or ""
            if r.get("is_series"):
                parts = r.get("series_parts") or []
                part_lines = "\n".join(
                    f"  - {p.get('title', '(untitled)')}" for p in parts
                )
                preview = (
                    f"[Series of {len(parts)} part(s) from search results]\n"
                    f"{summary}\n\n{part_lines}"
                    if part_lines else f"[Series]\n{summary}"
                )
                self.summary_ctrl.SetValue(preview.strip())
                self.show_parts_btn.Enable(bool(parts))
            else:
                self.summary_ctrl.SetValue(summary or "(no summary)")
                self.show_parts_btn.Disable()
            self.search_dl_btn.Enable(not self.main_frame._downloading)
        event.Skip()

    def _on_search_download(self):
        idx = self.results_ctrl.GetFirstSelected()
        if idx < 0 or idx >= len(self.results):
            return
        picked = self.results[idx]
        url = picked.get("url")
        if not url:
            self._log("Error: selected result has no URL.")
            return
        if self.main_frame._downloading:
            return
        self.main_frame._set_busy(True, kind="download")
        if picked.get("is_series"):
            self._log(f"Starting series download: {url}")
            if picked.get("parts_only"):
                part_urls = [
                    p.get("url")
                    for p in (picked.get("series_parts") or [])
                    if p.get("url")
                ]
                series_name = picked.get("title") or "Series"
                threading.Thread(
                    target=self.main_frame._run_series_merge_download,
                    args=(url,),
                    kwargs={
                        "series_name": series_name,
                        "part_urls": part_urls,
                    },
                    daemon=True,
                ).start()
            else:
                threading.Thread(
                    target=self.main_frame._run_series_merge_download,
                    args=(url,), daemon=True,
                ).start()
        else:
            self._log(f"Starting download: {url}")
            threading.Thread(
                target=self.main_frame._run_download,
                args=(url,), daemon=True,
            ).start()

    def _on_result_activated(self):
        # Enter/double-click: series rows open the parts dialog so
        # keyboard-only users can actually see what's inside the series
        # instead of blindly kicking off a multi-part merge.
        idx = self.results_ctrl.GetFirstSelected()
        if 0 <= idx < len(self.results):
            if self.results[idx].get("is_series"):
                self._on_show_parts()
                return
        self._on_search_download()

    def _on_show_parts(self):
        idx = self.results_ctrl.GetFirstSelected()
        if idx < 0 or idx >= len(self.results):
            return
        row = self.results[idx]
        if not row.get("is_series"):
            return
        parts = row.get("series_parts") or []
        if not parts:
            wx.MessageBox(
                "No parts have been loaded for this series yet.",
                "Series parts",
                wx.OK | wx.ICON_INFORMATION, self,
            )
            return
        dlg = SeriesPartsDialog(self, row["title"], parts)
        if dlg.ShowModal() == wx.ID_OK:
            picked = dlg.picked_url()
            if picked and not self.main_frame._downloading:
                self.main_frame._set_busy(True, kind="download")
                self._log(f"Starting part download: {picked}")
                threading.Thread(
                    target=self.main_frame._run_download,
                    args=(picked,), daemon=True,
                ).start()
        dlg.Destroy()

    # ── Close ─────────────────────────────────────────────────

    def _on_close(self, event):
        try:
            self.save_state()
        except Exception:
            pass
        try:
            self.main_frame._notify_search_frame_closed(self.site_key)
        except Exception:
            pass
        event.Skip()
