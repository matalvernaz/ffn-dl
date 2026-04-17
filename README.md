# ffn-dl

Cross-platform fanfiction downloader. Pulls stories from **FanFiction.net**,
**Archive of Our Own**, **FicWad**, **Royal Road**, **MediaMiner**, and
**Literotica** and exports them as EPUB, HTML, plain text, or a chaptered
M4B audiobook.

Accessible by design — the Windows build and wxPython GUI are tested with
NVDA, and the CLI produces screen-reader-friendly text output with no
interactive TUI gotchas.

## Install

### Windows (recommended)

Download the latest `ffn-dl.exe` from the
[Releases page](https://github.com/matalvernaz/ffn-dl/releases). It's a
single self-contained binary with its own Python, ffmpeg, and ffprobe
bundled — no dependencies to install. The app auto-updates from GitHub
when a new release is published.

### pip (Linux / macOS / dev)

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
| `all`       | All of the above              |

## Using it

### GUI

```bash
ffn-dl-gui      # installed as a script when the gui extra is present
# or
python -m ffn_dl.gui
```

Tabs for Download, FFN Search, AO3 Search, Royal Road Search (with list
browse for Rising Stars / Best Rated / etc.), and Literotica Search.
Author / bookmark pickers are multi-select with NVDA-readable check
state and a summary pane.

### CLI — common tasks

```bash
# Single story (URL or ID)
ffn-dl https://www.fanfiction.net/s/12345
ffn-dl 12345

# Batch from a text file
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
- **Per-chapter caching** in `~/.cache/ffn-dl`, so interrupted downloads
  resume cheaply and update-mode only fetches what actually changed.
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

## Audiobook notes

`-f audio` synthesises each chapter through
[edge-tts](https://github.com/rany2/edge-tts) (Microsoft's neural voices)
and concatenates into a chaptered M4B with embedded cover art. Needs
`ffmpeg` and `ffprobe` on PATH for the Linux/macOS install; they're
bundled in the Windows .exe.

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
