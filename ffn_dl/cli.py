"""Command-line interface for ffn-dl."""

import argparse
import logging
import re
import sys
from pathlib import Path

from .exporters import DEFAULT_TEMPLATE, EXPORTERS
from .ficwad import FicWadScraper
from .scraper import (
    CloudflareBlockError,
    FFNScraper,
    RateLimitError,
    StoryNotFoundError,
)
from .updater import count_chapters, extract_source_url


def _detect_site(url):
    """Return the appropriate scraper class based on the URL."""
    text = str(url).lower()
    if "ficwad.com" in text:
        return FicWadScraper
    return FFNScraper


def _is_author_url(url):
    """Return True if the URL points to an author page on any supported site."""
    return FFNScraper.is_author_url(url) or FicWadScraper.is_author_url(url)


def _scrape_author_stories(url, args):
    """Scrape an author page and return (author_name, [story_urls])."""
    scraper = _build_scraper(url, args)
    return scraper.scrape_author_stories(url)


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

        story = scraper.download(
            url,
            progress_callback=progress,
            skip_chapters=existing_chapters,
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
            story = scraper.download(url, skip_chapters=0)

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
            path = exporter(story, str(output_dir), template=args.name)
        print(f"\nSaved to: {path}")

        if args.clean_cache:
            scraper.clean_cache(story_id)

        return True

    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False
    except StoryNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
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


def _handle_search(args):
    """Interactive search mode: search FFN, display results, download on pick."""
    from .search import search_ffn

    filters = {
        "rating": args.rating,
        "language": args.language,
        "status": args.status,
        "genre": args.genre,
        "min_words": args.min_words,
        "crossover": args.crossover,
        "match": args.match,
    }
    filters = {k: v for k, v in filters.items() if v}

    print(f"Searching fanfiction.net for: {args.search}")
    if filters:
        print("Filters: " + ", ".join(f"{k}={v}" for k, v in filters.items()))
    print()
    try:
        results = search_ffn(args.search, **filters)
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

            # Check if clipboard contains an FFN or FicWad URL
            url = None
            ffn_match = _FFN_URL_RE.search(clip)
            ficwad_match = _FICWAD_URL_RE.search(clip)

            if ffn_match:
                url = ffn_match.group(0)
            elif ficwad_match:
                url = ficwad_match.group(0)

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
            "Supported sites: fanfiction.net, ficwad.com\n"
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
        "--no-cache",
        action="store_true",
        help="Disable chapter caching (re-download everything)",
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
        help="Search fanfiction.net for stories matching QUERY",
    )
    # Search filters (only apply when --search is used)
    from .search import (
        FFN_CROSSOVER, FFN_GENRE, FFN_LANGUAGE, FFN_MATCH,
        FFN_RATING, FFN_STATUS, FFN_WORDS,
    )
    parser.add_argument(
        "--rating",
        choices=list(FFN_RATING),
        metavar="R",
        help=f"Filter by rating ({', '.join(FFN_RATING)})",
    )
    parser.add_argument(
        "--language",
        metavar="LANG",
        help=f"Filter by language (e.g. {', '.join(list(FFN_LANGUAGE)[:6])}, ...)",
    )
    parser.add_argument(
        "--status",
        choices=list(FFN_STATUS),
        metavar="S",
        help=f"Filter by completion status ({', '.join(FFN_STATUS)})",
    )
    parser.add_argument(
        "--genre",
        metavar="G",
        help=f"Filter by genre (e.g. {', '.join(list(FFN_GENRE)[:6])}, ...)",
    )
    parser.add_argument(
        "--min-words",
        choices=list(FFN_WORDS),
        metavar="N",
        help=f"Filter by word count ({', '.join(FFN_WORDS)})",
    )
    parser.add_argument(
        "--crossover",
        choices=list(FFN_CROSSOVER),
        metavar="X",
        help=f"Crossover filter ({', '.join(FFN_CROSSOVER)})",
    )
    parser.add_argument(
        "--match",
        choices=list(FFN_MATCH),
        metavar="M",
        help=f"Match keywords against ({', '.join(FFN_MATCH)})",
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

        # Expand any author URLs found in positional args
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
