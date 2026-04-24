# ffn-dl

Cross-platform fanfiction and original-fiction downloader. Exports as
EPUB, HTML, plain text, or a chaptered M4B audiobook.

**Fanfic / original-fiction sites**
FanFiction.net · Archive of Our Own · FicWad · Royal Road ·
MediaMiner · Wattpad

**Erotica sites**
Literotica · Adult-FanFiction.org (AFF) · StoriesOnline · Nifty ·
SexStories · MCStories · Lushstories · Fictionmania · TGStorytime ·
Chyoa (interactive) · Dark Wanderer · GreatFeet

Every site supports direct-URL download and appears in the GUI's
search windows. The erotica sites share a unified "Erotic Story
Search" that fans out a query across all of them in parallel and
collapses results per site.

Accessible by design — the desktop GUI uses native widgets on every
platform (wxPython wraps Win32 on Windows, Cocoa on macOS, GTK3 on
Linux), so NVDA, JAWS, VoiceOver, and Orca read it the same way they
read any app on those platforms. The CLI is plain text with no
interactive TUI gotchas, usable from any screen-readable terminal.

## Install

### Windows (recommended)

Download the latest `ffn-dl-portable.zip` from the
[Releases page](https://github.com/matalvernaz/ffn-dl/releases). It's a
self-contained folder with its own Python, ffmpeg, and ffprobe bundled —
no dependencies to install. The app auto-updates from GitHub when a new
release is published.

### macOS (Apple Silicon)

Download `ffn-dl-macos-arm64.tar.gz` from the Releases page, extract, and
run `./ffn-dl/ffn-dl`. The binary is unsigned, so the first launch needs
right-click → Open (or **System Settings → Privacy & Security → Open
Anyway**) to clear Gatekeeper.

### Linux (x86_64)

Download `ffn-dl-linux-x86_64.tar.gz` from the Releases page, extract,
and run `./ffn-dl/ffn-dl`. Built against GTK3 — any modern desktop Linux
(Ubuntu 22.04+, Fedora 38+, Debian 12+) has the runtime libraries
already installed.

### pip (any platform, dev)

```bash
pip install "ffn-dl[all] @ git+https://github.com/matalvernaz/ffn-dl"
```

Extras are split so you only pull what you need:

| Extra       | Adds                          |
|-------------|-------------------------------|
| `epub`      | EPUB export (`ebooklib`)      |
| `audio`     | Audiobook synthesis (`edge-tts`) — requires ffmpeg on PATH |
| `gui`       | wxPython desktop GUI          |
| `clipboard` | Clipboard-watch mode          |
| `cf-solve`  | Playwright-backed Cloudflare-challenge fallback (also needs `playwright install chromium`) |
| `all`       | All of the above *except* `cf-solve` (opt-in due to ~400MB browser binary) |

The desktop binaries (Windows / macOS / Linux) ship with every extra
except `cf-solve` already included. Install `cf-solve` from **Edit →
Optional Features...** if you need it.

## Using it

### GUI

`ffn-dl` with no arguments launches the GUI — that's what
double-clicking the desktop binary does. From a pip install:

```bash
ffn-dl                    # no args → GUI
python -m ffn_dl.gui      # explicit GUI launch
```

The main window is a download form. Search windows open from the
**Search** menu:

- **FFN** (Ctrl+1) — full filter set: genre, rating, language, word
  count, status, world, up to four characters, pairing, exclusions
- **AO3** (Ctrl+2) — with series collapse when 2+ parts appear
- **Royal Road** (Ctrl+3) — query-based search plus list browse for
  Rising Stars / Best Rated / Complete / Weekly Popular
- **Wattpad** (Ctrl+4)
- **Erotic Story Search** (Ctrl+5) — unified fan-out across all
  twelve erotica sites (Literotica, AFF, StoriesOnline, Nifty,
  SexStories, MCStories, Lushstories, Fictionmania, TGStorytime,
  Chyoa, Dark Wanderer, GreatFeet) with a per-site scope dropdown
  for when you already know where you want to search

The **Library** menu has scan / reorganize / update / abandoned
management. **Watchlist** lets you follow authors or searches and
get a Pushover / Discord / email ping when a tracked story
updates. **Edit → Optional Features...** installs the extras
(EPUB, audio, clipboard, cf-solve) at runtime on any build — the
frozen desktop binaries pip-install into a portable `deps/`
folder so "delete the folder" actually uninstalls. Multi-select
pickers and result lists mirror their check / selection state
into the row label so every screen reader speaks it reliably.

### CLI — common tasks

```bash
# Single story (URL or ID). URLs for any of the supported sites
# work — the scraper is auto-selected from the URL.
ffn-dl https://www.fanfiction.net/s/12345
ffn-dl 12345
ffn-dl https://www.literotica.com/s/example-story
ffn-dl https://storiesonline.net/s/12345

# Batch from a text file (one URL per line, mixed sites allowed)
ffn-dl -b urls.txt

# Pick format
ffn-dl -f html  https://archiveofourown.org/works/1234
ffn-dl -f audio https://www.royalroad.com/fiction/26727   # needs ffmpeg

# All of an author's stories
ffn-dl -a https://www.fanfiction.net/u/1234/Name
ffn-dl -a https://archiveofourown.org/users/Name/works

# AO3 series merged into a single file
ffn-dl --merge-series https://archiveofourown.org/series/1234

# Search
ffn-dl -s "time travel" --site ffn  --sort favorites
ffn-dl -s "dungeon"      --site royalroad --rr-tags progression,magic
ffn-dl --rr-list "rising stars"   # list browse — no query needed
ffn-dl -s "werewolf"     --site literotica
# (fan-out search across every erotica site is GUI-only — open the
# Erotic Story Search window from the GUI search menu)

# Update an existing export with new chapters
ffn-dl -u "Path/To/Story.epub"

# Update a whole library folder (unchanged fics cost one HTTP probe)
ffn-dl -U ~/Fanfic --recursive --skip-complete

# Partial downloads
ffn-dl --chapters 1-5,10,50- https://...      # flexible ranges

# Send an EPUB to Kindle after download
ffn-dl --send-to-kindle you@kindle.com https://...
```

`ffn-dl --help` has the full list.

## Library management

Once you've scanned a directory of downloaded stories, ffn-dl tracks
them in a library index and layers several tools on top.

```bash
# One-time scan of a directory — identifies every story, records
# metadata, bootstraps the library index.
ffn-dl --scan-library ~/Fanfic

# During scan: auto-mark WIPs (status != Complete) whose file
# hasn't been touched in DAYS days as abandoned, so subsequent
# --update-library runs skip them. Reads the
# library_abandoned_after_days user pref by default; pass
# --abandoned-after-days N to override, or 0 to disable.
ffn-dl --scan-library ~/Fanfic --abandoned-after-days 730

# Review the abandoned list (scope with --library-dir)
ffn-dl --list-abandoned

# Revive one URL (the author posted again!) or all at once
ffn-dl --revive-abandoned https://www.fanfiction.net/s/12345
ffn-dl --revive-abandoned          # no URL = revive every marked story

# Search by metadata (title / author / fandom / URL substring)
ffn-dl --library-find "time travel"

# Full-text search across every indexed chapter body. Uses SQLite
# FTS5 syntax: prefix wildcards (dragon*), NEAR(a b), and boolean
# operators (AND / OR / NOT) all work. Bootstrap is a one-time
# --populate-search DIR; subsequent --update-library runs keep the
# index warm. Stories downloaded via direct URL (not the library
# update path) land in the text index on the next --populate-search.
ffn-dl --populate-search ~/Fanfic
ffn-dl --library-search "orphanage scene"

# Detect suspected cross-site mirror pairs (same story on FFN and
# AO3, Literotica and StoriesOnline, etc.). Needs >=2 corroborating
# signals (normalised title match, author match, first-chapter word
# overlap) to flag a pair, so common titles don't produce false
# positives. Read-only; never deletes.
ffn-dl --find-mirrors ~/Fanfic

# Hygiene: library doctor, watchlist doctor, cache doctor, or all
# three at once. --heal applies safe fixes; the index is auto-
# backed-up before destructive operations so --restore-index FILE
# can roll back a bad heal.
ffn-dl --doctor
ffn-dl --doctor --heal

# Per-chapter silent-edit detection. Hash-based, so an author's
# in-place typo fix shows up even though the chapter count didn't
# change.
ffn-dl --populate-hashes ~/Fanfic    # one-time bootstrap
ffn-dl --scan-edits ~/Fanfic         # drift report
```

### Auto-sort and the Original Works folder

When you configure a library path in preferences, new downloads are
sorted into fandom subfolders automatically. The auto-sorter
recognises each site's category format: FFN's `Books > Harry Potter`
breadcrumbs get their leading meta-category stripped, AO3's
`Harry Potter / Naruto` crossover joins get split so multi-fandom
routes to the misc bucket, and plain single-fandom strings pass
through untouched. Royal Road is treated as an original-fiction
source — RR downloads land in `Original Works/` rather than `Misc/`,
so your library surfaces original novels as a dedicated subtree
alongside the fandom folders.

Upgrading from a 1.x install with an existing library?
2.0.0 changes the auto-sort layout for FFN and Royal Road downloads —
run `ffn-dl --reorganize ~/Fanfic --apply` to migrate your existing
files to match the new layout. The dry-run (without `--apply`) prints
the proposed moves first.

## What it handles automatically

- **Rate limiting**: adaptive (AIMD) inter-chapter delay — starts fast,
  backs off on 429/503, decays back down on clean responses. FFN has a
  known 2s floor because of its bulk-captcha. `--delay-min` /
  `--delay-max` override with a fixed range if you want the old
  behaviour.
- **Parallel chapter fetches** on Royal Road, FicWad, and MediaMiner
  (default 3 workers, same AIMD feedback halves concurrency on
  rate-limit responses). FFN stays sequential.
- **Cloudflare impersonation** via `curl_cffi` (Chrome, Edge, Safari).
  Stubborn 403s can opt into `--cf-solve`, which launches a headless
  Chromium via Playwright, lets the challenge resolve, and injects
  the solved cookies into the scraper session. Solved cookies are
  cached under `~/.cache/ffn-dl/cf-cookies/` (chmod 0600) for 24
  hours so later runs reuse them without re-launching the browser.
- **Per-chapter caching** in `~/.cache/ffn-dl`, so interrupted downloads
  resume cheaply and update-mode only fetches what actually changed.
- **Cover image cache** at 7-day TTL so re-exporting a long series
  doesn't re-download the same cover per part.
- **Wayback fallback** (`--use-wayback`): when the live site 404s, try
  the most recent archive.org snapshot before giving up.
- **Series handling**: AO3 series collapse in search results when 2+
  parts appear; Literotica chapters (`Ch. NN` / `Pt. NN` / `- N` / `PN`)
  collapse per author + URL stem, and downloading the collapsed row
  resolves the canonical `/series/se/<id>` to pull chapters that
  didn't match the search.
- **Royal Road stubbed fictions**: the misleading bare `Stub` status is
  replaced with `Complete (Stubbed)` / `In-Progress (Stubbed)` /
  `Stubbed` depending on what RR exposes.

## Library-update performance knobs

Large libraries (thousands of fics) benefit from three gates on
`--update-library`:

- **`--recheck-interval SECONDS`** — skip stories whose index
  `last_probed` timestamp is within SECONDS of now. A value like
  `3600` makes a second `--update-library` minutes after the first
  near-instant.
- **`--skip-stale-complete DAYS`** — skip stories that are both
  marked Complete and whose file mtime is at least DAYS old. Gentler
  than `--skip-complete`: a fic completed yesterday still gets
  probed (the author may add an epilogue), but one untouched for a
  year stops costing an HTTP probe each run.
- **Abandoned WIPs get skipped automatically.** Any story carrying
  an `abandoned_at` timestamp in the index is dropped from the
  probe queue — the mark is set by `--scan-library` when
  `library_abandoned_after_days` is configured (or
  `--abandoned-after-days N` is passed explicitly) and the story's
  file has been untouched that long without being Complete. The
  mark is sticky until revived with `--revive-abandoned URL` (or
  all at once via the same flag with no argument). The Library
  dialog in the GUI exposes both the threshold setting and a
  "Manage abandoned..." review list so screen-reader users can
  walk the list and revive without touching the CLI.

`--recheck-interval` and `--skip-stale-complete` are overridden by
`--force-recheck`. Abandoned entries stay skipped — once you've
declared a WIP dead, a forced recheck doesn't automatically bring
it back; use `--revive-abandoned` to undo the mark.

## Audiobook notes

`-f audio` synthesises each chapter through
[edge-tts](https://github.com/rany2/edge-tts) (Microsoft's neural voices)
and concatenates into a chaptered M4B with embedded cover art. Needs
`ffmpeg` and `ffprobe` on PATH for the pip install; they're bundled in
the Windows / macOS / Linux binaries.

Character voice casting runs through
[BookNLP](https://github.com/booknlp/booknlp) when installed — each
speaker gets a distinct Microsoft voice, the narrator stays on a
stable baseline, and dialogue attribution falls back to a regex-based
parser when BookNLP isn't available or fails mid-run.

## Accessibility

ffn-dl is built and tested with screen-reader users as a first-class
audience. Concretely:

- **Windows**: GUI tested with NVDA. Multi-select pickers, search
  result rows, and watchlist entries mirror their check/selection
  state into the visible label text so MSAA-fragile controls still
  read correctly.
- **macOS**: wxPython wraps native Cocoa widgets; VoiceOver reads
  the GUI using the same AXUIElement tree it reads in Safari or
  Mail.
- **Linux**: wxPython wraps GTK3 widgets; Orca reads the GUI via
  at-spi2 (the system accessibility bus). Any distro that installs
  GNOME or KDE has at-spi2 active by default.
- **CLI on every platform**: plain text, one line per decision, no
  animated progress bars or cursor manipulation. Works in any
  terminal a screen reader can read (Windows Terminal, macOS
  Terminal + VoiceOver, any Linux terminal with Orca or BRLTTY).

## Development

```bash
git clone https://github.com/matalvernaz/ffn-dl
cd ffn-dl
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest
```

Tests run offline against static HTML fixtures and don't hit the network.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
