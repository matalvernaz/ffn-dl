# Changelog

## 2.1.0 — 2026-04-25

### Add

- **LLM attribution backend (Ollama / OpenAI / Anthropic / OpenAI-compatible).**
  Audiobook generation gains a fourth speaker-attribution backend that
  sends each chapter to a Large Language Model and asks it to label
  every quoted line. Recent research (LLaMa-3 evaluations on the
  Project Dialogism Novel Corpus) puts well-prompted LLMs above
  BookNLP-big on quotation accuracy, and the new backend lets users
  pick whichever provider they have available — local Ollama (no API
  key, runs offline) or a remote provider (OpenAI / Anthropic / any
  OpenAI-compatible endpoint such as Groq, OpenRouter, vLLM, ...). The
  CLI exposes ``--attribution llm`` plus ``--llm-provider``,
  ``--llm-model``, ``--llm-api-key``, and ``--llm-endpoint`` (with
  fallbacks to ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` /
  ``OPENROUTER_API_KEY`` env vars and the GUI prefs). The GUI shows
  an "LLM settings..." button next to the Attribution dropdown when
  the backend is selected; the modal asks for provider, model name,
  API key, and an optional endpoint override. The per-chapter
  attribution cache keys on (provider, model) so an Ollama-llama3
  result doesn't overwrite a GPT-4o result for the same chapter.

- **Character-list grounding for every attribution backend.** The
  metadata-derived cast list (FFN's bare-segment characters, AO3's
  character tags, FicWad's story-characters span) is now plumbed
  into the attribution pipeline as a closed-world prior. The LLM
  backend bakes the list into its prompt so model output stays
  inside the known cast wherever possible. The heuristic
  post-attribution passes treat cast members as confirmed speakers
  on their first occurrence (so "I'm Padma," in a story tagged
  with Padma Patil now binds correctly even before she speaks
  again) and skip the junk-speaker demotion for any cast name that
  happens to clash with a junk word ("Captain" in a Marvel fic
  tagged Captain America stays Captain instead of being demoted
  to narrator).

## 2.0.4 — 2026-04-24

### Fix

- **403-retry log noise: per-attempt warnings demoted to debug.** The
  scraper's retry path was logging every attempt of a transient 403 at
  WARNING. In real-world FFN traffic ~50% of requests hit a 403 that
  resolves on the very next try (the second request lands on the
  Cloudflare edge cache that the first one warmed up), and the
  per-attempt WARN spam was burying actually-stuck failures and
  dominating screen-reader output during library updates — one log
  sample showed 339 warnings in a single run, all self-resolved.
  Attempt 0 is now logged at DEBUG; escalations (attempt 1+, slow-retry
  tier, browser rotations) still WARN. A correlation-context-scoped
  counter tallies the resolved 403s and emits a single INFO summary at
  context exit (``"Resolved N transient 403 retries during this
  session"``) so the aggregate signal is preserved without the
  per-attempt noise.

## 2.0.3 — 2026-04-24

### Fix

- **BookNLP attribution no longer leaks its temp directory.**
  ``_refine_with_booknlp`` was creating a ``ffn-booknlp-*`` working
  dir under ``/tmp`` (holding the full book text plus BookNLP's
  ``.tokens`` / ``.entities`` / ``.quotes`` TSVs — tens of MB per
  long fic) and never deleting it. Every audiobook build with the
  BookNLP backend left one behind. Wrapped the body in
  ``try/finally`` and ``shutil.rmtree`` on exit so the dir is
  cleaned up even if BookNLP raises mid-process.

## 2.0.2 — 2026-04-24

### Fix

- **Screen-reader labels on Preferences fields and Optional
  Features buttons.** ``_make_labeled_row`` was creating its
  StaticText after the control in window Z-order, so MSAA walked
  backward from the control and found the wrong StaticText as
  the implicit label — which made the Notifications tab read
  the section headers ("Pushover", "Discord", "Email") as the
  first field's label and shifted every following field's label
  by one. Helper now calls ``ctrl.MoveAfterInTabOrder(static)``
  so the label sits in the right Z-order position, and
  notification labels carry their service name as a prefix
  ("Pushover API token", "Discord webhook URL", "Notification
  email address") so each field stands alone even without the
  section-header StaticText (which is now gone, since MSAA kept
  eating the first label anyway).
- **Optional Features install buttons now include the feature
  name.** Four identical "Reinstall..." buttons read
  indistinguishably under a screen reader; users couldn't tell
  which row focus was on. Buttons are now "Install EPUB
  export...", "Reinstall Audiobook synthesis (edge-tts)...",
  etc. — self-describing per-row labels, with the accelerator
  still on the action verb.

## 2.0.1 — 2026-04-24

### Docs

- README reorganised: opening paragraph now lists every supported
  site, split into fanfic / original-fiction and erotica buckets,
  so the twelve erotica sites (Literotica, AFF, StoriesOnline,
  Nifty, SexStories, MCStories, Lushstories, Fictionmania,
  TGStorytime, Chyoa, Dark Wanderer, GreatFeet) are visible
  alongside the obvious ones.
- GUI section corrected: the main window is a download form, not
  a tabbed Notebook. Search lives on its own menu with five
  windows (FFN, AO3, Royal Road, Wattpad, Erotic Story Search),
  each with its accelerator and filter surface called out.
- CLI examples expanded with Literotica / StoriesOnline URLs and
  a literotica-scoped search. The fan-out search note is now
  truthful — erotica fan-out is GUI-only; the CLI only exposes
  single-site erotica searches via ``--site literotica``.
- GUI launch instructions corrected — ``ffn-dl-gui`` was never a
  real entry point. ``ffn-dl`` with no arguments launches the
  GUI (what double-clicking the desktop binary does); from a
  pip install, ``python -m ffn_dl.gui`` is the explicit form.
- Migration note targets the 2.0.0 transition rather than the
  mid-session 1.23.34 number.

## 2.0.0 — 2026-04-24

A milestone release folding in a month of library-management,
anti-bot, and cross-platform work. Highlights:

### Features

- **Full-text library search.** ``--populate-search DIR`` builds a
  SQLite FTS5 index of every indexed story's chapter bodies;
  ``--library-search QUERY`` queries it with full FTS5 syntax
  (prefix wildcards, NEAR, boolean operators). BM25-ranked,
  cross-library by default, scopeable via ``--library-dir``.
  Metadata-only search (``--library-find``) still lives alongside
  for the "I remember the title" cases. Subsequent
  ``--update-library`` runs keep the index warm on touched
  stories; direct-URL downloads land in the index on the next
  ``--populate-search`` sweep.

- **Cross-site mirror detection.** ``--find-mirrors [DIR]`` reports
  suspected duplicate pairs — the same story posted to FFN and
  AO3, Literotica and StoriesOnline, etc. Three signals (normalised
  title match, normalised author match, first-chapter word
  overlap) and a ≥2-of-3 rule keep common titles from triggering
  false positives. Read-only; never deletes. Handles CJK /
  Cyrillic / accented titles as first-class rather than falling
  through the ASCII filter.

- **Playwright-backed Cloudflare-challenge fallback.** Opt in with
  ``--cf-solve``: on a stubborn 403 the scraper launches a headless
  Chromium via Playwright, waits for the challenge to clear, and
  injects the solved cookies into the curl_cffi session. Cookies
  are persisted for 24 h under ``~/.cache/ffn-dl/cf-cookies/``
  (chmod 0600) so later runs reuse them without re-launching the
  browser. Opt-in because Playwright ships a ~400 MB browser
  binary.

- **In-app installer for optional features.** **Edit → Optional
  Features...** lists every optional PyPI extra (``epub``,
  ``audio``, ``clipboard``, ``cf-solve``) with its current status
  and an Install / Reinstall button. Frozen builds pip-install
  into a portable ``deps/`` folder alongside ``ffn-dl`` so "delete
  the folder" really is a clean uninstall; ``cf-solve`` chains
  ``playwright install chromium`` automatically. Pip output
  streams into the dialog log pane.

- **macOS and Linux portable binaries.** CI now builds an Apple-
  Silicon tarball (``ffn-dl-macos-arm64.tar.gz``) on macos-latest
  and an x86_64 Linux tarball (``ffn-dl-linux-x86_64.tar.gz``) on
  ubuntu-latest, both with static ``ffmpeg`` / ``ffprobe``
  bundled. GUI stays accessible on each platform — wxPython wraps
  native Cocoa on macOS (VoiceOver reads the widget tree) and
  GTK3 on Linux (Orca reads via at-spi2).

- **Silent-edit detection.** Authors quietly revise chapters
  without bumping the chapter count; count-based update checks
  miss those drifts. ``--populate-hashes DIR`` seeds per-chapter
  SHA-256 baselines; ``--scan-edits DIR`` probes upstream and
  reports content drift separately from count changes.
  ``--update-library`` refreshes baselines on every successful
  download so silent-edit detection stays current.

- **Stale-complete probe gate.** ``--skip-stale-complete DAYS``
  skips stories that are both marked Complete and have a file
  mtime older than the threshold. Gentler than ``--skip-complete``
  — a fic completed yesterday is still probed (the author may
  add an epilogue), but one untouched for a year stops costing an
  HTTP probe each run. ``--force-recheck`` overrides; pending
  resumes (``remote_chapter_count > local``) bypass.

- **Abandoned-WIP detection.** WIPs that haven't seen a new
  chapter in ages were costing a probe per update run forever.
  ``--scan-library`` now auto-marks WIP stories (status !=
  Complete) whose file mtime is older than
  ``library_abandoned_after_days`` days; marked stories are
  skipped by every subsequent ``--update-library`` run until
  revived. The threshold is configurable from the Library
  dialog in the GUI; pref default is 0 (off) so upgraders don't
  wake up to a pile of silently-dead fics. Management commands:
  ``--list-abandoned`` to review, ``--revive-abandoned URL`` to
  clear one, ``--revive-abandoned`` with no argument to clear
  every abandoned entry in scope. The GUI's Library dialog gets
  a **Manage Abandoned...** button that opens a list + revive
  surface.

### Changes

- **Auto-sort cleans category strings before picking a folder.**
  FFN's ``Books > Harry Potter`` breadcrumb now lands in
  ``Harry Potter/``, not ``Books _ Harry Potter/``. AO3 crossovers
  joined with `` / `` split into distinct fandoms and route to
  the misc bucket instead of ``Harry Potter _ Naruto/``. Single-
  fandom strings with none of those separators pass through
  untouched.

- **Royal Road downloads go to ``Original Works/``, not Misc.**
  RR's catalogue is entirely original fiction, so "no fandom" on
  an RR story means the work is original, not unclassifiable.
  Configurable via the ``library_original_folder`` pref
  (default: ``Original Works``); an explicit category on an RR
  story still wins.

- **Portable Playwright browser path on frozen Windows.**
  ``PLAYWRIGHT_BROWSERS_PATH`` is pinned inside the portable
  folder so the Chromium binary lands next to the .exe. "Delete
  the ffn-dl folder" used to leave the browser stranded under
  ``%LOCALAPPDATA%\\ms-playwright``. Scoped to frozen builds so
  pip-installed users' existing ``playwright install`` layout is
  respected.

### Fixes

- **cf-solve cookie cache propagates across worker threads.**
  Under concurrent library updates, workers that hit their first
  403 before the solving worker persisted cookies used to be
  short-circuited and exhaust their retry budgets solo. The seed
  path now re-reads the disk cache on every 403, so a solve by
  worker A is immediately available to workers B..N on their next
  retry.

- **cf-solve cookie file is chmod 0600** on POSIX so a session
  token another local user could replay doesn't sit at the
  default umask. Windows ignores POSIX mode bits, no-op there.

- **Clearer error when SQLite lacks FTS5.** Stripped-down SQLite
  builds surface an opaque ``no such module: fts5``;
  ``--populate-search`` now raises a RuntimeError that names the
  cause and points at the fix.

### Migration

- Auto-sort layout for FFN and Royal Road downloads has changed
  (see the two entries under Changes). Upgrading users with an
  existing library can migrate with
  ``ffn-dl --reorganize ~/Fanfic --apply`` — the dry-run
  (omit ``--apply``) prints proposed moves first.

### Tests

- +129 tests across the session: FTS5 search primitives, mirror-
  detection signals and Unicode fallbacks, cf-solve round-trip +
  concurrency + permissions, optional-features installer, auto-
  sort category parsing, stale-complete gate, silent-edit
  detection, abandoned-WIP mark/revive/list + scan-time
  auto-sweep + refresh-queue skip. Full suite: 996 green.

## 1.23.25 — 2026-04-23

### Feature

- **Silent-edit detection.** Authors quietly revise chapters — fix
  typos, tweak dialogue, sometimes rewrite scenes — without changing
  the chapter count. The count-based ``--update-library`` check
  can't see those, so local copies drift from canon. Two new CLI
  flags cover this:

  - ``--populate-hashes DIR`` seeds a per-chapter SHA-256 for every
    story in DIR's library by re-parsing the local EPUB/HTML. Run
    once to bootstrap an existing library; subsequent ``--update-
    library`` runs refresh hashes on every successful download so
    the baseline stays current.
  - ``--scan-edits DIR`` probes every story's upstream, hashes the
    fresh chapters, and reports drift. Silent edits (content change
    under an unchanged count) and count changes (handled by the
    regular update path) are reported separately. Exits with code 2
    when drift was found so shell callers can branch.

  Hashing normalises whitespace and drops cosmetic inter-tag
  whitespace so a re-export through a different parser doesn't
  trigger a false-positive flag.

### Change

- **Literotica merged into the unified Erotic Story Search.**
  Literotica no longer has its own GUI search window — the standalone
  frame was a narrower version of the fan-out surface and maintaining
  both caused confusion about which to open. The unified search
  already covers Literotica and auto-collapses Literotica series
  across its results, so nothing functional is lost. The search
  menu's accelerator keys shift up by one: Wattpad is now Ctrl+4,
  Erotic Story Search is Ctrl+5.

### Tests

- 33 new tests. Content-hash primitives (normalisation under
  whitespace churn, ordering, diffing), bootstrap-hashes flow
  (populate, skip-existing, force-rehash, skip-missing,
  unreadable-TXT), scan-edits flow (unchanged / silent-edit / count-
  change detection, missing-baseline handling, StoryNotFound
  fallback). Full suite: 900 green.

## 1.23.24 — 2026-04-23

### Feature

- **Integrated ``--doctor`` command.** Runs every hygiene check in
  one pass: library integrity across every indexed root, watchlist
  integrity, and scraper cache (size + orphan entries). Read-only by
  default; add ``--heal`` to apply the full set of safe fixes. The
  library index is auto-backed-up before the heal so a misdiagnosed
  run can be rolled back with ``--restore-index``.

### Tests

- 12 new tests for the integrated doctor: empty-everything-clean,
  per-surface drift detection, cross-surface heal, auto-backup
  behaviour, and summary rendering. Full suite: 867 green.

## 1.23.23 — 2026-04-23

### Feature

- **Index backup and restore.** Three new CLI flags:
  ``--backup-index`` (write a timestamped copy of the current
  library index), ``--list-backups`` (show existing backups newest
  first), and ``--restore-index FILE`` (atomically swap in a backup).
  A rolling policy keeps the ten most recent backups. Destructive
  operations (``--library-doctor --heal``) auto-backup before
  mutating, so a misdiagnosed heal can be rolled back without the
  user having remembered to take a snapshot.
- **Watchlist doctor.** ``--watchlist-doctor`` reports malformed
  watchlist entries: invalid ``type``, empty target URL,
  unsupported site, URLs that no registered scraper recognises,
  and duplicates (``type`` + URL for story/author watches;
  ``site|query|filters`` for search watches). ``--heal`` drops
  unrepairable entries. Parallel to the library and cache doctors
  from the previous rounds.

### Tests

- 33 new tests. Backup/restore (rolling prune, atomic overwrite,
  timestamped listing, restore leaves backup intact), watchlist
  integrity checks across every category (invalid type, empty
  target, unsupported site, unresolvable URL, story vs. search
  dedupe keys), and summary-rendering invariants. Full suite: 855
  green.

## 1.23.22 — 2026-04-23

### Feature

- **Cover image cache.** Exporters re-downloaded the cover image on
  every run; now they cache under the portable cache dir keyed on a
  hash of the cover URL, with a 7-day TTL. Re-exporting a story,
  switching output formats, or exporting a long anthology where
  every part shares a cover URL all skip the network. Cache is
  best-effort — a disk full or permission issue doesn't fail the
  export, just disables caching for that run.
- **Scraper-cache doctor.** ``--cache-doctor`` reports on the scraper
  cache at ``~/.cache/ffn-dl``: total size, per-site distribution,
  top-ten largest entries, and (when a library index exists) orphan
  cache directories for stories no longer tracked in any library.
  Add ``--prune`` to delete the orphans. Complements the library
  doctor from 1.23.21 — both together cover the two disk-hygiene
  surfaces the program owns.
- **Library search.** ``--library-find QUERY`` does a case-insensitive
  substring search across every indexed story's title, author,
  fandom list, and URL — joined into a single haystack so multi-word
  queries like "harry potter au" can match across fields. Pass
  ``--library-dir DIR`` to scope to one root; otherwise all indexed
  libraries are searched. Each hit prints its title/author, fandom,
  status, chapter count, relative path, and canonical URL.

### Tests

- 38 new tests. Cover cache (TTL, failure paths, URL-keying,
  corrupt-entry fallthrough), scraper cache doctor (per-site
  distribution, orphan detection, prune semantics), library search
  (per-field matching, multi-root scoping, limit, convenience
  accessors). Full suite: 822 green.

## 1.23.21 — 2026-04-23

### Feature

- **Library doctor.** A new ``--library-doctor DIR`` CLI flag reports
  index/disk drift: entries pointing at files that no longer exist,
  orphan files on disk not in the index, mtime/size cache drift that
  would defeat the refresh-path "skip unchanged" optimisation, and
  stale records in the untrackable list. Read-only by default; add
  ``--heal`` to apply every recommended fix in one pass. Exits with
  status 2 when drift is detected in read-only mode so shell callers
  can detect it programmatically.
- **Library stats.** ``--library-stats DIR`` prints a summary of
  what's actually in a library: totals, per-site / per-status /
  per-format counts, top-ten fandoms, and freshness breakdown
  (never-probed, probe older than 30 days, pending updates where
  upstream has more chapters than local). Fully read-only.
- **Correlation IDs on download logs.** Every ``download()`` call now
  runs inside a fresh correlation context so log lines emitted
  anywhere in the stack — scraper, cache, exporter, library code —
  are tagged with the same ``[dl-<id>]`` prefix. Makes triage of
  library-wide update runs tractable: instead of eyeballing
  timestamps to figure out which warning belongs to which story,
  ``grep [dl-a83f4c21]`` produces the full story's trace.
- **Batch failure summary now shows the reason per entry.**
  ``--update-all``'s end-of-run Failed list used to be just relpaths;
  you had to scroll back to find the matching exception. The summary
  now carries a "→ <reason>" line under each failure, capturing the
  probe error or the exception class name + message directly.

### Fix

- **Atomic writes on every story output.** TXT, HTML, and EPUB
  exporters previously streamed directly to the destination path,
  so a crash (Ctrl-C, OS kill, power loss) mid-export left a
  truncated file in the library that the next scan treated as
  valid — masking the need to re-download. Every export now goes
  through a tmp-file + fsync + atomic rename, matching what
  ``LibraryIndex.save`` and the watchlist already did. Same change
  applied to the scraper's meta / chapter caches so a partial write
  there can't leave a valid-looking but half-populated cache entry.

### Change

- **BaseScraper.download auto-wraps with a correlation context.**
  Implemented via ``__init_subclass__`` so every site scraper picks
  up the tag without any callsite changes. The context is thread-
  local (via ``ContextVar``), so concurrent downloads in a library
  pass stay distinguishable.
- **``_fetch_part_text`` safety-cap constant extracted.** Wattpad's
  200-page upper bound is now ``_MAX_PART_PAGES``, with docstrings
  and a truncation notice surfaced to the reader when it fires.
  (Shipped in 1.23.20 — consolidated here for completeness.)

### Tests

- 75 new tests. Atomic-write invariants (content, no tmp residue,
  exception rollback), library integrity check + heal for every
  drift category, library stats distributions and freshness buckets,
  correlation-id scoping / thread-isolation / LogRecordFactory, end-
  to-end exporter output for TXT/HTML/EPUB including EPUB3 ZIP
  structure (mimetype first, container.xml valid, manifest
  consistent), and atomic-rollback on simulated ``write_epub``
  failure. Full suite: 784 green.

## 1.23.20 — 2026-04-23

### Change

- **Shared chapter-orchestration helper.** FicWad, Royal Road, and
  MediaMiner's download loops were the same ~30-line plan/fetch/cache
  block with per-site cosmetic differences. Extracted
  ``BaseScraper._materialise_chapters`` so future changes (retry-on-
  parse-error, progress reporting, cache layout) land in one place
  instead of three.
- **AFF author-link resolution hardened.** AFF rotates its author-
  link pattern every few years; the resolver now walks a chain of
  href shapes (modern ``profile.php?id=``, legacy
  ``authorlinks.php?no=``, members-subdomain variants) then falls
  back to any anchor inside a container whose class contains
  ``author``. A full redesign of the href template degrades to the
  structural fallback instead of losing the author field on every
  story.
- **MediaMiner breadcrumb title parser accepts multiple separator
  glyphs.** The current ❯ (U+276F) plus ›, →, », and ASCII ``>`` are
  all recognised, so a font / CSS refresh won't silently leave the
  fandom glued to the story title. Empty segments from stray
  leading/trailing separators are discarded rather than leaked into
  the EPUB title.
- **MediaMiner chapter label regex picks up ``Ch. 3`` variants**
  alongside ``Chapter 3``.
- **BaseScraper abstract contract is explicit.** The optional bulk-
  scrape methods (``scrape_author_stories``, ``scrape_author_works``,
  ``scrape_series_works``, ``scrape_bookmark_works``) now default to
  ``NotImplementedError`` with a message naming the ``is_*_url``
  check the caller should have gated on. Pasting a Wattpad series
  URL (Wattpad has no series concept) produces a clear error
  instead of a misleading ``AttributeError``.

### Fix

- **Wattpad 200-page safety-cap firings are now surfaced.** When the
  storytext endpoint paginates past the cap, the chapter HTML is
  prepended with a reader-visible ``wattpad-truncation-notice`` and
  the truncated body is not cached — if upstream starts serving the
  full body again, the next run picks it up instead of being locked
  into the stale partial.

### Tests

- 38 new tests. Highlights: 8 direct tests for
  ``BaseScraper._materialise_chapters`` (planning, cache-first,
  progress callback, total override), 4 for Wattpad truncation
  handling (flag set, notice text, no-cache, healthy chapter
  unaffected), 6 for AFF author-link fallback layers, 7 for the
  MediaMiner breadcrumb splitter across separator glyphs, and 8
  contract tests pinning the ``is_*_url → scrape_*`` invariant.

## 1.23.19 — 2026-04-23

### Fix

- **Royal Road: ``date_updated`` picked up the last table row rather
  than the newest chapter.** Authors who insert a bonus/omake chapter
  out-of-sequence (e.g. a 2024 "Chapter Ω1" slotted next to 2019's
  Chapter 4) leave the last row at an older timestamp than a middle
  row. The scraper now derives publish/update dates from
  ``min``/``max`` of the per-chapter timestamps instead of first/last,
  so the library shows the real last-update date and update-mode
  refetches at the right time.
- **AO3: series with more than 20 works silently dropped the tail.**
  ``scrape_series_works`` fetched a single page; AO3 paginates series
  at 20 works/page. The scraper now walks ``rel="next"`` like the
  author-scrape path, so 30-work and 50-work series collect cleanly.
- **Wattpad: bracket-matching story-object extractor is now string-
  and escape-aware.** The prior implementation counted raw braces and
  relied on Wattpad escaping braces inside strings as ``\\u007b`` —
  any change to that serialiser or a user title containing a raw
  brace would have split the enclosing JSON object mid-literal. The
  new implementation ignores braces inside JSON string literals and
  handles ``\\"`` / ``\\\\`` escapes correctly.
- **FicWad: Published/Updated timestamps now assigned by label, not
  table order.** The old code indexed the first ``data-ts`` span as
  ``date_published`` and the second as ``date_updated``; a layout
  flip would have silently swapped them. The parser now reads the
  label immediately preceding each span.

### Change

- **Literotica: three-layer selector chain for the story body.**
  Literotica's CSS-module class names rebuild per release
  (``_article__content_10cj1_81`` today, a different hash tomorrow).
  The scraper now prefers the ``itemprop="articleBody"`` microdata
  attribute — the stable contract screen readers and indexers rely
  on — then falls back to the CSS-module prefix, then to an enclosing
  ``<article itemtype="schema.org/Article">``. Pure CSS-bundle
  rebuilds that would have broken the scraper now degrade gracefully.

### Tests

- Added 48 tests covering the above: Wattpad bracket-matcher string
  awareness, Literotica fallback chain, FicWad label-driven dates
  with a multi-chapter dropdown fixture, Royal Road min/max timestamp
  derivation, AO3 series pagination and adult-gate behaviour,
  MediaMiner edge cases, and a cross-site empty-page invariant for
  every erotica scraper that guards against silent-empty Story
  returns on gate/error responses.

## 1.23.16 — 2026-04-20

### Fix

- **Fresh-copies updates no longer fetch every story twice.** The
  old code path first downloaded only the new chapters (skip =
  existing count), then threw them away and re-fetched every
  chapter from 1 when ``refetch_all`` was set. On FFN that wasted
  10–15 seconds per story to rate-limit for nothing — multiply by
  a 700-story library and a Fresh-Copies run was burning ~2h of
  extra upstream traffic. ``_download_one`` (CLI) and
  ``_run_download`` (GUI) now pass ``skip_chapters=0`` directly on
  the initial fetch when refetch_all is set and skip the merge
  helper entirely.

## 1.23.15 — 2026-04-20

### Feature

- **Search results are now tickable.** Every site's search frame now
  shows a native checkbox plus a ``[x]`` / ``[ ]`` prefix on the
  Title column so NVDA announces the selection state clearly. Space
  toggles the focused row, and "Download Selected" now downloads
  every ticked row as a batch instead of just the single
  arrow-highlighted one. Falls back to the focused row when nothing
  is ticked, so the old "arrow down and press Download" flow still
  works unchanged.

- **GUI downloads auto-sort into library fandom folders.** When the
  Save-to folder matches the configured library root, new downloads
  drop into ``<library>/<fandom>/`` using the same template the CLI
  already uses for ``--update-all``. First time a fandom is needed
  the GUI asks before creating the folder; later downloads of the
  same fandom skip the prompt. On first launch after scanning a
  library, the GUI offers once to promote the library root to the
  default save location so the auto-routing takes effect without
  any pref-digging.

### Change

- **Library manager is now modeless.** The window no longer blocks
  the rest of the app — you can kick off a library update and keep
  downloading new stories from the main window while it runs.
  Re-opening the menu item raises the existing window instead of
  stacking duplicates. Matches the Watchlist window's model.

- **Library update check is cancellable.** A new "Cancel Update"
  button next to "Check for Updates" stops the probe + download run
  cooperatively: in-flight probes short-circuit, Phase 3 breaks
  between stories. Closing the library window mid-run now prompts
  ("An update check is still running — cancel it and close?") so
  users aren't surprised when a background run keeps hammering
  upstream after the window is gone.

## 1.23.13 — 2026-04-20

### Change

- **Replace the "Force Full Recheck" and "Update (Fresh Copies)"
  buttons with two checkboxes next to the "Check for Updates"
  button.** The two options are orthogonal — force-recheck controls
  whether the probe TTL is honoured, fresh-copies controls whether
  chapters 1..existing are re-downloaded — and three separate
  buttons couldn't combine them. Now the user ticks any combination
  before pressing Update. Checkboxes reset on dialog close so a slow
  fresh-copies run never sticks around as a hidden default. The
  status log names the active modifiers so it's obvious from the
  output which combination ran.

## 1.23.12 — 2026-04-20

### Feature

- **Library manager: new "Update (Fresh Copies)" button.** Mirrors
  the single-file ``--refetch-all`` / Ctrl+Shift+U escape hatch at
  the library-wide level: re-download every chapter from upstream
  instead of merging newly-downloaded chapters with the ones already
  on disk. Slower, but catches silent author edits to old chapters
  across a whole library in one pass. The existing "Check for
  Updates" button still does the fast merge-in-place flow by default.

## 1.23.11 — 2026-04-20

### Fix

- **Bump ``ffn_dl/__init__.py``'s ``__version__`` to match
  ``pyproject.toml``.** 1.23.10 shipped with ``__init__.py`` still
  reading ``1.23.9``; the installed build reported the old version
  so the self-updater saw 1.23.9 vs GitHub's 1.23.10, re-updated,
  relaunched, still read 1.23.9, and looped. Both version locations
  have to move together — the build-windows workflow enforces a
  match going forward so this can't happen again.

## 1.23.10 — 2026-04-20

### Performance

- **Merge-in-place updates: reuse existing chapters from disk instead
  of re-downloading every chapter on every update.** The old flow
  downloaded each updated story twice — once for the new chapters
  (``skip_chapters=existing``), then again from scratch so the
  exporter had all chapters in memory. The second pass was supposed
  to hit the local chapter cache, but for any story originally
  downloaded by another tool, or whose cache had been cleared, it
  re-fetched every chapter from upstream. A 175-chapter fic with 20
  new chapters paid a ~20 minute FFN re-download tax for nothing.
  Now the update path reads chapters 1..existing back out of the
  on-disk export and concatenates them with the fresh new ones,
  cutting updates to a single short download. New ``--refetch-all``
  CLI flag + Update File with Fresh Copy (Ctrl+Shift+U) GUI entry
  bypass the shortcut for the (rare) case where an author silently
  revised old chapters. Only ffn-dl's own HTML and EPUB exports are
  read back — TXT is lossy (HTML already stripped) so TXT updates
  automatically fall back to the full re-download. Non-ffn-dl or
  hand-edited files also fall back gracefully with a user-visible
  log line explaining why.

### Fix

- **Resume interrupted library updates without re-probing every
  story.** A ``--update-library`` run that crashed or was killed
  after probing but before downloading used to lose every probe
  answer — the next run re-probed the entire library before
  discovering the same pending work. Now each successful probe
  stamps both ``last_probed`` *and* ``remote_chapter_count`` onto
  the library index. On the next refresh, entries where
  ``remote_chapter_count > chapter_count`` are queued for download
  with ``remote`` pre-filled, so the probe phase skips the network
  call entirely and goes straight to the download. Pending entries
  also bypass the probe-recency TTL — no point waiting an hour to
  finish a download we already know is needed. ``on_probe_complete``
  now takes ``(url, remote_count)`` (``remote_count`` is ``None``
  for story-gone answers so the pending flag is cleared cleanly).

### Diagnostics

- **Per-chapter progress lines now surface in the library GUI's
  update log.** ``_download_one`` used to hardcode ``print()`` for
  chapter progress, which dropped into ``sys.stdout`` and never
  reached the library window. It now takes an optional
  ``status_callback`` that the library GUI wires to its own log
  surface, so a long library update shows ``[155/175] Chapter Title``
  live instead of sitting silent for minutes per story and feeling
  like a hang.

## 1.23.9 — 2026-04-20

### Fix

- **Detect deleted FFN stories on the new error-page shape, and
  stamp them so the TTL absorbs them instead of re-probing every
  run.** FFN used to render missing stories with ``<title>Story
  Not Found</title>``; sometime before 2026 they switched to the
  generic ``<title>FanFiction</title>`` with the message buried in
  a ``<div class=panel_warning>`` → ``<span class='gui_warning'>``
  block. ``_check_for_blocks`` only matched the title shape, so
  probes on deleted stories fell through to ``_parse_metadata``,
  raised ``ValueError("Could not find story profile")``, got caught
  by ``probe_entry`` as a transient failure, and were never stamped
  — the same ~20 dead stories drained back into every library
  update's probe queue forever. Now the panel_warning shape also
  raises ``StoryNotFoundError``, and ``probe_entry`` fires
  ``on_probe_complete`` on that error too (with an
  ``upstream_missing: true`` flag on the queue entry) so TTL can
  suppress the next probe. Transient failures (rate-limit,
  Cloudflare block, timeout) still stay unstamped and retry next
  run. Fixture ``tests/fixtures/ffn_story_not_found.html`` captures
  the current live shape so future FFN redesigns won't silently
  regress this again.

### Diagnostics

- **``LibraryIndex.mark_probed`` now logs stamped/missed URL
  counts.** Previously silent — if a path-normalisation mismatch
  between the probe's root and the stored library key sent every
  URL to a phantom empty library, ``touched`` returned 0 and no one
  noticed. The new INFO line (`mark_probed: stamped N/M under
  <key>`) makes those leaks visible in the debug log, and a WARNING
  line surfaces the first 5 URLs that didn't match any entry so the
  root cause is pinpointable.
- **Flush-failure handler in the GUI/CLI stamp loop now calls
  ``logger.exception`` instead of only posting a UI warning.** The
  previous ``except Exception`` swallowed tracebacks, so we had no
  way to diagnose a flush that ran but failed silently. The full
  stack now lands in the debug log alongside the user-facing
  status line.

## 1.23.8 — 2026-04-20

### Perf

- **Send the high-entropy Sec-CH-UA-* client hints Cloudflare demands,
  and halve the 403 retry backoff.** Root-caused the ubiquitous 403s
  on FFN: the challenge response carries a ``Critical-CH`` header
  listing nine client hints (``Sec-CH-UA-Bitness``, ``-Arch``,
  ``-Full-Version``, ``-Full-Version-List``, ``-Model``,
  ``-Platform-Version``, plus the three low-entropy ones curl_cffi
  already sends). Real Chrome re-requests with those hints set;
  curl_cffi never does. Without them, Cloudflare challenges every
  first contact — the retry only succeeded because CF's edge cached
  the real chapter HTML at the moment of the challenge (observed
  ``cf-cache-status: HIT, age: 8`` in the 200 diagnostic). We now
  pre-populate the six missing hints on every Chromium session with
  values matching curl_cffi 0.15's ``chrome`` target (Chrome 146 on
  macOS 10.15.7 Intel), so the first request should pass. Also
  dropped ``FORBIDDEN_QUICK_RETRY_S`` from 5 to 2 seconds: even when
  a retry does fire, the cache is populated immediately so a 2–4 s
  wait (with jitter) is plenty. Net effect: fewer 403s, and each
  remaining retry costs ~3 s instead of ~7 s. Across a 100-chapter
  library update this recovers on the order of minutes.

## 1.23.7 — 2026-04-20

### Fix

- **403 diagnostic was reporting an empty cookie jar even when one
  existed.** curl_cffi's ``Session.cookies`` iterates as cookie
  *names* (strings), not Cookie objects — so ``c.name`` raised
  AttributeError inside the diagnostic, the ``except Exception``
  swallowed it, and every line printed ``jar=[]``. Now reads from
  ``sess.cookies.jar`` (the underlying ``http.cookiejar.CookieJar``)
  and formats each entry as ``name@domain`` so you can see at a
  glance whether ``__cf_bm`` is persisting across the 403→200 pair.

## 1.23.6 — 2026-04-20

### Diagnostics

- **Tag FFN log lines with the story ID and title.** Batch runs
  (``--update-all``, multi-story CLI calls) previously emitted a
  bare ``Fetching story metadata...`` followed by ``Fetching chapter
  N/M`` with no hint of which story was in flight — so if chapter 50
  was the one 403-looping, you couldn't tell what to retry by hand.
  Metadata line now prints the story ID up front, and once the meta
  parse lands we log a single ``Downloading FFN <id>: <title> by
  <author> (<N> chapters)`` header before the chapter loop starts.
  AO3 already did this; other scrapers (FicWad, etc.) still don't.

## 1.23.5 — 2026-04-20

### Diagnostics

- **Log headers and body prefix on HTTP 403, and again on the 200
  that recovers from one.** Nearly every chapter fetch was eating a
  403 first and succeeding on the quick retry, adding ~5 s per
  chapter to library updates. Before tuning the backoff we need to
  know *why* — a cookie that isn't persisting, a fingerprint the
  impersonation profile isn't fooling, or a genuine Cloudflare gate
  all look the same at warning level. The new ``_log_fetch_diagnostic``
  emits a single DEBUG line on every 403 and on the success that
  follows, with the current browser profile, cookie-jar names,
  response headers, and the first 300 bytes of body. Diff the pair
  to see what actually changed between the forbidden request and
  the allowed one.

## 1.23.4 — 2026-04-20

### Fix

- **Redact Lush's client-side Google Maps key from the test
  fixture.** When I captured ``lush_story.html`` for parse tests,
  it included Lushstories' own public Google Maps API key (sat in
  their inline config blob on every page). GitHub's secret
  scanner flagged it on the 1.23.1 push. Replaced the key with
  ``REDACTED_GOOGLE_MAPS_KEY_FOR_TESTS_...`` so future commits
  don't ship the same string. The key was never ours — any visit
  to lushstories.com sees it in page source — but there's no
  reason to keep it in our repo.

  The key was in git history from commit d610bab (v1.23.1); that
  history isn't rewritten since force-pushing would break every
  clone and the key is already on the public site anyway. This
  release stops bleeding it forward.

## 1.23.3 — 2026-04-20

### Fix

- **Incremental ``last_probed`` stamping.** ``last_probed`` was
  getting stamped in one shot at the very end of a library update.
  A user who closed the app mid-probe — easy to do during an
  800-story FFN scan (6 s/probe × 800 = ~80 minutes) — lost every
  stamp, so the *next* Check for Updates re-probed every story
  they'd already checked. This matched the reported symptom: a log
  showing TTL 6h / 0 skipped / 804 to probe even though the user
  had Check-for-Updates'd earlier.

  ``_run_update_queue`` now fires a per-URL ``on_probe_complete``
  callback after each successful probe (failures intentionally skip
  the callback so the TTL retries them next run). The GUI and CLI
  update paths buffer those URLs in memory and flush to the index
  in batches of 25, plus a final flush at the end. Worst-case
  crash loses the last ~25 stamps instead of all N. Existing TTL
  and Force Full Recheck behaviours unchanged.

  Three new regression tests in
  ``tests/test_library_incremental_probe_stamping.py`` guard the
  contract: callback fires only on success, 25 stamps land on disk
  before the buffer flushes at threshold, and the final flush
  picks up the trailing under-threshold batch.

## 1.23.2 — 2026-04-20

### Change

- **Library update TTL raised from 1 hour to 6 hours.** One hour was
  short enough that users who clicked Check for Updates, closed the
  dialog, and came back a couple hours later hit a full re-probe
  and assumed the TTL was broken. Six hours catches the "same
  session" and "came back later today" cases without gatekeeping
  next-morning probes. Force Full Recheck still bypasses, and the
  CLI flag still takes any value.

- **Check for Updates prints the TTL decision up front.** The
  status pane now starts an update scan with a line like
  ``TTL 6.0h: 183 recently-probed story(ies) skipped, 12 to probe.
  (Click Force Full Recheck to ignore the TTL.)`` so it's obvious
  whether the skip is firing or everything's getting queued because
  no entries have a ``last_probed`` stamp yet (common on the first
  probe after a fresh scan or an upgrade from a pre-TTL version).

## 1.23.1 — 2026-04-20

### Fix

- **AFF author parsing.** The story-header link pattern changed to
  ``profile.php?id=<N>`` on the members subdomain; the scraper
  still only matched the legacy ``authorlinks.php?no=<N>`` form, so
  every AFF download came back as "Unknown Author". Now matches
  both shapes and falls back to ``<div class="story-header-author">``
  for a third line of defence.

- **GreatFeet title strip.** Raw ``<title>`` carries embedded
  newlines + whitespace that broke the "strip the ``at
  greatfeet.com`` suffix" regex, leaving the full boilerplate in
  the story title. Collapse whitespace first, then strip — titles
  now come through as "Our Feet Need To Be Worshiped" instead of
  the two-line raw form.

- **SexStories search actually searches now.** Was scraping the
  homepage and filtering client-side by title substring; empty for
  anything but the most common queries. Uses
  ``/search/?search_story=<q>&page=<n>`` with tags appended to the
  query, the real full-text endpoint.

- **Chyoa search dedup.** Story and chapter URLs share the numeric
  id namespace, so keying on the number alone incorrectly dropped
  the second hit of a ``(story 14, chapter 14)`` pair. Key is now
  ``(kind, numeric)``.

### Change

- **Search fan-out routes through BaseScraper._fetch.** The
  simplified per-request curl_cffi session is gone; search now uses
  the same retry + 429/503 back-off + Cloudflare-block detection
  that downloads already benefit from. One module-scope scraper
  instance holds the AIMD delay state across the session so
  rate-limit bumps from one search leak through to the next rather
  than resetting every window open.

- **Dropped ``min_words`` from the Erotic Story Search form.** Most
  sites don't surface word counts in their listings, so rows came
  back as ``?`` and the filter silently passed them through. Kept
  the parser/filter internals for scripted callers who want to
  apply a threshold from the CLI.

- **GreatFeet title parser no longer relies on magic-string
  regex.** Inline marker ``<img>`` tags ("new!" / "hot!") are
  decomposed before reading link text, so alt-attribute strings
  like "Foot Fetish Offering" stop leaking into titles.

### Test

- **15 new end-to-end download tests.** Every erotica scraper now
  runs ``download()`` against a captured real HTML fixture, not
  just URL parsing. This is how the AFF and GreatFeet parse bugs
  above were discovered before shipping — previous tests only
  covered URL → id extraction and would have happily passed while
  every AFF download lost its author.

  Story + chapter fixtures live at ``tests/fixtures/erotica/``.
  Individual tests assert real titles/authors/tags and verify the
  chapter body comes through non-empty, plus a shared parametrised
  test for the ``skip_chapters = num_chapters`` case that
  ``--update`` passes.

## 1.23.0 — 2026-04-20

### Add

- **Pick Multiple button in erotica search.** Replaces Load More
  with the same tick-multiple-and-bulk-download flow an author-URL
  paste gets — the checklist dialog with sort/filter/select-all,
  opened pre-populated with every row from the current search.
  Tick ten stories, hit Download, they queue sequentially through
  the main frame's batch runner (same path bookmarks and author
  batches already use).

  Load More is hidden on the erotica frame because the fan-out
  already pulls from all 12 sites at once — paginating one site
  rarely buys more than ticking what you want from the first batch.
  Per-site search frames (FFN, AO3, Royal Road, Literotica,
  Wattpad) keep Load More and the single-row Download Selected
  unchanged.

  Series rows expand into their part list when fed to the picker so
  users can tick individual Literotica chapters even after the
  series collapse hides them behind a single parent row.

### Fix

- **Series collapse now applies to erotica results.** The fan-out
  was skipping ``collapse_literotica_series`` because the site key
  was ``"erotica"`` rather than ``"literotica"``, so numbered
  chapters of the same Literotica work were showing as separate
  rows ("Ch. 02", "Pt. 03", "- 4"). Literotica's collapse
  function already matches on URL shape and leaves non-Literotica
  rows untouched, so running it over the merged batch is the
  right fix — other sites' rows pass through unchanged.

## 1.22.1 — 2026-04-20

### Fix

- **Erotica search with tags but no keyword now works.** The
  SearchFrame's empty-query guard accepted Royal Road / Literotica
  filter-only browses but blocked the erotica fan-out when only a
  tag or site choice was set, responding with "Please enter a
  search query." even though every back-end erotica function handles
  empty queries fine (they treat them as "browse the tag"). Added an
  ``erotica_filter_only`` branch alongside the RR/Lit ones.

- **GreatFeet search results now show real story titles.** The
  first pass just labeled every row ``GreatFeet story <N>`` because
  the listing-page regex didn't reach the title text — GreatFeet's
  1997-era HTML has unclosed ``<a>`` tags that browsers tolerate but
  a naive regex skips over. BeautifulSoup handles the bad markup,
  the link text pulls through as the title, and the marker-image
  "Foot Fetish Offering" alt-text gets stripped so titles read
  cleanly.

## 1.22.0 — 2026-04-20

### Add

- **Four more erotica-site scrapers.** TGStorytime (TG parity with
  Fictionmania — same "two large archives" footing gay erotica gets
  from Nifty + Literotica), Chyoa (interactive CYOA fetish fiction,
  single-chapter mode), Dark Wanderer (dedicated cuckold XenForo
  community — thread starter's posts become chapters), and
  GreatFeet (dedicated foot-fetish archive running since 1997).
  Twelve erotica sites total now cover every common kink with at
  least one, and most (feet, femdom, cuckold, TG) with 4+ cross-
  referenceable archives.

- **Tag-picker coverage annotation.** Each tag in the Erotic Story
  Search multi-picker now shows how many sites carry it —
  e.g. ``femdom [5 sites]``, ``chastity [3 sites]``, ``cuckold
  [5 sites]`` — so users can tell well-covered kinks from niche
  ones before running a search. The ``[N sites]`` suffix is
  stripped before the tag hits the scrapers, so behaviour is
  unchanged; it's purely a UX hint in the picker.

### Change

- **Site column added to search results.** Every row now shows
  which archive it came from, in a "Site" column second from the
  left. Per-site search windows (FFN, AO3, Royal Road, Literotica,
  Wattpad) populate it with their own site key; the unified Erotic
  Story Search fills it with the originating archive (literotica,
  mcstories, darkwanderer, …). The "Site" column sits second so it
  stays visible even in narrow windows.

- **Per-site stats in search log.** Every erotica search now logs
  one line per archive with the count it contributed:
  ``sites — literotica: 8, mcstories: 3·exhausted, darkwanderer:
  0·exhausted, …``. Failed archives land in a separate line:
  ``failures — sofurry: FAIL (timeout)``. Previously a silently
  failed or empty-returning site was indistinguishable from "no
  matches at that site"; now it's explicit.

- **Load More respects per-site exhaustion.** When a site returns
  fewer than a full batch (or fails outright), it's marked exhausted
  and skipped on subsequent Load More clicks. Previously every Load
  More re-polled every archive, returning the same rows from sites
  that had already given their full tail.

- **Tag coverage table** (``TAG_SITE_COVERAGE``) maps every tag in
  the unified vocabulary to its supporting sites. Covered by tests
  so a future registration that references a dropped site trips
  CI instead of silently breaking the tag picker.

### Dropped candidates (this round)

- SoFurry, DailyDiapers — Laravel/Apache SPA responses require a
  JavaScript runtime we don't have. Deferred.
- BigCloset TopShelf — Cloudflare Challenge (not Managed Challenge)
  blocks anonymous scrapes; same root cause as Kristen Archives.

## 1.21.0 — 2026-04-20

### Add

- **Erotica subpackage with seven new site scrapers.** Added
  ``ffn_dl/erotica/`` grouping every erotica-focused scraper under
  one visible bucket, and populated it with scrapers for
  Adult-FanFiction.org (AFF), StoriesOnline (SOL), Nifty, SexStories,
  MCStories, Lushstories, and Fictionmania. Literotica moved into the
  same subpackage so the erotica surface is one file tree — the rest
  of the codebase now imports ``from .erotica import LiteroticaScraper``
  instead of ``from .literotica``. Existing general-purpose scrapers
  (AO3, FFN, FicWad, Royal Road, MediaMiner, Wattpad) are unchanged.
  All eight erotica sites are wired into URL auto-detection,
  ``canonical_url`` (with AFF/Fictionmania query-string id handling
  so library dedup picks up every variant), clipboard URL extraction,
  and the CLI download path.

- **Unified "Erotic Story Search" window (Ctrl+6).** A single
  SearchFrame that fans out across all eight erotica sites in
  parallel, merges results, and tags each row with its origin site.
  The site dropdown narrows to one archive when the user wants
  site-specific browsing. **Tags are the primary input** (multi-
  picker dialog, first focus position after the query box) — a
  direct answer to the long-standing gripe that the Literotica-only
  search buried tag entry. The unified tag vocabulary covers feet,
  femdom, spanking, cuckold, MC, humiliation, transgender, and the
  other cross-site common kinks; site-specific vocabularies
  (MCStories two-letter codes, Lush category slugs, SOL colon-joined
  tag URLs) are translated automatically.

### Change

- **BaseScraper now defines default ``is_author_url`` /
  ``is_series_url`` static methods** returning False, so the seven
  new scrapers (most of which have no author/series concept) don't
  need to carry stub implementations. AO3, FFN, Literotica, SOL
  still override with real checks.

### Dropped candidates

- ASSTR (domain offline — replaced with AFF as the "general tagged
  adult fanfic" slot).
- Kristen Archives (JS fingerprint gate requires a browser runtime).
- BDSM Library (connection timed out on every probe).
- BigCloset TopShelf, Dark Wanderer (structurally different — Drupal
  and XenForo — and left for a follow-up release).

## 1.20.18 — 2026-04-19

### Add

- **Per-probe progress during library update scans.** Phase 2 of
  ``_run_update_queue`` ran silently — after "Probing N stories for
  new chapters..." the user saw nothing until every probe finished
  and Phase 3 began. For a library with hundreds of FFN stories
  (FFN probes are serial at a 6 s floor, so 700 stories ≈ 70
  minutes) that silence was indistinguishable from a hang. Probes
  now emit one status line per completed probe —
  ``  [probe 42/804] <filename>: 23 chapter(s) upstream`` on
  success, ``probe failed: <reason>`` on error — and each site
  group announces itself with a header that calls out the
  per-site concurrency so the FFN serial cost is explicit.
  Behaviour and timing of the probes themselves is unchanged; the
  new output is visibility-only.

## 1.20.17 — 2026-04-19

### Change

- **HTML chapter counting is ~40× faster.** After 1.20.16 shipped,
  profiling Matt's real 806-file library revealed that the cache
  helped on warm runs but the cold first run still paid ~350 ms per
  file for BeautifulSoup to parse each HTML export. Since ffn-dl
  generates that markup itself, BS4 is overkill — a straightforward
  regex over the file's text with a class-list tokenisation check
  (so ``chapter-title`` doesn't match) returns the identical count
  in 8 ms on a 1.5 MB fic. For a 800-story FFN library that's the
  difference between a ~5-minute Phase 1 walk and ~8 seconds. The
  EPUB branch still goes through ``ebooklib`` (zip traversal is what
  that library is for); only the HTML path changed.

## 1.20.16 — 2026-04-19

### Change

- **Skip the ebooklib re-parse for unchanged library files.** Phase 1
  of an update-library run used to call `count_chapters()` on every
  indexed file, which for EPUBs meant a full `ebooklib.read_epub()`
  zip parse per file — tens of seconds over a few-thousand-book
  library even when nothing had changed on disk. The library index
  now records each file's `file_mtime` and `file_size` at scan time,
  and `build_refresh_queue` trusts the cached `chapter_count` when
  both match the live file. Any edit bumps mtime or size and forces
  a fresh read, so staleness is impossible. Older indexes written
  before this change naturally fall through to the slow path until
  their next scan populates the cache fields.

## 1.20.15 — 2026-04-19

### Add

- **TTL skip for `--update-library` and GUI Check for Updates.** Big
  libraries were slow to re-check because nothing was tracking which
  stories had already been probed recently. Each index entry now
  carries a `last_probed` timestamp, stamped after every successful
  probe pass (both CLI and GUI paths), and `build_refresh_queue`
  skips stories whose stamp is inside a caller-specified window.
  - CLI: new `--recheck-interval SECONDS` flag on `--update-library`
    (default `0` — probe everything, preserving prior behaviour), plus
    `--force-recheck` as an explicit "ignore the TTL this run" switch.
  - GUI: Check for Updates now defaults to a 1-hour TTL so a second
    click five minutes later is near-instant. A new "Force Full
    Recheck" button sits next to it for users who want every story
    reprobed regardless of when it was last checked.
- **`LibraryIndex.record` now preserves `last_probed` across rescan.**
  Without this, the post-update rescan after `--update-library`
  would wipe the stamp we'd just written and defeat the TTL on the
  very next run. `duplicate_relpaths` is preserved by the same merge
  logic.

## 1.20.14 — 2026-04-19

### Add

- **Watchlist tab in Preferences.** The autopoll toggle and poll
  interval — which 1.20.12 wired up for background polling — now
  have a proper home in the Preferences dialog (Edit → Preferences →
  Watchlist). The interval dropdown offers eight presets from 15
  minutes to 24 hours; apply_preferences reconfigures the running
  poll thread in place, so changing the interval or flipping
  autopoll takes effect immediately without an app restart. The
  runtime 5-minute safety floor enforced by `WatchlistPoller`
  stays untouched — every preset here is already above it, but
  the floor remains for anyone who hand-edits `settings.ini`.

## 1.20.13 — 2026-04-19

### Add

- **Watchlist manager window (Watchlist → Manage watchlist, Ctrl+W).**
  The watchlist has been CLI-only since 1.20.0 — add/list/remove/run
  all lived behind `--watchlist-*` flags. It now has a proper
  wxPython manager (`ffn_dl/gui_watchlist.py`) with a list view
  showing every watch's type, site, target, last-checked timestamp,
  and current status, plus buttons for Add Story URL, Add Author
  URL, Add Search, Remove, Pause/Resume, Run Now (all enabled
  watches), and Run Selected (polls just the highlighted row).
  Keyboard shortcuts: Delete removes the highlighted watch, F5
  refreshes the view, Ctrl+R runs all. Accessibility: every control
  has a descriptive `SetName` so NVDA announces a useful label, and
  the display-only list lets the screen reader read each column
  verbatim without the `[x] ` prefix trick the checkable dialogs
  need.
- **`watchlist.run_once` gained a `watch_ids` keyword argument.**
  When supplied as a set of watch ids, the runner restricts polling
  to those watches; when omitted it behaves exactly as before.
  "Run Selected" uses this to poll a single watch without bypassing
  the cooldown and notification machinery the rest of the system
  relies on.

## 1.20.12 — 2026-04-19

### Add

- **Background watchlist polling while the GUI is running.** The
  `KEY_WATCH_AUTOPOLL` and `KEY_WATCH_POLL_INTERVAL_S` prefs have
  existed since 1.20.0 but nothing in the GUI read them — autopoll
  required running `--watchlist-run` from cron or Task Scheduler.
  A new `WatchlistPoller` (`ffn_dl/watchlist_poller.py`) now spins
  up a daemon thread on launch when autopoll is enabled, reusing
  the same `watchlist.run_once` entry point the CLI flag uses. The
  interval is clamped at startup to `watchlist.MIN_POLL_INTERVAL_S`
  (5 minutes) so a corrupt config can't hammer sites, and results
  route through the root logger so they land in both the GUI status
  pane and the rotating file log. The thread is a daemon and
  `stop()` is non-blocking, so closing the app never hangs waiting
  for a sleep to wake up.
- **Preferences mutation is now live for the poll thread.** The
  Preferences dialog's OK handler (via `apply_preferences`) calls
  `WatchlistPoller.reconfigure()`, which reads the current autopoll
  and interval values and starts/stops/retargets the thread without
  requiring a restart. The next Preferences release (1.20.13) will
  expose the toggle itself in a new Watchlist tab — this release
  just plumbs the thread so that tab has something to talk to.

## 1.20.11 — 2026-04-19

### Change

- **Split gui.py into three modules.** gui.py had grown to 3189 lines
  covering the main window, the per-site search windows, four
  stand-alone dialogs, and their helpers. The search surface
  (`SearchFrame` plus the five site search-spec factories and the
  shared `_SEARCH_COLUMNS` constant) moved to
  `ffn_dl/gui_search.py`, and the four leaf dialogs
  (`VoicePreviewDialog`, `StoryPickerDialog`, `MultiPickerDialog`,
  `SeriesPartsDialog`) moved to `ffn_dl/gui_dialogs.py`. gui.py is
  now 1818 lines, holding just `MainFrame`, the log-bridge handler,
  and the `main()` entry point. Pure mechanical move — no behaviour
  changes, test suite unchanged.

## 1.20.10 — 2026-04-19

### Add

- **Unified Preferences dialog (Edit → Preferences, Ctrl+,).** Settings
  were scattered across the main form (format, filename template,
  output dir, HR-as-stars, strip notes, speech rate, attribution
  backend/size), the View menu (log level, save log to file), and
  the File menu (warn-before-closing). Several keys
  (`check_updates`, Pushover/Discord/email notification credentials)
  had no GUI at all and required editing `settings.ini` by hand.
  All of those are now in one tabbed dialog with five sections:
  General, Downloads, Audiobook, Notifications, Logging. Changes
  apply immediately — the main form's controls get re-synced on OK
  rather than waiting for a restart, and logging reconfigures in
  place. The Notifications tab unlocks the watchlist alerting
  credentials that were previously CLI-only.

## 1.20.9 — 2026-04-19

### Add

- **Close-during-download confirmation.** Closing the main window
  while a job was still running silently cancelled it — more than
  one user lost a half-built audiobook that way after walking away
  from the machine. ffn-dl now prompts before closing while a
  download, audiobook build, voice preview, or search is active,
  with wording tailored to which one is running (audiobooks call out
  that synthesised audio so far will be discarded; downloads note
  that cached chapters are kept). The prompt defaults to "Keep
  running" so muscle-memory Enter/Escape favours the safe path, and
  a "Don't ask again" checkbox turns the confirmation off for users
  who don't want it. The preference is also toggleable from File →
  "Warn before closing during downloads" so it can be re-enabled
  without hunting through settings.

## 1.20.8 — 2026-04-19

### Fix

- **Debug logs are readable again.** Before today, picking DEBUG in the
  log-level menu produced a file that was 90%+ third-party noise —
  HuggingFace `filelock` poll-spam while BookNLP waited on a model
  cache lock (one line every 50 ms, thousands per run), `httpcore`
  request traces from the same fetch, `asyncio` proactor chatter, and
  `h5py._conv` init lines. Our own `ffn_dl.*` debug output was
  drowning. `gui._apply_logging_config` now caps the known-noisy
  third-party loggers at INFO even when the root is at DEBUG, so a
  DEBUG log contains just ffn-dl's own diagnostics plus genuine
  third-party warnings/errors.
- **ffmpeg concat failures log the real error, not `b'...'`.** On
  some frozen Windows builds `subprocess.run(..., text=True)` hands
  back `bytes` instead of `str`, and `"%s" % bytes` renders as
  `b'...'` — which in one observed audiobook run truncated the
  stderr tail to 264 chars of the ffmpeg banner and hid the actual
  "please specify the format manually" message behind it. Both the
  per-chapter concat warning and `_run_ffmpeg`'s RuntimeError now
  route stderr through `_decode_stderr` so the real message survives.
  When a chapter concat fails and the logger is at DEBUG, the concat
  list is dumped too — a silent 29-chapter concat failure in the
  user's last audiobook run was untraceable because there was no way
  to see which input file the demuxer choked on.
- **TTS "No audio was received" no longer burns all three retries
  with identical parameters.** Edge-tts reproducibly rejects some
  text+voice+emotion combos, so plain retries of the same payload
  just waste budget. `_generate_with_semaphore` now uses progressive
  fallback: attempt 1 is full kwargs (assigned voice + emotion
  prosody), attempt 2 strips emotion prosody (rate/pitch/volume are
  the usual culprits), attempt 3 swaps to the narrator voice. A
  listener hears the line in the wrong voice rather than a silent
  gap — which is the tradeoff users prefer when edge-tts goes
  sideways.

## 1.20.7 — 2026-04-19

### Performance

- **`--update-all` / `--update-library` probes are dramatically faster.**
  The probe loop used to build a fresh scraper (and a fresh curl_cffi
  session) per story, paying a TLS handshake on every request and
  ignoring each site's own safe-concurrency cap. The loop now
  partitions the queue by site, builds one scraper per site that's
  shared across all of its probes, and runs each site group in its
  own pool sized to the site's `concurrency` — so FFN stays at 1
  worker (it captcha-bans on parallel bulk fetching regardless of
  `--probe-workers`) while RoyalRoad / FicWad / MediaMiner run their
  safe 3-wide pool. Connection reuse on HTTPS requests after the
  first on a given site drops roughly 300–600 ms per probe. On a
  mixed 100-story library the wall-clock improvement is in the
  minutes.
- **FFN chapter-count probes skip the full HTML parse.** FFN's
  `get_chapter_count` fetches chapter 1 and used to build a full lxml
  tree just to count `<option>` tags in the `chap_select` dropdown.
  A compiled regex over the response text gets the identical number
  in microseconds, and falls through to the bs4 path unchanged when
  the dropdown is absent (single-chapter works) or FFN changes the
  markup.

### Internal

- `BaseScraper` now carries a thread-local curl_cffi session so a
  single scraper instance can be reused safely across a worker pool.
  `_rotate_browser` rotates only the calling thread's session;
  `_bump_delay_up` / `_delay` hold a lock around the shared AIMD
  counter so concurrent workers see consistent delay state.

## 1.20.6 — 2026-04-19

### Fix

- **Self-update failures now land in `ffn-dl.log`.** The background
  update check and the manual *Check for Updates* menu both caught
  exceptions from `self_update.check_for_update()` and wrote the
  message only to the GUI output panel — so once the window closed,
  the curl/TLS/rate-limit error was gone. Both handlers now also
  call `logger.warning(..., exc_info=True)` so the traceback survives
  in the rotating file log and the failure is diagnosable after the
  fact.

## 1.20.5 — 2026-04-19

### Fix

- **Library dialog's Close button and window X now actually close
  the dialog.** LibraryDialog is opened modally via ShowModal() but
  its EVT_CLOSE handler only called event.Skip(), which lets wx
  destroy the widgets without ending the modal loop — ShowModal()
  stayed blocked and the dialog appeared stuck on screen. The
  handler now calls EndModal(wx.ID_CLOSE) when the dialog is modal,
  matching the already-correct pattern in ReviewDialog right below
  it. Covers Close button, the window X, and the Escape key.

## 1.20.4 — 2026-04-19

### Fix

- **GUI Check for Updates no longer crashes with "Update failed:
  'Namespace' object has no attribute 'name'".** The shared
  ``_download_one`` code path reads ``args.name`` (filename template),
  ``args.hr_as_stars``, ``args.strip_notes`` and ``args.clean_cache``
  unconditionally. ``library.refresh.default_refresh_args`` — used by
  the GUI's library update button — didn't set any of them, so the
  first story with new chapters raised AttributeError which the GUI
  caught and surfaced as an opaque "Update failed" message. All four
  are now populated, with ``name`` / ``hr_as_stars`` / ``strip_notes``
  pulled from Prefs so the GUI update honours the user's configured
  filename template and export flags the same way the CLI does.
  Audio-branch and kindle-send attributes are set too for defence in
  depth. A regression test asserts every attribute ``_download_one``
  reads is present on the Namespace.

## 1.20.3 — 2026-04-19

### Add

- **Duplicate detection.** When the library scanner sees two files
  with the same canonical source URL, it keeps one primary entry and
  records the other path(s) in a new ``duplicate_relpaths`` list on
  the entry — rather than silently overwriting the first relpath as
  1.20.x did. ``--scan-library`` prints the duplicate count and up to
  20 primary↔duplicate pairs so the user can review and delete the
  copy they didn't mean to keep. Validated against a real 817-file
  library: surfaced 10 duplicate pairs that had been invisible.

### Fix

- **URL canonicalisation.** Before a story URL is used as an index
  key, ``sites.canonical_url`` collapses the per-site variants that
  different downloaders emit:
  - FFN: ``/s/N`` / ``/s/N/`` / ``/s/N/1/`` / ``/s/N/1/Title-Slug``
    → ``https://www.fanfiction.net/s/N``
  - AO3: ``http://`` / ``https://`` / ``ao3.org`` / ``archiveofourown.org``
    → ``https://archiveofourown.org/works/N``
  - Royal Road, FicWad, MediaMiner, Literotica, Wattpad all get a
    matching per-site canonical form.
  - Unsupported hosts are still scheme + trailing-slash normalised so
    hand-typed URL variants can't silently duplicate.
  Without this, a library with ``/s/9215532`` and ``/s/9215532/1/``
  embedded in two different exports of the same story ended up with
  two distinct index entries. Now they collapse to one, and the
  second copy is correctly flagged as a duplicate.
- **Existing indexes migrate on load.** ``LibraryIndex.load`` rewrites
  any non-canonical keys from an older index and merges colliding
  entries into ``primary + duplicate_relpaths``, preferring whichever
  entry has more populated metadata as the primary. No re-scan is
  required to benefit from the URL collapse on an existing library.

## 1.20.2 — 2026-04-19

### Fix

- **`--update-library` no longer silently skips third-party HTML
  exports as "chapter count unknown".** 1.20.1 recovered the chapter
  count from FicLab's structured ``<th>chapters</th>`` row but the
  other formats embed it in prose: bold-br dumps say
  ``Content: Chapter X to Y of N chapters``, AO3's native HTML export
  uses ``Chapters: 43/?`` inside a ``Stats:`` block, and FLAG expresses
  it through its ``<a href="#chapter_N">`` TOC anchors. None of those
  landed in ``FileMetadata.chapter_count``, so the index stored 0,
  ``count_chapters`` (which only understands ffn-dl's own markup)
  also returned 0, and ``library/refresh.py`` skipped every such
  story. Three new body-level regex fallbacks in ``_fill_from_html``
  cover all three patterns; full-library accuracy on a real 817-file
  library goes from 94.0% to 100.0%.
  - **Action required for existing libraries:** re-run
    ``--scan-library DIR`` once on 1.20.2 so the index picks up the
    newly-derived chapter counts. ``--update-library DIR`` will then
    stop skipping those stories.

## 1.20.1 — 2026-04-19

### Fix

- **Library metadata extraction is now effectively complete for
  third-party HTML formats.** Smoke-tested against a real 817-file
  library: title 100%, author 100%, fandoms 99.8%, source_url 99.8%
  — up from 99.3% / 99.3% / 85.6% / 99.7% in 1.20.0.
  - New format parsers: FLAG / flagfic.com
    (``<span id="crAuthor">`` + ``<h1>Title by Author</h1>``), the
    ``<span class="title">`` / ``<span class="author">`` variant, and
    AO3's native HTML download (title recovered from the
    ``<title>Title - Author - Fandom</title>`` convention when no
    kv-table is present).
  - Universal fallbacks: when none of the format-specific parsers
    produced a title, try ``<meta property="og:title">``, the first
    ``<h1>``, and finally the ``<title>`` tag — with site-branding
    suffixes (``"Story | FanFiction"``) stripped and a generic-heading
    blocklist (``Copyright``, ``Summary``, …) applied so cover-page
    boilerplate isn't mistaken for a title.
  - ``<meta name="author">`` is used as a last-resort author source.
  - ``_split_title_by_author`` handles the ``"Story by Author"``
    pattern that shows up in ``<title>`` tags (HPFFA and others) —
    splits on the final ``" by "`` and assigns both fields, with a
    length/punctuation guard so titles like ``"Life by the Seaside"``
    don't lose their tail.
- **Crossover fandoms are now recovered from FicLab tag rows.** FFN's
  crossover convention emits a single tag of the form
  ``"{FandomA} + {FandomB} Crossover"``. For fics in a crossover
  bucket (e.g. ``misc/`` in a library organised by fandom), this is
  the only fandom signal available — the folder name isn't a fandom,
  the tags row is the whole story. Scan accuracy on the crossover
  subset went from 0% to 26/28.
- **Library scanner now back-fills fandom from the parent folder.**
  `library.identifier.identify(path, metadata, root=...)` uses the
  file's immediate subfolder under the scan root as a fandom when the
  HTML metadata didn't include one — the right fallback for libraries
  already organised by fandom folder. Catch-all folder names (``misc``,
  ``unsorted``, ``downloads``, …) are excluded so they can't pollute
  the index.

## 1.20.0 — 2026-04-19

### Add

- **Watchlist with notifications.** Subscribe to stories, authors, or
  saved searches and receive Pushover, Discord, or email alerts when
  they change. Three watch types share one polling loop:
  - **Story watches** — new chapter alerts via each scraper's cheap
    `get_chapter_count()` probe.
  - **Author watches** — new-work alerts via `scrape_author_works()`
    on any of the 7 supported sites.
  - **Search watches** — new-match alerts on a saved query for FFN,
    AO3, Royal Road, Literotica, or Wattpad.
  - New CLI flags: `--watchlist-add URL`, `--watchlist-add-search
    SITE QUERY`, `--watchlist-list`, `--watchlist-remove ID`,
    `--watchlist-run`, `--watchlist-test CHANNEL`. `--watchlist-run`
    is cron / Windows-Task-Scheduler friendly — one poll pass, exits.
  - Storage is a JSON file at `<portable_root>/watchlist.json` so
    entries survive auto-updates. Atomic writes + corrupt-file
    quarantine keep the file from wedging the app.
  - Per-watch cooldown prevents a transient scraper flake from
    spamming duplicate alerts.
  - GUI tab will land in a follow-up 1.20.x release; the CLI is the
    first cut so scheduled polling works on headless setups today.

### Fix

- **Library scan now reads metadata from every common HTML format.**
  The previous scanner only understood ffn-dl's own `<th>Title</th>
  <td>…</td>` kv-table, so ~99% of a library populated by FanFicFare
  / FicLab / raw browser downloads / AO3's native HTML export ended
  up indexed with null title, author, rating, status, fandom, and
  chapter_count — the adapter and source URL were detected but every
  other field was empty, which made `--reorganize` and the library
  GUI nearly useless. Smoke-tested against a real 817-file library:
  the title/author hit-rate went from 3/817 to 809/817 (99.0%),
  fandom from 0/817 to 699/817 (86%), chapter_count from 0/817 to
  768/817 (94%).
  - `_parse_kv_table` now returns lowercase-normalised keys, so
    FicLab's `<th>title</th>` resolves the same as ffn-dl's
    `<th>Title</th>`.
  - `_parse_kv_table` also understands `<dt>Label:</dt><dd>Value</dd>`
    (AO3's native HTML export) and `<tr><td>…</td><td>…</td></tr>`
    (EPUB title-page variant).
  - New `_parse_paragraph_labels` reads `<p>Label: value</p>` and
    `<b>Label:</b> value<br/>` paragraph dumps. Restricted to a
    known label set so chapter-body dialogue tags (`<p>Harry: …</p>`)
    aren't mistaken for metadata.
  - Chapter count is now pulled from the structured metadata when
    available, so `count_chapters()` (which only understands ffn-dl's
    own `<div class="chapter">` markup) no longer overwrites a
    correct number with 0 on third-party files.

## 1.19.2 — 2026-04-19

### Fix

- **Bulk-update commands now pre-flight format dependencies.**
  `--update-all` and `--update-library` previously skipped the
  `check_format_deps` guard that every other download entry point
  calls, so a user missing `ebooklib` or `edge_tts` would have every
  story in a library downloaded and then fail at export time. Both
  handlers now abort up front with a clear "Missing dependency"
  message, matching `_download_one`, `_handle_merge_series`, and
  `_handle_merge_parts`.
- **Surfaced silent failures in the package bootstrap.** The
  `portable.setup_env()` and `neural_env.activate()` calls in
  `ffn_dl/__init__.py` were wrapped in bare `except Exception: pass`,
  so if the portable-root redirect failed, user data would quietly
  land in `~/.ffn-dl` instead of the exe folder with no indication
  anything had gone wrong. Both catches now call `logger.exception`
  so the traceback reaches the log file when either bootstrap step
  fails, without blocking the import.

## 1.19.1 — 2026-04-19

### Change

- **Code-quality sweep.** Internal-only refactor following a codebase
  audit. No user-visible behaviour changes beyond the menu item below.
  - Centralised site / URL detection in a new `ffn_dl.sites` module so
    the CLI, clipboard watcher, GUI, and updater all share one
    registry of supported URL patterns.
  - Split `cli.main()` (previously 800+ lines) into `_build_parser`
    plus focused dispatch helpers (`_handle_update_file`,
    `_collect_urls`, `_expand_author_and_series_urls`, `_run_batch`,
    `_handle_install_attribution`).
  - Split `_handle_search` into `_build_search_spec`, `_collapse_results`,
    `_print_search_results`, `_prompt_search_choice`, and
    `_download_picked_result`.
  - Named all scraper retry / backoff magic numbers
    (`INITIAL_BACKOFF_S`, `MAX_BACKOFF_S`, `AIMD_DECAY_FACTOR`, etc.)
    and added real docstrings to `_fetch` and `_fetch_parallel`.
  - Replaced the worst bare `except Exception` swallows with narrower
    types plus `logger.debug` so failures are debuggable from the log.
  - Added type hints to CLI and scraper public APIs.

### Add

- **Help → Read the Manual.** New GUI menu item (F1) that opens the
  project README in the default browser.

## 1.19.0 — 2026-04-19

### Add

- **Library manager.** Scan a directory full of fanfiction files from
  any downloader (ffn-dl's own exports, FanFicFare, FicHub, bare HTML
  or text with an embedded URL) and index what's there, keep it sorted
  by fandom via a configurable path template, check every tracked
  story for new chapters upstream, and route new downloads into the
  right category folder automatically.
  - `--scan-library DIR` — walk, identify, and record every story
    file. Uses structured metadata when the originating tool left it
    behind; falls back to a URL-in-content regex. Symlinks are
    skipped so self-referential ones can't loop forever.
  - `--reorganize DIR [--apply]` — plan the moves that would bring
    a library into alignment with the path template (default
    `{fandom}/{title} - {author}.{ext}`). Dry-run by default; empty
    source directories get cleaned up after an apply.
  - `--update-library DIR` — index-driven refresh. For each tracked
    story, probe the source for new chapters and download any
    updates in place, preserving the original file's format and
    location. Works across ffn-dl, FanFicFare, and FicHub files.
  - `--review-library DIR` — interactive CLI for promoting
    untrackable files (title + author but no embedded URL). Paste a
    source URL per file and the entry moves into the tracked list.
  - **Auto-sort on download.** When a library path is configured in
    prefs and the user hasn't passed `--output`, new downloads land
    at `<library>/<fandom>/<filename>`. Multi-fandom crossovers and
    files with no fandom tag route to a Misc folder. Explicit
    `--output` always wins.
  - **GUI.** A new `&Library` menu (`Ctrl+L`) opens a hub dialog
    with a dir picker, path template, misc folder, and buttons for
    Scan, Reorganize, Check for Updates, and Review Ambiguous. The
    reorganize preview uses per-row checkboxes; the review dialog
    walks untrackable files one at a time for URL entry. All
    screen-reader-friendly (`[x]`/`[ ] ` label prefixes on
    `wx.CheckListBox` since MSAA state reporting is unreliable).

### Fix

- **Path-template hardening.** `..`/`.` segments are dropped so a
  hostile template or malformed metadata value can't escape the
  library root. Every segment is capped at 200 chars, preserving
  extensions. Windows reserved device names (CON, PRN, AUX, NUL,
  COM1-9, LPT1-9) get an underscore prefix when they appear as a
  segment's base name.
- **FanFicFare metadata reading.** Relationship tags like
  `Harry/Hermione` no longer leak into the fandom list and misroute
  fics to Misc — the `/` discriminator separates them from real
  fandom names.
- **Malformed EPUB visibility.** A file `ebooklib` can't read now
  emits a logger warning instead of being silently indexed with no
  metadata.
- **Prefs without wxPython.** CLI-only installs no longer error at
  `Prefs()` construction; the wx-backed config object is optional
  and the class falls back to returning defaults when unavailable.

## 1.18.1 — 2026-04-18

### Add

- **StoryPickerDialog remembers your sort choice.** The "Sort by"
  dropdown in the author/bookmarks picker now persists across
  launches via a new `story_picker_sort` pref, so picking
  *Last updated (newest first)* once keeps it that way for every
  future picker. (Search-tab "Sort by" dropdowns were already
  persisted as part of per-site search state.)

## 1.18.0 — 2026-04-18

### Add

- **Backend-agnostic post-attribution refinement pass.** Runs after
  every attribution backend (builtin, BookNLP, fastcoref) and after
  cache loads, so rebuilding audio from an existing attribution
  cache picks up the new rules too. Two patterns are handled:
  - **Self-introductions.** `"I'm Ron, by the way, Ron Weasley."`,
    `"I am Hermione Granger."`, `"My name is Alastor Moody."`,
    `"Call me Tom."`, `"…, by the way, Bond, James Bond."` —
    when the current speaker is None, the first attributed speaker
    in the chapter, or carryforward from the previous attributed
    line, and the name inside the quote is a confirmed speaker
    elsewhere in the book (or a full First-Last pair), the segment
    is re-attributed to that name. BookNLP coref can't identify a
    character the first time they name themselves because it only
    links to entities it has already seen; this pass covers that
    gap without touching distinct explicit attributions.
  - **Junk-speaker demotion.** Single-capitalised-word speakers
    that only occur once in the entire book AND match a narrow
    fanfic common-noun blocklist (*Wizard*, *Dwarf*, *Veela*,
    *Cruciatus*, *Expulso*, *Disillusionment*, *Barrier*,
    *Scroll*, *Password*, *Ministry*, *Beauxbatons*, *Unknown*,
    …) are demoted back to narrator. On a real 44-chapter HP fic
    this takes BookNLP big's distinct-speaker count from 237 to
    211 — all drops are spells, species, places, or BookNLP PROP
    sentinels, none are real characters.

## 1.17.0 — 2026-04-18

### Fix

- **Builtin attribution: capitalised common words no longer become
  fake speakers.** Pre-action attribution treated the first proper-
  noun-shaped token in the action beat as the speaker, so words like
  *Halloween*, *Reluctantly*, *Behind*, *With*, *Magicals*, *Blimey*,
  *Merlin*, *Earth*, *Box*, *Trevor!*, *Thank*, *Breathe* — anything
  capitalised at sentence start — could win the speaker assignment.
  parse_segments now pre-scans the chapter for names confirmed via
  explicit speech-verb attribution and filters pre-action candidates
  against that set.
- **Builtin attribution: orphan-tail name no longer hijacks the next
  speaker.** When a previous post-attribution clause was consumed
  (`"…," Sirius breathed`) the leftover tail (`out while gently
  cradling Harry.`) became the only pre-text for the next dialogue,
  and pre-action picked *Harry* — the object being held, not the
  speaker. Pre-action is now skipped when the orphan tail starts with
  a lowercase word (= continuation of the previous sentence). The
  consecutive-quote carryforward also bails out on long orphan tails
  that mention another character (even in possessive form), so
  `"…," yelled loudly as he beat on the door of his cousin
  Andromeda's home. …` no longer keeps Sirius as the speaker for
  Andromeda's reply.
- **Builtin attribution: action verbs after dialogue now attribute
  when the name is confirmed elsewhere.** Lines like `"…," Sirius
  ran his hands over his face.` and `"…," Hermione motioned to the
  timid-looking boy.` were dropped because `ran`/`motioned` aren't
  speech verbs. A soft post-attribution path now accepts any verb in
  `Name verbed` form when *Name* is a confirmed speaker elsewhere
  in the chapter.
- **Builtin attribution: stray unbalanced quote no longer desyncs
  the rest of the chapter.** A single typo like `…to leave."` with
  no matching opener caused every subsequent dialogue/narration pair
  to invert for the rest of the chapter. parse_segments now runs a
  quote-balancing pre-pass that classifies each quote as opener or
  closer from its neighbors and drops orphans before pairing.
- **BookNLP / fastcoref fallback no longer poisons the attribution
  cache.** When the neural backend was uninstalled or raised mid-run
  the dispatcher silently returned the unrefined builtin segments,
  and the audiobook pipeline saved those under the requested
  backend's cache key — so the next render saw a "cache hit" and
  skipped the real refinement entirely. The pipeline now consults
  `attribution.has_failed(backend, size)` after each refine call and
  only persists when the backend actually ran. Existing polluted
  cache entries can be cleared by deleting
  `cache/attribution/v1/<backend>/<size>/`.

### Lists expanded

- `_SENTENCE_STARTERS` now includes common holiday names, sentence-
  start prepositions / connectives (*Behind*, *Beside*, *Without*,
  *Despite*, *Throughout*, …), fanfic-narration interjections
  (*Blimey*, *Merlin*, *Magic*, *Box*, …), and the contracted
  pronouns (*I'll*, *You're*, *We've*, …) that the proper-noun
  regex would otherwise pick up at line start.
- `_SPEECH_VERBS` adds `advised`, `counseled`, `encouraged`,
  `lectured`, `admonished`, `motioned` so the strict post-
  attribution path catches more standard tags before falling back
  to the soft confirmed-speaker check.

## 1.16.3 — 2026-04-18

### Fix

- **Chapter-audio cache write no longer crashes ffmpeg with "Invalid
  argument".** The 1.16.0 atomic-write scheme named the in-flight
  file `<hash>.mp3.tmp`, and ffmpeg picked its muxer from the final
  extension — `.tmp` is unknown, so every chapter audio render
  failed at the muxer-init step. The temp file is now
  `<hash>.tmp.mp3`, which still gives an atomic `os.replace` swap
  but keeps `.mp3` as the last extension so ffmpeg infers the
  format correctly.

## 1.16.2 — 2026-04-18

### Fix

- **Audiobook chapter assembly no longer fails on Windows temp paths.**
  The ffmpeg concat demuxer interprets escape sequences inside
  single-quoted path values, so a Windows temp path like
  `C:\Users\...\Temp\ffn-tts-xxxx\seg_000001.mp3` had its `\t` /
  `\n` / etc. silently reinterpreted and the segment files couldn't
  be found. Concat list entries now normalise to forward slashes
  and escape embedded quotes. The related warning now logs the
  tail of ffmpeg's stderr instead of the banner, so future concat
  regressions are self-diagnosing.

## 1.16.1 — 2026-04-18

### Fix

- **Windows release build no longer fails on a flaky ffmpeg mirror.**
  GitHub's "releases/download/latest" redirect occasionally serves a
  ~92-byte stub instead of the real archive, and the build step
  trusted whatever arrived. The v1.16.0 tag fell into exactly that
  hole and never produced an installer. The build now size-checks
  the download and retries up to five times before giving up, so
  transient hiccups don't swallow a release.

## 1.16.0 — 2026-04-18

### Add

- **Persistent chapter-audio cache.** Per-chapter TTS output now lives
  under `<portable_root>/cache/chapter_audio/`, content-addressed on
  (segments + voice assignments + narrator + rate). A failed M4B mux,
  a cover-download retry, or a re-render with a different cover no
  longer re-synthesises thousands of segments — only new or edited
  chapters are spoken again. Cache hit/miss counts are logged.
- **Spoken chapter headings now survive failed runs.** Headings are
  synthesised separately from the body and excluded from the cache
  key, so a one-off heading-TTS failure on an earlier run can no
  longer poison subsequent runs with a "Chapter N. Title"-less body.
  (This also addresses the user-visible "chapter names aren't being
  announced" regression after the mux fixes in 1.15.1 / 1.15.2.)

### Improve

- **Dialogue attribution handles adverb interposition.** Patterns like
  `"…," Harry finally said`, `"…," said Harry quietly`, and
  `Harry quietly said, "…"` now resolve to the named speaker instead
  of falling through to narrator voice. Adverbs are matched
  lowercase-only so proper nouns ending in `-ly` (Sally, Riley, Holly)
  aren't swallowed.
- **Pre-action attribution handles multi-name action beats.** When
  the narration before a quote mentions more than one character (e.g.
  `Harry grinned at Ron. "Let's go."`), the subject of the *last*
  sentence is used as the speaker instead of bailing to unattributed.

## 1.15.2 — 2026-04-18

### Fix

- **M4B mux no longer fails on odd-dimension cover art.** The ipod
  muxer was defaulting to libx264 for the cover stream, which rejects
  any image whose width or height is not divisible by 2 (common for
  small webp thumbnails like 75x100). Force `-c:v mjpeg` so the cover
  is stored as JPEG attached-picture instead of being re-encoded to
  h264.

## 1.15.1 — 2026-04-18

### Fix

- **M4B mux no longer fails when a cover image is present.** The
  `-map_metadata` flag was emitted before the cover `-i`, which ffmpeg
  rejects as an input option applied to an output file, aborting the
  whole audiobook build right after synthesis. All `-i` inputs now
  precede any output options.

## 1.15.0 — 2026-04-18

### Fix

- **Spoken chapter headings no longer mislabel structural chapters.**
  A chapter titled "Prologue", "Epilogue", "Interlude", "A/N", etc.
  (optionally with a subtitle, e.g. "Prologue: Before the Fall") is
  now read verbatim instead of being prefixed with "Chapter N." —
  "Chapter 1. Prologue" was wrong for every book that has a prologue.

## 1.14.0 — 2026-04-18

### Add

- **Spoken chapter titles.** Each chapter in the generated M4B now
  opens with a narrator-voiced "Chapter N. Title" heading followed by
  a short beat, so listeners hear where they are instead of only
  seeing the chapter marker. Titles that already start with "Chapter"
  are read verbatim; pure-number titles collapse to "Chapter N".
- **Audiobook attribution intro.** A one-line "Title, by Author.
  Downloaded from <site>." preamble is synthesised in the narrator
  voice and prepended to the M4B with its own "Introduction"
  chapter marker. Site names are mapped to TTS-friendly forms
  (Archive of Our Own, Royal Road, fanfiction dot net, etc.).

## 1.13.1 — 2026-04-18

### Fix

- **BookNLP attribution now logs progress markers around model
  construction and ``process()``.** The constructor's tail (loading
  ~1.2 GB of ``.model`` weights into PyTorch) and the inference pass
  itself were previously invisible in the file log, since BookNLP
  uses ``print()`` + ``tqdm`` and the GUI Windows build has no
  attached console. A run that took 5–15 min on Windows CPU looked
  identical to a real hang. We now emit ``BookNLP: constructing
  model``, ``model construction complete``, ``processing N chars``,
  and ``process() returned`` so the log shows where time is being
  spent and a true hang can be distinguished from slow inference.

## 1.13.0 — 2026-04-18

### Change

- **GUI uses a menu bar instead of tabs.** The main window is now just
  the Download surface (URL, format, save-to folder, audio options,
  status log). Each site's search lives in its own non-modal window,
  summoned from the **Search** menu or via Ctrl+1 (FFN), Ctrl+2 (AO3),
  Ctrl+3 (Royal Road), Ctrl+4 (Literotica), Ctrl+5 (Wattpad). Keep
  several search windows open at once — arrow through results in one
  while a download runs from another.
- **New menus:** File (Exit), Search (per-site), View (Log Level
  submenu, Save log to file toggle, Open log folder), Help
  (Check for Updates..., About ffn-dl). The log-level dropdown,
  save-to-file checkbox, and open-log-folder button moved out of the
  status row into the View menu.
- **Help → Check for Updates...** lets you trigger the update check
  on demand; unlike the silent launch check, it tells you when
  there's nothing new.

## 1.12.12 — 2026-04-18

### Fix

- **BookNLP model downloads no longer hang on a truncated file.**
  Upstream BookNLP fetches its model weights via
  ``urllib.request.urlretrieve`` with no timeout, no resume, no size
  verification, and no atomic rename — so a mid-download
  interruption leaves a short file at the target path that its
  ``is_file()`` guard then accepts as "complete", causing torch.load
  to fail (or, on a stalled socket, the process hangs indefinitely
  with no progress). We now pre-populate
  ``~/booknlp_models/`` ourselves with a size-verified, resumable
  downloader (HTTP ``Range`` requests into ``<file>.part``, atomic
  rename on a Content-Length match, 60s socket timeout, 3 retries).
  BookNLP's guard then sees complete files and skips its broken
  downloader entirely. Logs show per-file progress every ~50 MB.

## 1.12.11 — 2026-04-18

### Change

- **FFN downloads now use a steady 6s/chapter delay instead of
  bursting 20 chapters then pausing ~60s.** The old chunk-pause
  pattern matched what Cloudflare's bot-detection actually flags —
  fast bursts followed by long silences. FanFicFare's proven default
  (`slow_down_sleep_time: 6` in its defaults.ini for
  www.fanfiction.net, applied to every request) is what we now match:
  a uniform per-chapter pace. Downloads take roughly the same wall
  time but run continuously, and AIMD still doubles the delay up to
  60s if a 429/503 slips through. `--chunk-size N` still works for
  users who want the old behavior.

## 1.12.10 — 2026-04-18

### Fix

- **BookNLP install no longer logs a false "model could not be
  downloaded" warning.** On a first-ever neural backend install, the
  spaCy `en_core_web_sm` download into `DEPS_DIR` succeeded but
  `_ensure_spacy_model`'s post-download `find_spec` check returned
  `None`, so the install flow warned "BookNLP will fall back to
  builtin at run time" despite the model being present on disk. Root
  cause: `neural_env.activate()` runs once at package import and
  no-ops when `DEPS_DIR` doesn't exist yet. The first install creates
  `DEPS_DIR` *after* that no-op, so the main process's `sys.path`
  never picked it up. `install()` now re-activates after
  `pip_install` succeeds, and `_ensure_spacy_model` re-activates
  after a frozen-path download for good measure.

## 1.12.9 — 2026-04-18

### Fix

- **Auto-updater no longer loops on every launch.** The v1.12.3–v1.12.8
  releases shipped with ``ffn_dl/__init__.py`` still pinned to
  ``1.12.7``, so the installed build reported itself as 1.12.7 even
  after a successful update. The updater then saw the newer tag on
  GitHub, re-downloaded, re-extracted, relaunched, and immediately
  offered the same update again. Bumping ``__version__`` in lockstep
  with ``pyproject.toml`` fixes the compare.

## 1.12.8 — 2026-04-18

### Fix

- **BookNLP attribution no longer dies on smart-quoted fanfic.** Several
  BookNLP modules — most visibly ``english_booknlp.process()`` — open
  text files with bare ``open(filename)``. On Windows that defaults to
  cp1252 and chokes on UTF-8 right-double-quotes (``E2 80 9D``) with
  ``'charmap' codec can't decode byte 0x9d``.
  ``_patch_booknlp_text_encoding()`` shims ``open`` in every affected
  module to default to ``encoding="utf-8"`` for text reads.
- **Windows-path shim now runs on every platform** instead of only when
  ``sys.platform == "win32"``. ``os.path.basename`` on POSIX Python
  doesn't recognise ``\\`` as a separator, so the previous guard made
  the shim a silent no-op on any non-Windows host that received a
  Windows-style model path. Replaced ``_osp.basename`` with an
  OS-agnostic ``rsplit`` on both separators.

## 1.12.7 — 2026-04-18

### Change

- **HuggingFace downloads now live under the visible ``cache/`` folder**
  instead of the hidden ``.cache/huggingface/`` sibling that the
  ``HOME`` redirect used to create. ``portable.setup_env()`` sets
  ``HF_HOME=<root>/cache/huggingface`` and moves any pre-existing
  download on first run, so the ~300 MB BERT weights aren't
  re-fetched. Nothing changes for the user except that the portable
  folder is less confusing to browse.

## 1.12.6 — 2026-04-18

### Fix

- **BookNLP attribution now actually loads instead of silently falling
  back to the builtin.** BookNLP's three taggers (entity, coref,
  quote) were saved against an older ``transformers`` where
  ``BertEmbeddings`` registered ``position_ids`` as a buffer.
  Transformers 4.31+ removed that buffer, so
  ``model.load_state_dict(torch.load(...))`` hit
  ``Unexpected key(s) in state_dict: "bert.embeddings.position_ids"``
  and our dispatcher logged a backend failure then reverted to the
  builtin parser — exactly the bad-voicing case BookNLP is supposed
  to fix. We now install a per-module ``torch`` shim that strips any
  ``*.embeddings.position_ids`` keys from the dict returned by
  ``torch.load`` before ``load_state_dict`` sees it; the global
  ``torch.load`` is untouched, so nothing else is affected.

## 1.12.5 — 2026-04-18

### Fix

- **BookNLP now actually loads on Windows.** Three of BookNLP's tagger
  classes (entity, coref, quote) derive the HuggingFace base-model
  name from the on-disk model file via
  ``model_file.split("/")[-1]`` — on POSIX that strips the directory,
  on Windows paths use ``\`` so it returns the whole absolute path
  unchanged. ``transformers.from_pretrained`` then feeds e.g.
  ``C:\\ffdl\\booknlp_models\\entities_google/bert_uncased_...``
  straight into HuggingFace Hub's repo-id validator, which rejects it
  because repo ids can't contain ``:`` or ``\``. We install a small
  shim on each module's ``re`` binding that calls ``os.path.basename``
  before the ``google_bert``-replacing ``re.sub`` runs. Upstream
  BookNLP bug; the workaround is localized to the three taggers and
  leaves all other regex calls untouched.

## 1.12.4 — 2026-04-18

### Fix

- **Embedded Python subprocesses can now actually import the packages
  we pip-installed into `neural/deps/`.** The `--target` pip flag puts
  files in the right place, and `neural_env.run_python` set
  `PYTHONPATH=<DEPS_DIR>` to make them importable — but the embeddable
  Python ships with a `._pth` file, and per the documented embed
  contract, `._pth` disables `PYTHONPATH` entirely. So every call to
  `python -m spacy …` through the embedded Python died with
  `No module named spacy`, which the v1.12.3 logger-routing change
  finally surfaced. The fix writes the absolute `DEPS_DIR` path into
  the `._pth` file next to the interpreter (inserted before
  `import site` so additions are visible when site.py runs). The edit
  is idempotent and re-applied on every `ensure_embed_python` call, so
  installs bootstrapped against older versions self-heal on the next
  render. Together with the `--target` fix from v1.12.3, BookNLP's
  one-shot spaCy model download now actually lands somewhere the main
  .exe can import from.

## 1.12.3 — 2026-04-18

### Fix

- **BookNLP attribution no longer silently falls back to builtin on
  every render.** When the frozen app auto-downloaded the missing
  `en_core_web_sm` spaCy model, spaCy's `download` subcommand shells
  out to `pip install <wheel>` with no `--target`, so the model landed
  in the embeddable Python's own `Lib/site-packages` — which isn't on
  the main .exe's `sys.path`. Every runtime check kept failing the
  availability test and BookNLP degraded to the builtin parser. The
  download now forwards `--target <neural/deps>` so the model installs
  where `site.addsitedir` actually picks it up. Subprocess output from
  the download also routes through the logger when no UI callback is
  supplied, so a future failure is visible in `logs/ffn-dl.log`
  instead of vanishing. Reinstalling BookNLP from the GUI isn't
  required — the next audiobook render self-heals.

## 1.12.2 — 2026-04-18

### Fix

- **Auto-update no longer leaves a ghost `%LOCALAPPDATA%\ffn-dl\`
  folder next to the real portable install.** Portable-root resolution
  used a probe file (`tempfile.NamedTemporaryFile` inside the exe dir)
  to decide whether to fall back to AppData. Right after an update,
  the freshly-extracted `ffn-dl.exe` can be briefly non-writable
  (Defender scan, OneDrive indexing, residual handles from
  ZipExtractor), the probe failed, and the fallback path silently
  created empty `cache/` + `neural/` subdirs under `%LOCALAPPDATA%`
  while the real install kept working out of the exe dir. Root
  resolution now checks the exe path against the known
  system-protected roots (`%ProgramFiles%`, `%ProgramFiles(x86)%`,
  `%ProgramW6432%`, `%SystemRoot%`, WindowsApps) and only falls back
  when the install actually lives inside one of them. Ordinary
  locations (Downloads, Desktop, Tools folders) always use the exe
  dir. Users who already have the ghost folder can delete it safely —
  nothing writes to it anymore.

## 1.12.1 — 2026-04-18

### Fix

- **BookNLP attribution now works out of the box.** `pip install
  booknlp` doesn't pull spaCy's `en_core_web_sm`, so first use failed
  with `[E050] Can't find model 'en_core_web_sm'` and the dispatcher
  quietly fell back to the builtin parser — logging the same warning
  once per chapter (44× for a 44-chapter book). The install flow now
  runs `spacy download en_core_web_sm` after installing BookNLP, and
  the runtime path self-heals by attempting the download on first use
  for installs that predate this change.
- **BookNLP model loaded once per render, not once per chapter.** The
  ~150 MB (small) / ~1 GB (big) weights used to reload on every
  chapter; they're now cached on the module for the lifetime of the
  process.
- **Attribution-backend failures no longer spam warnings.** After the
  first fall-back warning for a given backend/size, subsequent
  chapters in the same render stay silent instead of re-emitting the
  same line.

## 1.12.0 — 2026-04-18

### Change

- **Auto-updater rewritten around a bundled `ZipExtractor.exe` helper
  (the same pattern Libation uses, from ravibpatel/AutoUpdater.NET,
  MIT).** Replaces the detached batch-script + `tasklist` poll +
  `robocopy` approach that silently failed in several ways across
  1.10.x and 1.11.x. The new flow copies the helper to `%TEMP%` to
  decouple it from the install, spawns it via Win32 `ShellExecuteW`
  with the `runas` verb only when the install dir isn't
  user-writable (so no UAC prompt in the common case), and lets the
  helper block on our PID via `Process.WaitForExit` before it
  touches any file. The helper uses the Windows Restart Manager API
  to diagnose locked files and writes a `ZipExtractor.log` next to
  itself, so future update failures are actually diagnosable.
- The portable release zip now ships `ZipExtractor.exe` next to
  `ffn-dl.exe`. The Windows workflow builds it from AutoUpdater.NET
  v1.9.2 on each release.

### Add

- **GUI: log-level selector and "Save log to file" checkbox** in the
  Status row. Levels are DEBUG / INFO / WARNING / ERROR; file logs
  go to `logs/ffn-dl.log` inside the portable root (rotating at
  1 MB × 3 backups). An "Open log folder" button opens the folder
  in the platform file browser. Python's root logger is now bridged
  into the in-app status pane so scraper, updater, and TTS log
  records appear alongside the hand-written status messages.
- `cleanup_old_exe()` also sweeps `%TEMP%/ffn-dl-update-*` workdirs
  older than 24h that the old batch updater left behind.

## 1.11.3 — 2026-04-17

### Fix

- **BookNLP attribution now actually runs in the frozen Windows build.**
  PyInstaller's static analysis only bundles stdlib modules it can
  detect as imported from ffn-dl's own code, so modules like `timeit`
  that BookNLP's transitive deps (torch/transformers) import at
  runtime were silently missing from the frozen `ffn-dl.exe`. BookNLP
  would blow up on first use with `No module named 'timeit'`,
  `refine_speakers` would swallow the exception and fall back to the
  builtin regex attribution, and users would see their audiobook
  render fine but `booknlp_models/` stay empty forever because
  BookNLP never actually instantiated. `neural_env.activate()` now
  appends the embeddable Python's `python<MM>.zip` (full stdlib) to
  `sys.path` so any such gap falls back to the embedded stdlib. Fix
  is self-contained — no rebuild of the neural backend install is
  needed; the embedded Python is already sitting in `neural/py/`.

## 1.11.2 — 2026-04-17

### Improve

- **`XXX` / `XXXX` / `X X X` now count as scene-break dividers.**
  Pure-uppercase `X` runs of 3+ characters are overwhelmingly used
  as scene breaks in fanfic but were previously excluded from the
  detector along with `OOO` and lowercase `ooo` / `xxx` — the
  collective exclusion was too broad. `OOO` and the lowercase
  variants stay excluded (ambiguous with rating labels and prose
  affection/laugh markers); uppercase `X` runs get through. Applies
  to both the HTML/EPUB `--hr-as-stars` path and the TTS scene-break
  detector, so `<p>XXX</p>` now renders as `* * *` (or a silence
  beat in audio).

## 1.11.1 — 2026-04-17

### Improve

- **`--strip-notes` now catches divider-bracketed author notes on FFN.**
  The previous heuristic only matched paragraphs that started with an
  explicit ``A/N`` / ``Author's Note`` label, which missed the common
  FFN pattern where notes are wholly bolded paragraphs the author
  fences off with their own text dividers (``-x-x-x-x-...``) and a
  redundant chapter-title banner (``Chapter 1 - Title``). Added two
  structural passes, each gated by multiple signals to keep the
  false-positive rate low:

  - **Top pass**: drops the pre-divider block only when a text /
    ``<hr>`` divider is immediately followed by a chapter-title
    banner AND the pre-divider content is either fully bold or
    contains a narrow note keyword (``patreon``, ``thanks for
    reading``, ``leave a review``, etc.). A fic that opens with a
    flashback and a scene break (no banner after it) is left alone.
  - **Bottom pass**: drops the final divider plus everything after
    it only when that trailing block contains a note keyword. An
    ``-End Chapter-`` style banner immediately before the divider is
    pulled into the drop so the visible chapter doesn't end on it.

- **`--hr-as-stars` now also visualises text-based dividers**
  (``-x-x-x-x-...``, ``***``, ``===``, ``~~~``, etc.), not just
  ``<hr>`` tags. Long symbol-only lines (authors often stretch them
  to 60-80 chars) are recognised regardless of length; ornamental-
  letter lines (``oOo`` / ``xXx``) stay capped at 40 chars and need
  a mixed-case or zero-digit pattern to avoid tripping on short
  words. The TTS scene-break detector got the same length-cap
  relaxation, so audiobooks render the same dividers as silence.

## 1.11.0 — 2026-04-17

### Add

- **Wattpad support.** New site scraper, CLI dispatcher registration,
  clipboard-watch URL pattern, and a Search Wattpad tab in the GUI
  alongside the existing FFN/AO3/RR/Literotica tabs. Metadata is
  lifted from the server-rendered story page by bracket-matching the
  embedded JSON blob (Wattpad's Next.js class names rotate between
  builds), and chapter bodies come from `apiv2/?m=storytext`, the
  same endpoint the mobile app uses. Accepts story URLs
  (`/story/<id>`), part URLs (`/<part_id>`), and bare numeric IDs;
  part URLs are auto-resolved to their owning story via
  `api/v3/story_parts/<id>`.

  Handles Wattpad's Paid Stories program cleanly: paywalled chapters
  return a bilingual "This story is part of the Paid Stories
  program" stub, which the scraper detects, preserves the chapter
  slot in the output with a short placeholder, and skips caching so
  a later unlock (or an author-opened preview) refetches the real
  text. If every requested chapter is paywalled, raises a
  `WattpadPaidStoryError` with guidance to use `--chapters` for the
  free preview parts.

  Author pages (`/user/<name>`) enumerate published stories via the
  mobile API. Search uses `api.wattpad.com/v4/stories` with
  client-side filters for mature/completed (the v4 search endpoint
  has no server-side filter params).

## 1.10.5 — 2026-04-17

### Fix

- **Auto-updater still left users on the old version after 1.10.2.**
  The batch helper waited on the parent PID with `tasklist` (the
  1.10.2 fix) but used `timeout /t 1 /nobreak` between polls. The
  batch is spawned DETACHED, so its cmd.exe has no console, and
  `timeout` needs a console input handle even with /nobreak — it
  fails immediately with "ERROR: Input redirection is not supported,
  exiting the process immediately." The wait loop spun through all
  120 iterations in a few seconds while ffn-dl.exe was still alive,
  hit the `:giveup` branch, and exited without copying the new files
  or relaunching. Swapped `timeout` for `ping -n 2 127.0.0.1 >nul`,
  which doesn't depend on a console and is the canonical detached-
  batch sleep.

## 1.10.4 — 2026-04-17

### Change

- **Audiobook text cleanup is now opt-in, gated on the same flags that
  control the visual output.** 1.10.3 unconditionally stripped A/Ns
  and converted every scene divider to silence in audiobook mode on
  the theory that "nobody wants to hear 'asterisk asterisk asterisk'"
  — but a listener *could* legitimately want the A/N in the narration
  or the literal "star star star" reading. The behaviour now follows
  `--strip-notes` / `--hr-as-stars` (and the matching GUI checkboxes)
  for every output format. With both flags off, the audiobook falls
  back to the pre-1.10.3 behaviour (A/Ns read aloud, `<hr/>` → "* * *"
  via edge-tts). The GUI checkbox label and CLI help text are updated
  to spell out what each flag does in audio mode, and the
  "Mark scene breaks clearly" checkbox now means "asterisks in text
  output, a 1.5-second silence pause in audiobook output".

## 1.10.3 — 2026-04-17

### Fix

- **Audiobook mode was reading author's notes and scene dividers
  aloud.** The `generate_audiobook` path called `html_to_text` on
  chapter HTML with no preprocessing — so any `<p>A/N: ...</p>` note
  was synthesised as narration, and every `<hr/>` turned into the
  literal string `* * *` which edge-tts reads as "asterisk asterisk
  asterisk". The `--strip-notes` and `--hr-as-stars` CLI flags were
  never threaded through to the audio exporter; they only affected
  EPUB/HTML/TXT output. The audiobook pipeline now always runs
  `strip_note_paragraphs` on each chapter (A/Ns are universally wrong
  for a listening experience) and replaces every divider — real
  `<hr/>` tags *and* text-based dividers like `---`, `===`, `* * *`,
  `~~~`, `###`, `oOo`, `xXx`, `o0o`, em-dash runs, and similar — with
  a 1.5-second silence clip inserted at the right spot in the ffmpeg
  concat stream. Detection is permissive enough to catch the endless
  variations fanfic authors invent ("ooOoo", "OoOoO", "•·•·•",
  "*~*~*", "— — —") while still rejecting real short prose
  ("Chapter 1", "Oh.", "OK", ellipses).

## 1.10.2 — 2026-04-17

### Fix

- **Auto-updater silently failed to replace `ffn-dl.exe` and `_internal`
  DLLs.** The updater batch tried to detect whether the parent process
  had released its file locks by renaming `ffn-dl.exe` to a scratch
  name and back — but a running Windows PE can be renamed freely
  (rename touches the directory entry, not the mapped image section),
  so the wait loop exited immediately while the exe was still locked.
  robocopy then hit ERROR 32 on `ffn-dl.exe` and `libcrypto-3.dll`,
  exhausted its 4-second retry budget (`/R:2 /W:1`), and gave up —
  leaving the user on the old version with no error surfaced in the
  GUI. The batch now polls `tasklist` for the parent PID (passed in
  from the spawning process) and waits up to 120 seconds for it to
  exit, with robocopy's per-file retry bumped to `/R:30` as a second
  line of defence against handle-cleanup races.

## 1.10.1 — 2026-04-17

### Fix

- **Literotica downloads were producing empty EPUBs.** Literotica's
  current layout wraps the main story body in a div whose class name
  starts with `_introduction__text_` — historically the class of a
  short author blurb above the body. The chrome-stripping pass in
  `extract_body` was decomposing every element whose class contained
  `_introduction`, which gutted the chapter text and left the reader
  with a "Report" button and nothing else. `_introduction` is now
  absent from the strip list, and the summary is pulled from
  `<meta name="description">` (where the real blurb lives now) instead
  of the repurposed intro div.
- **Audiobook (`-f audio`) failed when the output directory was
  relative.** `build_m4b` wrote bare chapter filenames into its concat
  list file, then invoked ffmpeg with the list sitting in its own
  tempdir. ffmpeg resolves concat `file` entries relative to the list
  file's directory, not process CWD, so `ch_0001.mp3` was looked up
  inside `/tmp/ffn-m4b-xxxx/` and missed every time. This hit every
  default invocation: `ffn-dl -f audio <url>` with no `-o` gave an
  output dir of `Path(".")` and failed unconditionally. Chapter paths
  are now resolved to absolute before going into the concat list.
- **Corrupt cache files no longer crash the downloader.** A partial
  write to `meta.json` or a chapter cache entry used to surface as a
  `ValueError` from `json.loads` mid-download and leave the user to
  manually clear `~/.cache/ffn-dl/`. Both cache loaders now tolerate
  `ValueError` / `UnicodeDecodeError` / `OSError`, log a warning,
  unlink the bad file, and return `None` so the scraper refetches it
  cleanly.
- **Missing EPUB/audio extras no longer waste a full download.** A
  user without `ebooklib` installed running `ffn-dl -f epub` (the
  default) used to fetch every chapter before surfacing the install
  hint. The same held for `-f audio` without `edge-tts`. Both formats
  now pre-flight their optional dependency at the top of the download
  handler, so the error arrives in under a second.
- **Royal Road and other sites without native word counts now show a
  real number in the console summary.** Exporters already fell back to
  counting words from the rendered chapter text when the source site
  didn't expose one, but the CLI's summary line was displaying `Words:
  ?`. The summary now uses the same fallback path so what prints
  matches what lands in the exported file.

## 1.10.0 — 2026-04-18

### Breaking

- **Windows release is now a portable zip, not a single .exe**. Unzip
  `ffn-dl-portable.zip` anywhere and double-click `ffn-dl.exe` inside.
  Everything the app writes — GUI preferences, chapter cache, embedded
  Python for neural backends, installed torch / fastcoref / BookNLP,
  BookNLP model weights — now lives inside that folder. Uninstall is
  "delete the folder"; backup is "zip the folder"; move to another
  machine is "copy the folder." Nothing goes to the registry, AppData,
  or the user's home directory anymore (unless the user unzipped into
  a read-only location like `C:\Program Files`, in which case data
  falls back to `%LOCALAPPDATA%\ffn-dl\`).

### Changed

- **GUI preferences moved from the Windows registry to `settings.ini`**
  alongside `ffn-dl.exe`. Pip-installed ffn-dl is unchanged (still
  uses `wx.Config`'s platform default, including registry on Windows).
  Existing .exe users' registry prefs are NOT migrated — re-set your
  filename template, output directory, and audiobook preferences on
  first launch.
- **Chapter cache moved** from `~/.cache/ffn-dl` to `<exe>/cache/` for
  frozen builds. Pip installs still use the home-dir location.
- **Neural backend install dir moved** from `%LOCALAPPDATA%\ffn-dl\neural`
  (1.9.2) to `<exe>/neural/`. Users who installed fastcoref or BookNLP
  on 1.9.2 will need to reinstall on 1.10.0 via the GUI Install button.
- **BookNLP models** now land in `<exe>/booknlp_models/` instead of
  `~/booknlp_models/`. Achieved by redirecting `HOME`/`USERPROFILE` to
  the portable root at app startup so BookNLP's hardcoded `~/booknlp_models`
  resolves inside the folder.
- **Auto-updater rewritten for the zip format**. Downloads
  `ffn-dl-portable.zip`, extracts to a temp folder, writes a batch
  script that waits for ffn-dl.exe to release its locks, robocopies
  the new files into place (preserving `settings.ini`, `cache/`,
  `neural/`, and `booknlp_models/`), and relaunches. 1.9.2 clients
  will see "new version available" but their old self-updater can't
  apply a zip — download 1.10.0 manually once.

## 1.9.2 — 2026-04-18

### Feature

- **Neural attribution backends install from the standalone .exe**.
  The previous release disabled the Install button when running as
  the frozen Windows build because `sys.executable -m pip` points
  at the .exe bootloader and fails. This release adopts the pattern
  ComfyUI / A1111 / InvokeAI use: on first Install, ffn-dl downloads
  a Python 3.12 embeddable distribution (~10 MB) to
  `%LOCALAPPDATA%\ffn-dl\neural\py\`, bootstraps pip into it, and
  then runs `pip install --target=<neural\deps>` with that
  interpreter. On app startup `ffn_dl/__init__.py` calls
  `site.addsitedir()` on that deps directory so torch's `.pth`
  registration works and `import fastcoref` / `import booknlp`
  succeed from the frozen exe. Torch is pulled from PyPI's
  `whl/cpu` index so users don't accidentally download the 2.5 GB
  CUDA build. After a successful install a message dialog asks the
  user to restart ffn-dl so the new modules are loaded before the
  first audiobook render.

## 1.9.1 — 2026-04-18

### Fix

- **Install button no longer crashes the standalone .exe build**. In
  a PyInstaller-frozen exe `sys.executable` points at ffn-dl.exe
  itself (not at a Python interpreter), so `sys.executable -m pip
  install booknlp` would route the pip flags into ffn-dl's own
  argparse and fail with "unrecognized arguments: -m --upgrade
  booknlp". The .exe's bundled Python is also isolated and read-only,
  so neural backends can't be imported from it even if the install
  somehow succeeded. The GUI now detects the frozen state,
  disables the Install button, and displays "(not available in .exe
  build)" next to the backend choice. Selecting a neural backend
  logs a clear explanation pointing at the pip install path
  (`pip install ffn-dl[gui,audio]` + `pip install fastcoref` /
  `booknlp`). CLI `--install-attribution` similarly surfaces the
  explanation instead of attempting the doomed subprocess.
  Built-in attribution, speech rate, inter-speaker pauses, and the
  pronunciation override map all still work in the .exe as before.

## 1.9.0 — 2026-04-17

### Audiobook — major overhaul

- **Character names are no longer stripped from audiobook narration**.
  Previously the TTS pipeline consumed "Harry said" after a quote so
  only Harry's voice would read the line. That meant each character
  had a unique voice but no way for a listener to tell who was
  speaking. The narrator now reads attribution text aloud
  ("Harry said") while the character voice handles the quoted line —
  exactly how a regular audiobook sounds.
- **Much better speaker attribution**, driven by a stress-test pass
  that found 11 distinct categories of bugs:
  - Titled camelcase surnames ("Professor McGonagall") are detected
    as a single speaker instead of being split or lost entirely.
  - Question words ("Where", "Why", "Who", "Which", "Whom") no
    longer leak into the speaker list.
  - Pronoun resolution is gender-aware — "he muttered" after
    "Hermione called" now resolves to the nearest male character
    rather than picking the most recent name regardless of gender.
  - Pre-dialogue action attribution ("Ron looked up. 'Trouble?'")
    is now recognized as Ron speaking.
  - "paused", "hesitated", "stopped" are treated as dialogue-
    adjacent verbs so interrupted speech stays attributed.
  - Back-and-forth unattributed dialogue alternates between the
    two most recent speakers instead of sticking to one voice.
  - Unattributed dialogue is read with quote marks preserved so
    the narrator voice renders it with dialogue intonation rather
    than sounding like exposition.
  - "Mr. Dumbledore" and "Mr Dumbledore" merge into a single
    speaker instead of getting two different voices.
  - Carry-forward extended to longer narration gaps when no other
    named character breaks in.
- **Speech rate control**. New spinbox in the GUI (shown only for
  audio format) and `--speech-rate PCT` flag for the CLI. Integer
  percent delta applied to every synthesis call; combines additively
  with emotion-driven rate shifts so a shout stays a shout at +30%.
- **Inter-speaker pauses**. A 400 ms silence clip is inserted at
  every voice change so multi-character scenes stop sounding like
  a relay handoff.
- **Per-story pronunciation overrides**. An editable JSON file
  `.ffn-pronunciations-<id>.json` in the audiobook output folder
  lets you respell names and invented words that edge-tts mangles.
  First run writes a skeleton file with instructions.
- **Optional neural attribution backends**. A new module ships
  with registry-driven support for alternative attribution models:
  - **fastcoref** (~90 MB, via `pip install fastcoref`) remaps
    pronoun-attributed lines to the correct named character using
    neural coreference.
  - **BookNLP** (~150 MB small / ~1 GB big, via `pip install
    booknlp`) replaces attribution with Bamman et al.'s full
    quote + coref pipeline — most accurate on long works.
  - Selected in the GUI (dropdown + background pip install) or
    via `--attribution {builtin,fastcoref,booknlp}`. Install with
    `ffn-dl --install-attribution BACKEND`.
  - Missing or failing backends silently fall back to the built-in
    parser — audiobook renders never crash on a missing dep.
- **Model size selector** for backends that offer size variants.
  BookNLP exposes Small and Big; the GUI shows a secondary
  dropdown next to the backend choice when relevant, hidden
  otherwise.

## 1.8.5 — 2026-04-17

### Fix

- **Royal Road "Words" column now shows an estimated word count
  instead of raw pages**. RR search cards don't expose a word count
  at all — only a page count — so the previous code showed
  "2,534p" in the Words column, which was read as if it were a
  tiny 4-digit word count. Converted at RR's house ratio of 275
  words per page and displayed with a leading "~" to mark it as
  an estimate (e.g. "~696,850"). The fiction page itself has the
  authoritative number and is picked up at download time.

## 1.8.4 — 2026-04-17

### Diagnostics

- **Version shown in window title**. Previously the running version
  was only visible from the "Update available" dialog — if you wanted
  to know whether an auto-update had actually taken, you had no way
  to tell at a glance. Title bar now reads "ffn-dl 1.8.4 - Fanfiction
  Downloader".
- **Search errors now pop up as a message box**, not just a line in
  the status log at the bottom of the window. The log is easy to miss
  when the expected outcome is "results appear in the list above" —
  and with NVDA the scrolled-off log line won't be announced at all.
  Error popups force attention and read out the full message.

## 1.8.3 — 2026-04-17

### Fix

- **Filter-only searches no longer rejected as "missing query"**. After
  1.8.0 added the Genres / Tags / Warnings multi-pickers, clicking
  Search with just a tag ticked (and no free-text query typed) bounced
  off the "Please enter a search query" guard and did nothing — even
  though Royal Road's `/fictions/search?tagsAdd=progression` works
  fine on its own. The GUI gate now recognizes RR tag-only, genre-only,
  warning-only, and numeric-bound-only searches, plus Literotica
  category-only, as valid standalone browses. The CLI gate was widened
  in the same way: `--rr-genres Fantasy` (no `--search`) now runs.

## 1.8.2 — 2026-04-17

### CI fix

- **Lazy-import `edge_tts`**. `ffn_dl/tts.py` did a top-level
  `import edge_tts`, so importing anything from the module (e.g.
  the FFMETADATA escape helper exercised by `test_exporters.py`)
  required the `audio` optional extra. CI installs only `[dev,epub]`,
  so the Tests workflow had been silently red since 1.7.2 when those
  tests were added. The import is now deferred to the two call
  sites that actually synthesize audio, with a clear error message
  if someone tries to build an audiobook without the extra installed.

## 1.8.1 — 2026-04-17

### Fixes

- **Search query no longer persists across sessions.** Whatever was
  typed into the search box used to come back on next launch — more
  annoying than useful. Filters, tag picks, and checkboxes still
  persist (those are painful to re-set), but the query field starts
  empty every launch.
- **Auto-update restart no longer races the new process.** On Windows
  the old `restart()` did `subprocess.Popen + sys.exit(0)` with no
  detach flags, so the child inherited the parent's console + process
  group and its PyInstaller `_MEIPASS` extraction could race the
  parent's cleanup of the same temp dir. Symptom: app reopened but
  search (and any other curl_cffi network call) silently did nothing
  on the first post-update launch. The child is now spawned DETACHED
  with a new process group, breaking away from any Job object the
  installer might have placed us in. On POSIX we use `os.execv`
  instead (same PID, no second process, no race). wx.Config is also
  flushed explicitly before the spawn so the child can't read stale
  registry values that the parent hadn't yet written out.
- **Prefs re-saved immediately before update restart.** The previous
  code saved prefs at the start of the download, so any filter tweaks
  the user made while the progress dialog was open were lost.

## 1.8.0 — 2026-04-17

### Search filters

- **Royal Road gets genres, tags, warnings, and numeric bounds as
  first-class filters**. Previously the only RR discovery surface was
  the free-text "Tags" box, which required knowing RR's tag slugs
  (`progression`, `litrpg`, `xianxia`, …). Three new multi-pick
  dialogs (Genres / Tags / Warnings) expose RR's full canonical list
  behind the Search Royal Road tab's `Pick…` buttons, with a type-to-
  filter field so you can jump straight to "LitRPG" or "Portal
  Fantasy / Isekai" without scrolling. Also added min/max word-count,
  min/max page-count, and minimum rating text filters.
- **AO3 category and language dropdowns**. Previously category
  (Gen / F/M / M/M / F/F / Multi / Other) wasn't exposed at all, and
  language was a free-text ISO-code field. Both are now proper choice
  dropdowns — language accepts either a pretty label ("French") or a
  raw code ("fr") for languages not in the canonical list.
- **FFN second-genre filter**. FFN's search form has two genre
  dropdowns that AND together; only the first was wired up.
  Genre 2 now lets you narrow to e.g. "Romance" AND "Angst".
- **Literotica category browsing**. A Category dropdown now lists
  all 29 of Literotica's top-level categories (Loving Wives, Sci-Fi
  & Fantasy, Romance, …) and browses that category without needing
  to know its tag slug. Unknown labels still fall back to slug-
  normalization so anything typable works.

New multi-pick dialog (`MultiPickerDialog`) follows the same NVDA
compatibility pattern as the story picker: literal `[x] ` / `[ ] `
prefixes on every row so check state is readable, and a filter field
on top for keyboard-only narrowing.

### CLI

New flags: `--genre2`, `--ao3-category`, `--ao3-freeform`,
`--rr-genres`, `--rr-warnings`, `--rr-min-words`, `--rr-max-words`,
`--rr-min-pages`, `--rr-max-pages`, `--rr-min-rating`,
`--lit-category`. `--lit-category` can stand in for `--search` the
same way `--rr-list` already does — you can browse "Loving Wives"
with no query.

## 1.7.2 — 2026-04-17

### Audiobook

- **FFMETADATA1 special-character escaping**: story and chapter titles
  containing `=`, `;`, `#`, `\`, or a newline were passed straight into
  the chapter metadata file, silently breaking ffmpeg's parser and
  aborting the m4b mux. Every value written to `chapters_meta.txt` now
  goes through a spec-compliant escape helper.
- **ffmpeg errors now surface stderr**: `subprocess.run(check=True)`
  was hiding the actual ffmpeg message behind a bare
  `CalledProcessError`, so when a mux failed the user just saw "error"
  with no way to tell whether it was metadata, codec, or concat.
  Failures now raise `RuntimeError` with the last twenty lines of
  ffmpeg's stderr and the pipeline step that blew up.

## 1.7.1 — 2026-04-17

### Downloads

- **Parallel chapter fetches on Royal Road, FicWad, and MediaMiner**:
  these scrapers used to fetch every chapter serially — on a 500-chapter
  RR epic that meant paying the HTTP round-trip 500 times in sequence.
  Downloads now run with a small worker pool (default 3) so idle wire
  time turns into actual throughput. Each worker uses its own session
  so concurrent libcurl handles don't race.
- **AIMD on concurrency too**: the same feedback loop that halves the
  delay on 429/503 now also halves the active concurrency for the next
  batch, all the way down to sequential if the site keeps pushing
  back. FFN stays at concurrency=1 — it captcha-bans on bulk fetching
  regardless of parallelism.
- **AO3 and Literotica are unchanged**: AO3 grabs the whole work in a
  single `view_full_work=true` request (no chapter loop to parallelise),
  and Literotica stories are typically one or two pages where the
  pooling overhead isn't worth it.

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
