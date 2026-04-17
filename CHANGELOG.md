# Changelog

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
