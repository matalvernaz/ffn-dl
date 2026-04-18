# Changelog

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
