# Changelog

## 1.7.0 — 2026-04-17

### Metadata

- **Word count in the header, everywhere**: RR, MediaMiner, and
  Literotica downloads used to skip the Words / Reading Time rows
  because none of those sites expose a total word count in their
  metadata. The exporter now falls back to counting the downloaded
  chapter text when no site-provided count is present, so every
  export has a Words line. When the site does expose a count (FFN,
  AO3, FicWad), it's still preferred because it includes anything
  the downloader doesn't fetch (omakes, appendices).
- **Royal Road: Published and Last Updated dates**: the RR scraper
  now lifts the first and last chapter's timestamps out of the
  chapters table and emits them as `date_published` / `date_updated`
  so the exporter renders `Published: YYYY-MM-DD` and
  `Updated: YYYY-MM-DD` in the header block. These were missing
  from RR downloads entirely.

## 1.6.4 — 2026-04-17

### Accessibility

- **Author / bookmark picker now announces checked state to NVDA**:
  `wx.CheckListBox`'s native MSAA check-state reporting was unreliable
  on Windows, so screen-reader users couldn't tell which stories they
  had ticked. Every row now carries a literal `[x] ` or `[ ] ` prefix
  that rewrites on toggle and on *Select All* / *Select None*.
- **Summary pane in the picker**: a read-only multi-line field below
  the list shows the currently focused story's summary and updates as
  you arrow through. Keyboard-only users no longer have to abandon
  the dialog to see what a story is about.
- **FFN author rows now carry a summary**: `scrape_author_works` used
  to return the title / meta / stats but drop the blurb. The summary
  was missing from every FFN author picker session until now.

## 1.6.3 — 2026-04-17

### Royal Road

- **STUB status is no longer misleading**: Royal Road's `STUB` label
  means the author trimmed chapters after publishing elsewhere — it's
  a state, not a size descriptor. The 1.6.0 display of "Stub" in the
  status column read like "this is a short piece" for fictions with
  hundreds of remaining chapters. STUB is now separated from the
  completion state: the status becomes `Stubbed` on its own, or
  combined as `Complete (Stubbed)` / `In-Progress (Stubbed)` / etc.
  when the card or fiction page exposes a completion label.
- **Enrichment fetch for stubbed results**: when the search card
  carries only STUB with no completion label, one follow-up GET to
  the fiction page pulls the real status (Complete / In-Progress /
  Hiatus / Dropped / Inactive) and combines them. Some stubbed
  fictions don't expose completion anywhere public on RR; those
  still display as plain `Stubbed`.
- **List browse for RR**: a new `Browse` dropdown on the Royal Road
  tab lets you pull one of RR's curated lists — Best Rated, Trending,
  Active Popular, Weekly/Monthly Popular, Latest Updates, New
  Releases, Complete, Rising Stars — instead of a free-text search.
  Tags still filter the list. CLI equivalent: `--rr-list "rising
  stars"` (no `--search` argument needed).

## 1.6.2 — 2026-04-17

### Fixes

- **Series parts split across search pages now merge**: the collapse
  ran per-page, so `Miss Abby` on page 1 and `Miss Abby Pt. 02` on
  page 2 stayed as separate rows. Load-more now re-collapses the
  full accumulated list (GUI rebinds focus to the first new row so
  keyboard users aren't lost; CLI reprints the whole list so the
  numbers still line up).
- **Annual/year URL slugs no longer falsely group**: `/s/foo-2023`
  and `/s/foo-2024` used to collapse as a "series" because of the
  bare trailing number. The URL pattern is now accepted only when
  the title also carries a recognisable chapter marker (`Ch. NN`,
  `Pt. NN`, `- N`, or `P<N>`).
- **Slug-collision guard for bare-titled adoption**: if a standalone
  `/s/foo` coexists with an unrelated later serial `/s/foo-ch-01,
  /s/foo-ch-02` by the same author, the standalone is no longer
  folded into the serial. Adoption only happens when the existing
  group doesn't already have an explicit Part 1.

## 1.6.1 — 2026-04-17

### Fixes

- **Literotica series grouping misses bare-titled Part 1s**: Literotica's
  convention is to post the first part of a serial with no suffix on
  the title or URL, then append `Pt. 02` / `Ch. 02` / `- 2` on later
  parts. The 1.6.0 collapse only matched suffixed titles, so the bare
  part 1 stayed as a separate row alongside its own collapsed series.
  A second pass now adopts any bare-titled work whose URL slug equals
  the base stem of an existing suffixed group (same author).
- **"- N" and "P<N>" suffixes** (e.g. `Housewife Comes Out - 6`,
  `Under the Heels of Eleonora Vane P4`) are now recognised as chapter
  markers alongside the existing `Ch. NN` / `Pt. NN` patterns.
- **Enter on a series row opens "Show Parts"** instead of kicking off
  the full merge download. Keyboard-only users (NVDA) couldn't easily
  expand a series to see what's inside it; the merge download is still
  one button-press away via *Download Selected*.

## 1.6.0 — 2026-04-17

### Search

- **Literotica series grouping**: results whose titles and URL slugs
  match the `Ch. NN` / `Pt. NN` pattern now collapse into a single
  series row per base title. Downloading the row resolves the anchor
  part's canonical `/series/se/<id>` so chapters that didn't appear
  in the search are still pulled, then merges everything into one
  file. Falls back to the visible parts if no series link is found
  on the page.
- **AO3 series collapse fix**: a lone work that happened to be part of
  a series was being promoted into a "Series" row with one part, hiding
  the work's real title behind the series title. Collapse now requires
  at least two parts of the same series to appear in the results.

## 1.5.0 — 2026-04-17

### Downloads

- **Adaptive (AIMD) inter-chapter delay**: the scraper no longer sleeps a
  fixed 1–3s (or 2–5s for FFN) between every chapter. Sites that aren't
  rate-limiting get full-speed downloads — the delay starts at 0 and only
  grows (doubling, capped at 60s) if a fetch comes back 429/503. After
  the site stops pushing back it decays ~10% per successful fetch toward
  the site's floor. FFN keeps a 2s floor since it's known to bulk-captcha;
  AO3, Royal Road, FicWad, Literotica, and MediaMiner start at 0.
  `--delay-min` / `--delay-max` still override AIMD with a fixed range
  for anyone who wants the old behavior.

## 1.4.0 — 2026-04-17

### Fixes

- **Royal Road download crash** (`'NoneType' object has no attribute 'get'`):
  the anti-piracy stripper called `tag.decompose()` while iterating the
  same tree, which left orphaned descendants whose `attrs` became `None`
  and crashed the next `tag.get("class")`. Hidden tags are now collected
  before any are removed.

## 1.3.1 — 2026-04-17

### Fixes

- **Auto-updater freeze**: the download-progress callback was calling
  `wx.ProgressDialog.Update()` from the worker thread, which deadlocks
  the main event loop — the app downloaded the new build and then
  froze. Progress is now marshalled through `wx.CallAfter` (throttled
  to ~10 Hz) and cancel state goes through a `threading.Event` instead
  of a cross-thread widget read.

## 1.3.0 — 2026-04-17

### Search

- **Load more / pagination**: every `search_*` function now takes a
  `page` argument and the hard 25-result cap is gone. The CLI gains
  `--limit` and `--start-page`; the GUI has a **Load More** button per
  search tab and an `m` prompt in interactive CLI search.
- **FFN sort**: `--sort updated/published/reviews/favorites/follows`
  for CLI and a matching dropdown in the GUI FFN tab.
- **AO3 series collapse**: results that belong to a single AO3 series
  now show up as a series row tagged `[Series · N part(s)]`, hiding
  the individual work. Downloading the row merges the full series
  into one file. A **Show Parts...** dialog in the GUI lets you pull
  up the parts and grab just one.

### Author & bookmark picker

- **Multi-select GUI picker**: pasting an author URL (FFN, FicWad,
  AO3, Royal Road, MediaMiner, Literotica) or an AO3 bookmarks URL
  (`/users/NAME/bookmarks`) now opens a dialog with one checkbox per
  story. Pick any subset instead of auto-downloading everything.
- **Sort in the picker**: title, word count, chapter count, last
  updated, and section (own vs. favorites).
- **FFN favorites**: the picker includes the author's favorite
  stories alongside their own, tagged `[Favorite]`. Filter to "Own
  only", "Favorites only", or "All".

### GUI performance

- Status log now batches writes through a 100ms timer and drops the
  `TE_RICH2` style. Long downloads that used to visibly hang while
  logging progress line-by-line now stream smoothly.
- Status log is capped at 5000 lines (oldest trimmed), so long
  sessions don't accumulate unbounded text.
- Search results ListCtrl populates inside `Freeze`/`Thaw` to
  eliminate row-by-row redraw flicker.

## 1.2.0 — 2026-04-17

### New sites

- **Archive of Our Own** (`archiveofourown.org`) — full scraper with
  single-page (`view_full_work=true`) fetches, adult-content gate bypass,
  paginated author pages, and `/series/<id>` expansion.
- **Royal Road** (`royalroad.com`) — fictions, author pages, status
  labels, and cover URLs. Strips the site's anti-piracy paragraphs by
  parsing the page's `<style>` blocks for `display:none` rules and
  dropping any element carrying a matching class.
- **MediaMiner** (`mediaminer.org`) — niche anime/manga archive; stories
  at `/fanfic/view_st.php/<sid>` or `/fanfic/s/<cat>/<slug>/<sid>`,
  chapter bodies in `#fanfic-text`, author pages at
  `/fanfic/src.php/u/<name>`.
- **Literotica** (`literotica.com`) — stories paginated as `?page=N` are
  mapped to chapters; series expand via `/series/se/<id>`. Selectors
  match on stable CSS-module prefixes so the scraper survives build churn.

### Search

- Built-in search tabs in the GUI for **FFN**, **AO3**, and **Royal Road**,
  each with site-specific filters.
- FFN filters: rating, language, status, genre, word count, crossover,
  match-field (title / summary).
- AO3 filters: rating, completion, crossover, sort column, plus free-text
  fandom / character / relationship / word-count range.
- Royal Road filters: status, type (original / fanfiction), sort, tag list.
- Search tab selections persist across launches.

### Update mode

- `--update-all DIR` scans a folder of previously-downloaded exports and
  refreshes any that gained chapters. Cheap chapter-count probe per
  story, so unchanged fics cost one HTTP request.
- `-r/--recursive`, `--dry-run`, `--skip-complete` for `--update-all`.
- `--probe-workers N` runs the probe phase concurrently (default 5).
- AO3 update path uses a bare `/works/<id>` probe before doing the
  expensive `view_full_work` fetch.

### Export

- `--hr-as-stars` replaces `<hr/>` scene breaks with a centred `* * *`
  divider in HTML and EPUB output.
- `--strip-notes` drops paragraphs that start with A/N, Author's Note,
  etc. AO3 structured notes are already excluded at scrape time.
- `--merge-series` combines every work in an AO3 series into a single
  EPUB, each work rendered as an intro chapter followed by its own
  chapters. Also honoured for Literotica series.
- `--chapters SPEC` limits downloads to specific chapter numbers or
  ranges (e.g. `1-5`, `20-`, `1,3,5-10`).
- EPUB/HTML CSS picks up book-style paragraph indent (suppressed after
  headings and scene breaks), italicised blockquotes, and letter-spaced
  scene-break markers.
- EPUB Dublin Core `source` / `identifier` / `publisher` now reflect the
  actual origin site instead of always saying "fanfiction.net".

### Audiobook

- **Voice preview** dialog in the GUI — click "Preview Voices...", fetch
  chapter 1, listen to each detected character's assigned voice before
  committing to a full audiobook generation. "Change Voice..." swaps
  voices and writes straight back to the story's voice-map JSON.

### Delivery

- `--use-wayback` falls back to an archive.org snapshot when the live
  site 404s or keeps failing. Useful for deleted fics.
- `--send-to-kindle EMAIL` emails each exported file to the supplied
  address via SMTP (configured through `SMTP_HOST` / `SMTP_USER` /
  `SMTP_PASSWORD` env vars).

### FFN-specific

- Short-form author URLs (`fanfiction.net/~name`) resolve correctly
  instead of falling through to the story parser.
- Chunked chapter fetches with a ~60-second pause every 20 chapters
  (default, tunable via `--chunk-size`) to avoid tripping FFN's
  captcha wall on long fics.
- Author-page scraping no longer includes the author's favourites.

### Preferences & updates

- Filename template, format, output folder, `--hr-as-stars`,
  `--strip-notes`, and per-site search filter selections persist via
  `wx.Config` (registry on Windows, dotfile elsewhere).
- Startup update checker queries GitHub's latest-release endpoint. On
  Windows frozen builds it can download the new exe and swap it in
  place; on other platforms it opens the release page.

### Tests

- 100 passing unit tests with saved HTML fixtures for FFN, AO3,
  FicWad, Royal Road, MediaMiner, Literotica; URL parsing, metadata
  parsing, chapter extraction, search URL builders, updater round-trips,
  exporter helpers. GitHub Actions runs them on every push.

---

## 1.1.1 — 2026-04-16

- Improved dialogue attribution (consecutive-quote fallback, possessive
  stripping, fanfic-style attribution verbs, name consolidation).

## 1.1.0

- Expanded character-voice name detection for speaker identification.

## 1.0.x

- Initial releases: FFN + FicWad download, EPUB / HTML / TXT / M4B
  export, character-voiced audiobook generation, update mode, batch
  downloads, clipboard watch, author-page scraping.
