"""Command-line interface for ffn-dl."""

import argparse
import logging
import re
import sys
from pathlib import Path

from .ao3 import AO3LockedError, AO3Scraper
from .exporters import DEFAULT_TEMPLATE, EXPORTERS
from .ficwad import FicWadScraper
from .literotica import LiteroticaScraper
from .mediaminer import MediaMinerScraper
from .models import parse_chapter_spec
from .royalroad import RoyalRoadScraper
from .scraper import (
    CloudflareBlockError,
    FFNScraper,
    RateLimitError,
    StoryNotFoundError,
)
from .updater import count_chapters, extract_source_url, extract_status


def _detect_site(url):
    """Return the appropriate scraper class based on the URL."""
    text = str(url).lower()
    if "ficwad.com" in text:
        return FicWadScraper
    if "archiveofourown.org" in text or "ao3.org" in text:
        return AO3Scraper
    if "royalroad.com" in text:
        return RoyalRoadScraper
    if "mediaminer.org" in text:
        return MediaMinerScraper
    if "literotica.com" in text:
        return LiteroticaScraper
    return FFNScraper


def _is_author_url(url):
    """Return True if the URL points to an author page on any supported site."""
    return (
        FFNScraper.is_author_url(url)
        or FicWadScraper.is_author_url(url)
        or AO3Scraper.is_author_url(url)
        or RoyalRoadScraper.is_author_url(url)
        or MediaMinerScraper.is_author_url(url)
        or LiteroticaScraper.is_author_url(url)
    )


def _is_series_url(url):
    """Return True if the URL points to a series (AO3 or Literotica)."""
    return AO3Scraper.is_series_url(url) or LiteroticaScraper.is_series_url(url)


def _scrape_author_stories(url, args):
    """Scrape an author page and return (author_name, [story_urls])."""
    scraper = _build_scraper(url, args)
    return scraper.scrape_author_stories(url)


def _scrape_series_works(url, args):
    """Scrape an AO3 series and return (series_name, [work_urls])."""
    scraper = _build_scraper(url, args)
    return scraper.scrape_series_works(url)


def _merge_stories(series_name, series_url, stories):
    """Combine a series of Story objects into one Story for single-file export."""
    from html import escape
    from .models import Chapter, Story

    authors = []
    for s in stories:
        if s.author and s.author not in authors:
            authors.append(s.author)
    combined_author = authors[0] if len(authors) == 1 else ", ".join(authors)

    summaries = []
    for s in stories:
        if s.summary:
            summaries.append(f"<strong>{escape(s.title)}</strong>: {escape(s.summary)}")
    combined_summary = "\n".join(summaries) or "A series of works."

    total_words = 0
    per_work_words = []
    for s in stories:
        w = s.metadata.get("words", "").replace(",", "").strip()
        if w.isdigit():
            total_words += int(w)
            per_work_words.append(int(w))

    all_complete = all(
        s.metadata.get("status", "").lower() == "complete" for s in stories
    )

    merged = Story(
        id=0,
        title=series_name,
        author=combined_author or "Various",
        summary=combined_summary,
        url=series_url,
    )
    if total_words:
        merged.metadata["words"] = f"{total_words:,}"
    merged.metadata["status"] = "Complete" if all_complete else "In-Progress"
    merged.metadata["category"] = "AO3 series"

    ch_num = 1
    for s in stories:
        header_html = (
            f"<h1>{escape(s.title)}</h1>"
            f"<p><em>by {escape(s.author)}</em></p>"
        )
        if s.summary:
            header_html += f"<blockquote>{escape(s.summary)}</blockquote>"
        if s.url:
            header_html += (
                f'<p><a href="{escape(s.url)}">Original on AO3</a></p>'
            )
        merged.chapters.append(
            Chapter(number=ch_num, title=s.title, html=header_html)
        )
        ch_num += 1
        for ch in s.chapters:
            merged.chapters.append(
                Chapter(number=ch_num, title=ch.title, html=ch.html)
            )
            ch_num += 1

    return merged


def _handle_merge_series(series_urls, args, output_dir):
    """Download each series URL (AO3 or Literotica), merge its works, export as one file."""
    all_ok = True
    for series_url in series_urls:
        scraper = _build_scraper(series_url, args)
        try:
            series_name, work_urls = scraper.scrape_series_works(series_url)
        except (RateLimitError, CloudflareBlockError, StoryNotFoundError) as exc:
            print(f"Error fetching series {series_url}: {exc}", file=sys.stderr)
            all_ok = False
            continue
        if not work_urls:
            print(f"No works found in series: {series_url}", file=sys.stderr)
            all_ok = False
            continue

        print(f"\nSeries: {series_name}")
        print(f"Downloading and merging {len(work_urls)} works...\n")
        stories = []
        for i, work_url in enumerate(work_urls, 1):
            print(f"  [{i}/{len(work_urls)}] {work_url}")
            def progress(current, total, title, cached):
                tag = " (cached)" if cached else ""
                print(f"      [{current}/{total}] {title}{tag}")
            work_scraper = _build_scraper(work_url, args)
            try:
                story = work_scraper.download(work_url, progress_callback=progress)
                stories.append(story)
            except Exception as exc:
                print(f"    Error: {exc}", file=sys.stderr)
                all_ok = False

        if not stories:
            print(f"Nothing downloaded for series {series_name}.", file=sys.stderr)
            all_ok = False
            continue

        merged = _merge_stories(series_name, series_url, stories)

        print(f"\n  Merged {len(stories)} works / {len(merged.chapters)} sections")
        if args.format == "audio":
            from .tts import generate_audiobook
            def audio_progress(current, total, title):
                print(f"  Synthesizing [{current}/{total}] {title}")
            path = generate_audiobook(
                merged, str(output_dir), progress_callback=audio_progress
            )
        else:
            exporter = EXPORTERS[args.format]
            path = exporter(
                merged, str(output_dir), template=args.name,
                hr_as_stars=args.hr_as_stars,
                strip_notes=args.strip_notes,
            )
        print(f"  Saved: {path}")
    return all_ok


def _build_scraper(url, args):
    """Build a scraper instance for the given URL using CLI args."""
    scraper_cls = _detect_site(url)
    kwargs = {
        "max_retries": args.max_retries,
        "use_cache": not args.no_cache,
    }
    if args.delay_min is not None and args.delay_max is not None:
        kwargs["delay_range"] = (args.delay_min, args.delay_max)
    elif args.delay_min is not None or args.delay_max is not None:
        d_min = args.delay_min if args.delay_min is not None else 1.0
        d_max = args.delay_max if args.delay_max is not None else 5.0
        kwargs["delay_range"] = (d_min, d_max)
    if args.chunk_size is not None:
        kwargs["chunk_size"] = args.chunk_size
    if getattr(args, "use_wayback", False):
        kwargs["use_wayback"] = True
    return scraper_cls(**kwargs)


def _download_one(url, args, output_dir, *, update_path=None, existing_chapters=0):
    """Download and export a single story. Returns True on success, False on error."""
    scraper = _build_scraper(url, args)

    def progress(current, total, title, cached):
        tag = " (cached)" if cached else ""
        print(f"  [{current}/{total}] {title}{tag}")

    try:
        story_id = scraper.parse_story_id(url)
        if update_path:
            print(
                f"Checking story {story_id} on {scraper.site_name} "
                f"(existing file has {existing_chapters} chapters)..."
            )
        else:
            print(f"Downloading story {story_id} from {scraper.site_name}...")

        chapter_spec = parse_chapter_spec(getattr(args, "chapters", None))
        story = scraper.download(
            url,
            progress_callback=progress,
            skip_chapters=existing_chapters,
            chapters=chapter_spec,
        )

        new_count = len(story.chapters)
        words = story.metadata.get("words", "?")
        status = story.metadata.get("status", "Unknown")

        if update_path and new_count == 0:
            print(f"\n  Up to date — no new chapters.")
            return True

        print()
        print(f"  Title:    {story.title}")
        print(f"  Author:   {story.author}")
        if update_path:
            total = existing_chapters + new_count
            print(f"  Chapters: {total} ({new_count} new)")
        else:
            print(f"  Chapters: {new_count}")
        print(f"  Words:    {words}")
        print(f"  Status:   {status}")

        if update_path:
            # For update, we need the full story to re-export.
            # Re-download everything (cache makes this fast).
            print("\n  Re-exporting full story...")
            story = scraper.download(url, skip_chapters=0, chapters=chapter_spec)

        if args.format == "audio":
            from .tts import generate_audiobook

            def audio_progress(current, total, title):
                print(f"  Synthesizing [{current}/{total}] {title}")

            print("\nGenerating audiobook...")
            path = generate_audiobook(
                story, str(output_dir), progress_callback=audio_progress
            )
        else:
            exporter = EXPORTERS[args.format]
            path = exporter(
                story,
                str(output_dir),
                template=args.name,
                hr_as_stars=args.hr_as_stars,
                strip_notes=args.strip_notes,
            )
        print(f"\nSaved to: {path}")

        if getattr(args, "send_to_kindle", None):
            try:
                from .mailer import SMTPConfigError, send_file

                send_file(args.send_to_kindle, path)
                print(f"Emailed to: {args.send_to_kindle}")
            except SMTPConfigError as exc:
                print(f"Could not send: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"Email failed: {exc}", file=sys.stderr)

        if args.clean_cache:
            scraper.clean_cache(story_id)

        return True

    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False
    except StoryNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False
    except AO3LockedError as exc:
        print(f"Locked: {exc}", file=sys.stderr)
        return False
    except CloudflareBlockError as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        return False
    except RateLimitError as exc:
        print(f"\nRate limited: {exc}", file=sys.stderr)
        print(
            "Try increasing --delay-min / --delay-max or wait before retrying.",
            file=sys.stderr,
        )
        return False
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        return False


def _read_batch_file(path):
    """Read URLs from a batch file, skipping blank lines and comments."""
    urls = []
    batch_path = Path(path)
    if not batch_path.is_file():
        raise FileNotFoundError(f"Batch file not found: {path}")
    with open(batch_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


_FFN_URL_RE = re.compile(r"https?://(?:www\.)?fanfiction\.net/s/\d+", re.I)
_FICWAD_URL_RE = re.compile(r"https?://(?:www\.)?ficwad\.com/story/\d+", re.I)
_AO3_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:archiveofourown\.org|ao3\.org)/works/\d+", re.I
)
_RR_URL_RE = re.compile(
    r"https?://(?:www\.)?royalroad\.com/fiction/\d+", re.I
)
_MM_URL_RE = re.compile(
    r"https?://(?:www\.)?mediaminer\.org/fanfic/"
    r"(?:view_st\.php/\d+|s/[^?#\s]+?/\d+)", re.I
)
_LIT_URL_RE = re.compile(
    r"https?://(?:www\.)?literotica\.com/s/[a-z0-9-]+", re.I
)


def _handle_search(args):
    """Interactive search mode: search the chosen site, display results, download on pick."""
    from .search import search_ao3, search_ffn, search_royalroad

    if args.site == "ao3":
        site_label = "archiveofourown.org"
        filters = {
            "rating": args.rating,
            "language": args.language,
            "complete": args.status,
            "crossover": args.crossover,
            "sort": args.sort,
            "fandom": args.fandom,
            "word_count": args.word_count,
            "character": args.character,
            "relationship": args.relationship,
            "single_chapter": args.single_chapter,
        }
        search_fn = search_ao3
    elif args.site == "royalroad":
        site_label = "royalroad.com"
        filters = {
            "status": args.status,
            "type": getattr(args, "rr_type", None),
            "order_by": getattr(args, "rr_order_by", None),
            "tags": getattr(args, "rr_tags", None),
        }
        search_fn = search_royalroad
    else:
        site_label = "fanfiction.net"
        filters = {
            "rating": args.rating,
            "language": args.language,
            "status": args.status,
            "genre": args.genre,
            "min_words": args.min_words,
            "crossover": args.crossover,
            "match": args.match,
        }
        search_fn = search_ffn
    filters = {k: v for k, v in filters.items() if v}

    print(f"Searching {site_label} for: {args.search}")
    if filters:
        print("Filters: " + ", ".join(f"{k}={v}" for k, v in filters.items()))
    print()
    try:
        results = search_fn(args.search, **filters)
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No results found.")
        sys.exit(0)

    for i, r in enumerate(results, 1):
        status_tag = " [Complete]" if r["status"] == "Complete" else ""
        print(f"  {i:>2}. {r['title']}")
        print(f"      by {r['author']} | {r['fandom']} | "
              f"{r['words']} words | {r['chapters']} ch | "
              f"Rated {r['rating']}{status_tag}")
        if r["summary"]:
            # Truncate long summaries
            s = r["summary"]
            if len(s) > 120:
                s = s[:117] + "..."
            print(f"      {s}")
        print()

    while True:
        try:
            choice = input(f"Enter a number (1-{len(results)}) to download, or 'q' to quit: ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        choice = choice.strip().lower()
        if choice == "q":
            sys.exit(0)

        try:
            idx = int(choice)
        except ValueError:
            print("Invalid input. Enter a number or 'q'.")
            continue

        if not 1 <= idx <= len(results):
            print(f"Pick a number between 1 and {len(results)}.")
            continue

        picked = results[idx - 1]
        print(f"\nDownloading: {picked['title']}")
        print(f"  {picked['url']}\n")

        if args.format is None:
            args.format = "epub"
        if args.output is None:
            args.output = "."

        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        ok = _download_one(picked["url"], args, output_dir)
        sys.exit(0 if ok else 1)


def _handle_update_all(args):
    """Scan a folder for previously-downloaded exports and update each."""
    from concurrent.futures import ThreadPoolExecutor

    folder = Path(args.update_all)
    if not folder.is_dir():
        print(f"Error: {folder} is not a directory.", file=sys.stderr)
        sys.exit(1)

    exts = (".epub", ".html", ".txt")
    iterator = folder.rglob("*") if args.recursive else folder.iterdir()
    files = sorted(
        p for p in iterator
        if p.is_file() and p.suffix.lower() in exts
    )
    if not files:
        where = "recursively in" if args.recursive else "in"
        print(f"No .epub, .html, or .txt files {where} {folder}.")
        sys.exit(0)

    workers = max(1, int(args.probe_workers or 5))
    mode_bits = []
    if args.recursive:
        mode_bits.append("recursive")
    if args.dry_run:
        mode_bits.append("dry-run")
    if args.skip_complete:
        mode_bits.append("skipping completed")
    mode_bits.append(f"{workers} probe worker{'s' if workers != 1 else ''}")
    mode = f" ({', '.join(mode_bits)})"
    print(f"Scanning {len(files)} files in {folder}{mode}...\n")

    updated = []
    up_to_date = []
    failed = []
    skipped = []
    would_update = []

    fmt_map = {".epub": "epub", ".html": "html", ".txt": "txt"}

    # Phase 1 (serial, fast): read local state. Anything that can be
    # resolved without a network call — missing source URL, unreadable
    # file, skip-complete — is decided here and never queues a probe.
    probe_queue = []
    for path in files:
        rel = str(path.relative_to(folder)) if args.recursive else path.name

        try:
            url = extract_source_url(path)
        except (ValueError, FileNotFoundError) as exc:
            print(f"  [skip] {rel}: no source URL ({exc})")
            skipped.append(rel)
            continue

        try:
            local = count_chapters(path)
        except Exception as exc:
            print(f"  [skip] {rel}: couldn't read ({exc})")
            skipped.append(rel)
            continue

        if local == 0:
            print(f"  [skip] {rel}: local chapter count is 0 (probably not an ffn-dl export)")
            skipped.append(rel)
            continue

        if args.skip_complete:
            try:
                status = extract_status(path)
            except Exception:
                status = ""
            if status.lower() == "complete":
                print(f"  [skip] {rel}: marked Complete ({local} chapters)")
                skipped.append(rel)
                continue

        probe_queue.append({"path": path, "rel": rel, "url": url, "local": local})

    # Phase 2 (concurrent): remote chapter-count probes.
    if probe_queue:
        print(f"\nProbing {len(probe_queue)} stories for new chapters...")

        def probe_one(entry):
            scraper = _build_scraper(entry["url"], args)
            return scraper.get_chapter_count(entry["url"])

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe_one, entry) for entry in probe_queue]
            for entry, fut in zip(probe_queue, futures):
                try:
                    entry["remote"] = fut.result()
                except (RateLimitError, CloudflareBlockError,
                        StoryNotFoundError, AO3LockedError, ValueError) as exc:
                    entry["error"] = exc
                except Exception as exc:
                    entry["error"] = exc
        print()

    # Phase 3 (serial): apply the decisions. Any actual downloads run
    # one-at-a-time so we don't stack parallel chapter fetches on a
    # single site — that's what the scrapers' per-request pacing guards.
    total = len(probe_queue)
    cancelled = False
    for i, entry in enumerate(probe_queue, 1):
        rel = entry["rel"]
        print(f"[{i}/{total}] {rel}")

        if "error" in entry:
            print(f"  Probe failed: {entry['error']}")
            failed.append(rel)
            continue

        local = entry["local"]
        remote = entry["remote"]
        if remote <= local:
            label = "up to date" if remote == local else f"remote has fewer chapters ({remote} < {local}) — leaving alone"
            print(f"  {local} local / {remote} remote — {label}")
            up_to_date.append(rel)
            continue

        new_count = remote - local
        print(f"  {local} local / {remote} remote — {new_count} new chapter(s)")

        if args.dry_run:
            would_update.append((rel, local, remote))
            continue

        path = entry["path"]
        args.format = fmt_map.get(path.suffix.lower(), "epub")
        args.output = str(path.parent)
        output_dir = Path(args.output)
        try:
            ok = _download_one(
                entry["url"], args, output_dir,
                update_path=path, existing_chapters=local,
            )
        except KeyboardInterrupt:
            print("\nCancelled.")
            cancelled = True
            break
        if ok:
            updated.append(rel)
        else:
            failed.append(rel)

    if cancelled:
        pass  # summary still printed below

    print(f"\n{'='*60}")
    if args.dry_run:
        print(
            f"Dry run — would update {len(would_update)}, "
            f"{len(up_to_date)} up to date, {len(failed)} failed, "
            f"{len(skipped)} skipped."
        )
        if would_update:
            print("Would update:")
            for name, local, remote in would_update:
                print(f"  {name}  ({local} -> {remote})")
    else:
        print(
            f"Update-all complete — {len(updated)} updated, "
            f"{len(up_to_date)} up to date, {len(failed)} failed, "
            f"{len(skipped)} skipped."
        )
    if failed:
        print("Failed:")
        for name in failed:
            print(f"  {name}")
    print('='*60)
    sys.exit(0 if not failed else 1)


def _handle_watch(args):
    """Clipboard watch mode: poll clipboard for FFN/FicWad URLs."""
    try:
        import pyperclip
    except ImportError:
        print(
            "Error: pyperclip is required for --watch mode.\n"
            "Install it with:  pip install ffn-dl[clipboard]",
            file=sys.stderr,
        )
        sys.exit(1)

    import time

    if args.format is None:
        args.format = "epub"
    if args.output is None:
        args.output = "."

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded = set()
    last_clip = ""

    print("Watching clipboard... paste a fanfiction.net or ficwad.com URL to download")
    print("Press Ctrl+C to stop.\n")

    try:
        # Grab current clipboard so we don't immediately trigger on old content
        try:
            last_clip = pyperclip.paste() or ""
        except Exception:
            last_clip = ""

        while True:
            time.sleep(2)
            try:
                clip = pyperclip.paste() or ""
            except Exception:
                continue

            if clip == last_clip:
                continue
            last_clip = clip

            # Check if clipboard contains a supported URL
            url = None
            for pattern in (_FFN_URL_RE, _FICWAD_URL_RE, _AO3_URL_RE,
                            _RR_URL_RE, _MM_URL_RE, _LIT_URL_RE):
                match = pattern.search(clip)
                if match:
                    url = match.group(0)
                    break

            if not url:
                continue

            if url in downloaded:
                continue

            downloaded.add(url)
            print(f"Detected URL: {url}")
            ok = _download_one(url, args, output_dir)
            if ok:
                print(f"\nDone. Still watching... ({len(downloaded)} downloaded so far)\n")
            else:
                print(f"\nFailed. Still watching...\n")

    except KeyboardInterrupt:
        print(f"\nStopped. Downloaded {len(downloaded)} stories this session.")
        sys.exit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ffn-dl",
        description="Download fanfiction from fanfiction.net and ficwad.com",
        epilog=(
            "Supported sites: fanfiction.net, ficwad.com, "
            "archiveofourown.org, royalroad.com, mediaminer.org, "
            "literotica.com\n"
            "Name template placeholders: "
            "{title} {author} {id} {words} {status} {rating} {language} {chapters}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "url",
        nargs="*",
        help=(
            "One or more story URLs or numeric IDs "
            "(e.g. https://www.fanfiction.net/s/12345, "
            "https://ficwad.com/story/76962, or just 12345)"
        ),
    )
    parser.add_argument(
        "-b",
        "--batch",
        metavar="FILE",
        help=(
            "Read URLs from a file (one per line; blank lines and "
            "lines starting with # are skipped)"
        ),
    )
    parser.add_argument(
        "-u",
        "--update",
        metavar="FILE",
        help="Update an existing file — reads source URL, downloads new chapters",
    )
    parser.add_argument(
        "-U",
        "--update-all",
        metavar="DIR",
        help=(
            "Update every .epub/.html/.txt in DIR. Uses a cheap chapter-count "
            "probe per story so unchanged fics cost one HTTP request."
        ),
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="With --update-all: descend into subdirectories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --update-all: report what would be updated, skipped, or "
            "is up to date, without downloading any new chapters"
        ),
    )
    parser.add_argument(
        "--skip-complete",
        action="store_true",
        help=(
            "With --update-all: skip stories whose local file is already "
            "marked Complete (saves the remote probe)"
        ),
    )
    parser.add_argument(
        "--probe-workers",
        type=int,
        default=5,
        metavar="N",
        help=(
            "Concurrent chapter-count probes during --update-all "
            "(default: 5; set to 1 to serialise)"
        ),
    )
    parser.add_argument(
        "--merge-series",
        action="store_true",
        help=(
            "When given an AO3 series URL, download every work and combine "
            "them into a single file instead of one file per work. Each work "
            "is rendered as a title chapter followed by its own chapters."
        ),
    )
    parser.add_argument(
        "-a",
        "--author",
        metavar="URL",
        help=(
            "Download all stories from an author page "
            "(e.g. https://www.fanfiction.net/u/123/Name, "
            "https://ficwad.com/a/Name)"
        ),
    )
    all_formats = sorted(EXPORTERS) + ["audio"]
    parser.add_argument(
        "-f",
        "--format",
        choices=all_formats,
        default=None,
        help="Output format (default: epub, or inferred from --update file)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: current directory, or --update file's dir)",
    )
    parser.add_argument(
        "-n",
        "--name",
        default=DEFAULT_TEMPLATE,
        metavar="TEMPLATE",
        help=(
            "Filename template (default: '%(default)s'). "
            "See --help footer for available placeholders."
        ),
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=None,
        metavar="SEC",
        help="Minimum delay between chapter requests (default: 2 FFN, 1 FicWad)",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=None,
        metavar="SEC",
        help="Maximum delay between chapter requests (default: 5 FFN, 3 FicWad)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum retries per request on rate-limit or error (default: 5)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Pause ~60s after every N chapter fetches "
            "(default: 20 on FFN, disabled on FicWad). "
            "Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--chapters",
        metavar="SPEC",
        help=(
            "Restrict download to specific chapters. "
            "SPEC is a comma-separated list of single chapters and/or "
            "ranges. Examples: '1-5', '1,3,5', '1-5,10', '20-', '-3'. "
            "'20-' means chapter 20 through the end; '-3' means 1 through 3."
        ),
    )
    parser.add_argument(
        "--use-wayback",
        action="store_true",
        help=(
            "If a story 404s or the site keeps failing, try fetching "
            "the latest archive.org snapshot instead. Useful for deleted "
            "fics and during site outages."
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable chapter caching (re-download everything)",
    )
    parser.add_argument(
        "--hr-as-stars",
        action="store_true",
        help=(
            "Replace each <hr/> scene break with a centred '* * *' marker "
            "in HTML/EPUB output (TXT output already renders hr as '* * *'). "
            "Useful for readers whose stylesheet draws hr as a barely-visible line."
        ),
    )
    parser.add_argument(
        "--strip-notes",
        action="store_true",
        help=(
            "Remove paragraphs that start with 'A/N', \"Author's Note\", etc. "
            "Heuristic — catches the common FFN pattern; AO3's structured "
            "notes are already excluded at scrape time."
        ),
    )
    parser.add_argument(
        "--send-to-kindle",
        metavar="EMAIL",
        help=(
            "After each successful download, email the exported file to "
            "EMAIL. Configure SMTP via SMTP_HOST / SMTP_USER / SMTP_PASSWORD "
            "(and optional SMTP_PORT / SMTP_FROM). EMAIL must be on Amazon's "
            "approved personal-document list for Kindle delivery."
        ),
    )
    parser.add_argument(
        "--clean-cache",
        action="store_true",
        help="Remove cached chapters after successful export",
    )
    parser.add_argument(
        "-s",
        "--search",
        metavar="QUERY",
        help="Search for stories matching QUERY (see --site to pick FFN, AO3, or Royal Road)",
    )
    parser.add_argument(
        "--site",
        choices=["ffn", "ao3", "royalroad"],
        default="ffn",
        help="Which site to search (default: ffn)",
    )
    # Search filters (only apply when --search is used). Values accepted
    # depend on --site; see the search module for the full tables.
    from .search import (
        FFN_GENRE, FFN_LANGUAGE, FFN_WORDS, AO3_RATING, AO3_SORT,
        RR_ORDER_BY, RR_STATUS, RR_TYPE,
    )
    parser.add_argument(
        "--rating",
        metavar="R",
        help=(
            "Rating filter. FFN: K, K+, T, M, K-T. "
            f"AO3: {', '.join(k for k in AO3_RATING if k != 'all')}."
        ),
    )
    parser.add_argument(
        "--language",
        metavar="LANG",
        help=(
            "Language filter. FFN: english, spanish, french, german, ... "
            "AO3: ISO code (e.g. en, fr)."
        ),
    )
    parser.add_argument(
        "--status",
        metavar="S",
        help=(
            "Completion status: in-progress, complete "
            "(mapped to AO3's 'complete' field automatically)."
        ),
    )
    parser.add_argument(
        "--genre",
        metavar="G",
        help=f"FFN-only: {', '.join(list(FFN_GENRE)[1:8])}, ... (see search.FFN_GENRE)",
    )
    parser.add_argument(
        "--min-words",
        metavar="N",
        help=f"FFN-only word-count bucket: {', '.join(list(FFN_WORDS)[1:])}",
    )
    parser.add_argument(
        "--crossover",
        metavar="X",
        help="Crossover filter: any, only, exclude",
    )
    parser.add_argument(
        "--match",
        metavar="M",
        help="FFN-only: match keywords in title or summary (any, title, summary)",
    )
    parser.add_argument(
        "--sort",
        metavar="S",
        help=f"AO3-only sort: {', '.join(list(AO3_SORT)[:4])}, ...",
    )
    parser.add_argument(
        "--fandom",
        metavar="NAME",
        help="AO3-only: filter by fandom name(s)",
    )
    parser.add_argument(
        "--word-count",
        metavar="RANGE",
        help="AO3-only word-count range, e.g. '<5000', '>10000', '1000-5000'",
    )
    parser.add_argument(
        "--character",
        metavar="NAME",
        help="AO3-only: filter by character name(s)",
    )
    parser.add_argument(
        "--relationship",
        metavar="NAME",
        help="AO3-only: filter by relationship tag(s)",
    )
    parser.add_argument(
        "--single-chapter",
        action="store_true",
        help="AO3-only: one-shots only",
    )
    parser.add_argument(
        "--rr-type",
        metavar="T",
        help="Royal Road-only story type: original / fanfiction / any",
    )
    parser.add_argument(
        "--rr-order-by",
        metavar="SORT",
        help=f"Royal Road-only sort: {', '.join(list(RR_ORDER_BY)[:5])}, ...",
    )
    parser.add_argument(
        "--rr-tags",
        metavar="TAGS",
        help="Royal Road-only: comma-separated tag list (e.g. 'progression,magic')",
    )
    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help=(
            "Watch clipboard for fanfiction URLs and download automatically "
            "(requires pyperclip: pip install ffn-dl[clipboard])"
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    # --- Search mode ---
    if args.search:
        _handle_search(args)
        return

    # --- Update-all folder mode ---
    if args.update_all:
        _handle_update_all(args)
        return

    # --- Clipboard watch mode ---
    if args.watch:
        _handle_watch(args)
        return

    # --- Resolve --update mode (single-file, no batch) ---
    if args.update:
        if args.batch:
            parser.error("--update and --batch cannot be used together")
        update_path = Path(args.update)
        url = extract_source_url(update_path)
        existing_chapters = count_chapters(update_path)
        fmt_map = {".epub": "epub", ".html": "html", ".txt": "txt"}
        if args.format is None:
            args.format = fmt_map.get(update_path.suffix.lower(), "epub")
        if args.output is None:
            args.output = str(update_path.parent)
        if args.format is None:
            args.format = "epub"
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            ok = _download_one(
                url,
                args,
                output_dir,
                update_path=update_path,
                existing_chapters=existing_chapters,
            )
        except KeyboardInterrupt:
            print("\nCancelled. Re-run the same command to resume.")
            sys.exit(130)
        sys.exit(0 if ok else 1)

    # --- Resolve --author mode ---
    if args.author:
        if args.batch:
            parser.error("--author and --batch cannot be used together")
        if args.format is None:
            args.format = "epub"
        if args.output is None:
            args.output = "."
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            author_name, story_urls = _scrape_author_stories(args.author, args)
        except (RateLimitError, CloudflareBlockError, StoryNotFoundError) as exc:
            print(f"Error fetching author page: {exc}", file=sys.stderr)
            sys.exit(1)

        if not story_urls:
            print("No stories found on the author page.", file=sys.stderr)
            sys.exit(1)

        print(f"Author: {author_name}")
        print(f"Found {len(story_urls)} stories.")
        urls = story_urls
        # Fall through to batch processing below

    else:
        # --- Collect URLs from positional args and --batch file ---
        urls = list(args.url) if args.url else []

        if args.batch:
            try:
                urls.extend(_read_batch_file(args.batch))
            except FileNotFoundError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)

        # --- --merge-series: handle series URLs specially ---
        if args.merge_series:
            series_urls = [u for u in urls if _is_series_url(u)]
            if series_urls:
                if args.format is None:
                    args.format = "epub"
                if args.output is None:
                    args.output = "."
                output_dir = Path(args.output)
                output_dir.mkdir(parents=True, exist_ok=True)
                ok = _handle_merge_series(series_urls, args, output_dir)
                # Remove series URLs from further processing
                urls = [u for u in urls if not _is_series_url(u)]
                if not urls:
                    sys.exit(0 if ok else 1)

        # Expand any author or series URLs found in positional args
        expanded = []
        for url in urls:
            if _is_author_url(url):
                try:
                    author_name, story_urls = _scrape_author_stories(url, args)
                except (RateLimitError, CloudflareBlockError, StoryNotFoundError) as exc:
                    print(f"Error fetching author page {url}: {exc}", file=sys.stderr)
                    sys.exit(1)
                if not story_urls:
                    print(f"No stories found on author page: {url}", file=sys.stderr)
                    sys.exit(1)
                print(f"Author: {author_name}")
                print(f"Found {len(story_urls)} stories.")
                expanded.extend(story_urls)
            elif _is_series_url(url):
                try:
                    series_name, work_urls = _scrape_series_works(url, args)
                except (RateLimitError, CloudflareBlockError, StoryNotFoundError) as exc:
                    print(f"Error fetching series page {url}: {exc}", file=sys.stderr)
                    sys.exit(1)
                if not work_urls:
                    print(f"No works found in series: {url}", file=sys.stderr)
                    sys.exit(1)
                print(f"Series: {series_name}")
                print(f"Found {len(work_urls)} works.")
                expanded.extend(work_urls)
            else:
                expanded.append(url)
        urls = expanded

        if not urls:
            parser.error("either a URL, --batch FILE, --update FILE, or --author URL is required")

    if args.format is None:
        args.format = "epub"
    if args.output is None:
        args.output = "."

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Single URL: preserve original exit-code behaviour ---
    if len(urls) == 1:
        try:
            ok = _download_one(urls[0], args, output_dir)
        except KeyboardInterrupt:
            print("\nCancelled. Re-run the same command to resume.")
            sys.exit(130)
        sys.exit(0 if ok else 1)

    # --- Multiple URLs: batch mode with summary ---
    succeeded = 0
    failed = 0
    failures = []

    try:
        for i, url in enumerate(urls, 1):
            print(f"\n{'='*60}")
            print(f"[{i}/{len(urls)}] {url}")
            print(f"{'='*60}")
            ok = _download_one(url, args, output_dir)
            if ok:
                succeeded += 1
            else:
                failed += 1
                failures.append(url)
    except KeyboardInterrupt:
        print("\nCancelled.")
        # Count remaining URLs as not attempted
        remaining = len(urls) - (succeeded + failed)
        print(f"\n{'='*60}")
        print(f"Batch interrupted — {succeeded} succeeded, {failed} failed, "
              f"{remaining} not attempted.")
        if failures:
            print("Failed URLs:")
            for u in failures:
                print(f"  {u}")
        sys.exit(130)

    print(f"\n{'='*60}")
    print(f"Batch complete — {succeeded} succeeded, {failed} failed "
          f"out of {len(urls)} total.")
    if failures:
        print("Failed URLs:")
        for u in failures:
            print(f"  {u}")
    print(f"{'='*60}")
    sys.exit(0 if failed == 0 else 1)
