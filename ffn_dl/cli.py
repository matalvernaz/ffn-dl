"""Command-line interface for ffn-dl."""

import argparse
import logging
import sys
import threading
from pathlib import Path

from .ao3 import AO3LockedError
from .exporters import DEFAULT_TEMPLATE, EXPORTERS, check_format_deps
from .erotica import LiteroticaScraper
from .models import parse_chapter_spec
from .scraper import (
    CloudflareBlockError,
    RateLimitError,
    StoryNotFoundError,
)
from .sites import (
    detect_scraper as _detect_site,
    extract_story_url,
    is_author_url as _is_author_url,
    is_series_url as _is_series_url,
)
from .updater import count_chapters, extract_source_url, extract_status
from .wattpad import WattpadPaidStoryError

logger = logging.getLogger(__name__)

# Errors that a per-item download can raise and that we want to handle
# by recording the failure and moving on, rather than aborting the
# whole batch. Kept narrower than bare ``Exception`` so programming
# bugs (AttributeError, KeyError on missing fields) still surface.
_DOWNLOAD_EXPECTED_ERRORS = (
    RateLimitError,
    CloudflareBlockError,
    StoryNotFoundError,
    AO3LockedError,
    WattpadPaidStoryError,
    ValueError,
    OSError,
    ImportError,
)


def _scrape_author_stories(
    url: str, args: argparse.Namespace,
) -> tuple[str, list[str]]:
    """Scrape an author page and return (author_name, [story_urls])."""
    scraper = _build_scraper(url, args)
    return scraper.scrape_author_stories(url)


def _scrape_series_works(
    url: str, args: argparse.Namespace,
) -> tuple[str, list[str]]:
    """Scrape an AO3 series and return (series_name, [work_urls])."""
    scraper = _build_scraper(url, args)
    return scraper.scrape_series_works(url)


def _merge_stories(series_name: str, series_url: str, stories: list):
    """Combine a series of Story objects into one Story for single-file export.

    The merged Story gets a computed title (the series name), a
    combined author (single author if all works share one, otherwise
    comma-joined), and a per-work summary block. Each source work
    becomes a title chapter followed by its own chapters, preserving
    chapter numbering across the merged document so exporters can
    render a proper table of contents.
    """
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


def _handle_merge_series(
    series_urls: list[str],
    args: argparse.Namespace,
    output_dir: Path,
) -> bool:
    """Download each series URL (AO3 or Literotica), merge its works, export as one file."""
    try:
        check_format_deps(args.format)
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        return False
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
            except _DOWNLOAD_EXPECTED_ERRORS as exc:
                logger.debug("Series part download failed: %s", exc, exc_info=True)
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
                merged, str(output_dir),
                progress_callback=audio_progress,
                speech_rate=args.speech_rate,
                attribution_backend=args.attribution,
                attribution_model_size=args.attribution_model_size,
                strip_notes=args.strip_notes,
                hr_as_stars=args.hr_as_stars,
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


def _handle_merge_parts(
    series_name: str,
    series_url: str,
    work_urls: list[str],
    args: argparse.Namespace,
    output_dir: Path,
) -> bool:
    """Download an explicit list of work URLs and merge them into one file.
    Used for Literotica-style "series" detected from search-result titles.
    Tries to resolve the anchor part's canonical /series/se/<id> first so
    chapters that didn't appear in the search are still included; falls
    back to the passed-in work URLs if no series link can be found.
    """
    if not work_urls:
        print(f"No parts to merge for {series_name}.", file=sys.stderr)
        return False

    try:
        check_format_deps(args.format)
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        return False

    # Resolve the anchor part to its canonical series (Literotica only).
    try:
        anchor_scraper = _build_scraper(work_urls[0], args)
        if isinstance(anchor_scraper, LiteroticaScraper):
            resolved = anchor_scraper.resolve_series_url(work_urls[0])
            if resolved:
                print(f"Resolved full series: {resolved}")
                try:
                    s_name, s_urls = anchor_scraper.scrape_series_works(resolved)
                    if s_urls:
                        series_url = resolved
                        series_name = s_name or series_name
                        work_urls = s_urls
                except _DOWNLOAD_EXPECTED_ERRORS as exc:
                    logger.debug("Series scrape failed", exc_info=True)
                    print(
                        f"  (Series scrape failed: {exc}); using known parts.",
                        file=sys.stderr,
                    )
    except _DOWNLOAD_EXPECTED_ERRORS as exc:
        logger.debug("Series URL resolution failed", exc_info=True)
        print(f"  (Couldn't resolve series URL: {exc})", file=sys.stderr)

    print(f"\nSeries: {series_name}")
    print(f"Downloading and merging {len(work_urls)} parts...\n")
    stories = []
    for i, work_url in enumerate(work_urls, 1):
        print(f"  [{i}/{len(work_urls)}] {work_url}")
        def progress(current, total, title, cached):
            tag = " (cached)" if cached else ""
            print(f"      [{current}/{total}] {title}{tag}")
        work_scraper = _build_scraper(work_url, args)
        try:
            stories.append(
                work_scraper.download(work_url, progress_callback=progress)
            )
        except _DOWNLOAD_EXPECTED_ERRORS as exc:
            logger.debug("Merge-parts download failed", exc_info=True)
            print(f"    Error: {exc}", file=sys.stderr)

    if not stories:
        print(f"Nothing downloaded for {series_name}.", file=sys.stderr)
        return False

    merged = _merge_stories(series_name, series_url, stories)
    print(f"\n  Merged {len(stories)} parts / {len(merged.chapters)} sections")
    if args.format == "audio":
        from .tts import generate_audiobook
        def audio_progress(current, total, title):
            print(f"  Synthesizing [{current}/{total}] {title}")
        path = generate_audiobook(
            merged, str(output_dir),
            progress_callback=audio_progress,
            speech_rate=args.speech_rate,
            attribution_backend=args.attribution,
            attribution_model_size=args.attribution_model_size,
            strip_notes=args.strip_notes,
            hr_as_stars=args.hr_as_stars,
        )
    else:
        exporter = EXPORTERS[args.format]
        path = exporter(
            merged, str(output_dir), template=args.name,
            hr_as_stars=args.hr_as_stars,
            strip_notes=args.strip_notes,
        )
    print(f"  Saved: {path}")
    return True


def _apply_library_autosort(args) -> None:
    """If no explicit --output was passed and a library is configured,
    route fresh downloads into it. Sets args.output to the library
    root and stashes the template + misc folder on args so
    _download_one can compute the per-story subdirectory once the
    story metadata is known.

    No-op when the user passed --output or when the library path pref
    is empty. Safe to call multiple times.
    """
    if args.output is not None:
        return
    from .library.template import DEFAULT_MISC_FOLDER, DEFAULT_TEMPLATE
    from .prefs import (
        KEY_LIBRARY_MISC_FOLDER,
        KEY_LIBRARY_PATH,
        KEY_LIBRARY_PATH_TEMPLATE,
        Prefs,
    )

    prefs = Prefs()
    library_path = (prefs.get(KEY_LIBRARY_PATH, "") or "").strip()
    if not library_path:
        return

    args.output = library_path
    args._library_autosort = True
    args._library_template = (
        prefs.get(KEY_LIBRARY_PATH_TEMPLATE) or DEFAULT_TEMPLATE
    )
    args._library_misc = (
        prefs.get(KEY_LIBRARY_MISC_FOLDER) or DEFAULT_MISC_FOLDER
    )


def _library_subdir_for(story, args) -> Path | None:
    """Compute the library-relative directory for a just-scraped story.

    Returns None when auto-sort isn't enabled on these args (caller
    should use output_dir as-is). Uses only the directory part of
    the library template — the filename still comes from the usual
    name template so --name overrides keep working.
    """
    if not getattr(args, "_library_autosort", False):
        return None
    from .library.template import render
    from .updater import FileMetadata

    category = story.metadata.get("category")
    fandoms: list[str] = []
    if category:
        # AO3 sometimes comma-separates crossover fandoms; other
        # scrapers hand us a single string. Splitting on comma mirrors
        # the exporter's own "category" rendering logic.
        fandoms = [f.strip() for f in category.split(",") if f.strip()]

    md = FileMetadata(
        title=story.title,
        author=story.author,
        fandoms=fandoms,
        rating=story.metadata.get("rating"),
        status=story.metadata.get("status"),
        format=args.format or "epub",
    )
    full = render(
        md,
        template=args._library_template,
        misc_folder=args._library_misc,
    )
    return full.parent


def _build_scraper(url: str, args: argparse.Namespace):
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


def _download_one(
    url: str,
    args: argparse.Namespace,
    output_dir: Path,
    *,
    update_path: Path | None = None,
    existing_chapters: int = 0,
) -> bool:
    """Download and export a single story. Returns True on success, False on error."""
    scraper = _build_scraper(url, args)

    def progress(current, total, title, cached):
        tag = " (cached)" if cached else ""
        print(f"  [{current}/{total}] {title}{tag}")

    try:
        check_format_deps(args.format)
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
        words = story.metadata.get("words", "")
        if not words:
            from .exporters import _count_story_words
            counted = _count_story_words(story)
            words = f"{counted:,}" if counted else "?"
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

        # Library auto-sort: for fresh downloads only, route into
        # <library>/<fandom>/... based on the story's metadata.
        # Updates stay where they were (update_path already points to
        # the existing file's parent).
        if update_path is None:
            subdir = _library_subdir_for(story, args)
            if subdir is not None:
                output_dir = output_dir / subdir
                output_dir.mkdir(parents=True, exist_ok=True)

        if args.format == "audio":
            from .tts import generate_audiobook

            def audio_progress(current, total, title):
                print(f"  Synthesizing [{current}/{total}] {title}")

            print("\nGenerating audiobook...")
            path = generate_audiobook(
                story, str(output_dir),
                progress_callback=audio_progress,
                speech_rate=args.speech_rate,
                attribution_backend=args.attribution,
                attribution_model_size=args.attribution_model_size,
                strip_notes=args.strip_notes,
                hr_as_stars=args.hr_as_stars,
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
            except (OSError, RuntimeError) as exc:
                logger.debug("Kindle email failed", exc_info=True)
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
    except WattpadPaidStoryError as exc:
        print(f"Paywalled: {exc}", file=sys.stderr)
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


def _read_batch_file(path: str) -> list[str]:
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


def _build_search_spec(args: argparse.Namespace):
    """Return (site_label, search_fn, filters) for the chosen --site.

    Each site carries a different flag set; this function maps the
    argparse namespace to the keyword dict that the per-site search
    function expects. Unset filter keys are dropped so we don't pass
    ``None`` through to the downstream URL builders.
    """
    from .search import (
        search_ao3, search_ffn, search_literotica, search_royalroad,
        search_wattpad,
    )

    if args.site == "ao3":
        site_label = "archiveofourown.org"
        filters = {
            "rating": args.rating,
            "language": args.language,
            "complete": args.status,
            "crossover": args.crossover,
            "category": getattr(args, "ao3_category", None),
            "sort": args.sort,
            "fandom": args.fandom,
            "word_count": args.word_count,
            "character": args.character,
            "relationship": args.relationship,
            "freeform": getattr(args, "ao3_freeform", None),
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
            "genres": getattr(args, "rr_genres", None),
            "warnings": getattr(args, "rr_warnings", None),
            "min_words": getattr(args, "rr_min_words", None),
            "max_words": getattr(args, "rr_max_words", None),
            "min_pages": getattr(args, "rr_min_pages", None),
            "max_pages": getattr(args, "rr_max_pages", None),
            "min_rating": getattr(args, "rr_min_rating", None),
            "list": getattr(args, "rr_list", None),
        }
        search_fn = search_royalroad
    elif args.site == "literotica":
        site_label = "literotica.com (tag browse)"
        filters = {"category": getattr(args, "lit_category", None)}
        search_fn = search_literotica
        if getattr(args, "lit_page", None):
            args.start_page = max(args.start_page, int(args.lit_page))
    elif args.site == "wattpad":
        site_label = "wattpad.com"
        filters = {
            "mature": getattr(args, "wp_mature", None),
            "completed": getattr(args, "wp_completed", None),
        }
        search_fn = search_wattpad
    else:
        site_label = "fanfiction.net"
        filters = {
            "rating": args.rating,
            "language": args.language,
            "status": args.status,
            "genre": args.genre,
            "genre2": getattr(args, "genre2", None),
            "min_words": args.min_words,
            "crossover": args.crossover,
            "match": args.match,
            "sort": args.sort,
        }
        search_fn = search_ffn
    filters = {k: v for k, v in filters.items() if v}
    return site_label, search_fn, filters


def _collapse_results(raw_results: list, site: str) -> list:
    """Apply per-site series collapsing. Sites without a series concept
    (FFN, Royal Road, Wattpad) return the raw list unchanged."""
    from .search import collapse_ao3_series, collapse_literotica_series

    if site == "ao3":
        return collapse_ao3_series(raw_results)
    if site == "literotica":
        return collapse_literotica_series(raw_results)
    return list(raw_results)


def _print_search_results(results: list, start_idx: int = 1) -> None:
    """Render the search results list the interactive prompt picks from."""
    for i, r in enumerate(results, start=start_idx):
        if r.get("is_series"):
            parts = len(r.get("series_parts") or [])
            print(f"  {i:>2}. {r['title']}  [Series · {parts} part(s) seen]")
            print(f"      by {r.get('author', '')} | {r.get('fandom', '')}")
        else:
            status_tag = " [Complete]" if r.get("status") == "Complete" else ""
            print(f"  {i:>2}. {r['title']}")
            print(
                f"      by {r['author']} | {r['fandom']} | "
                f"{r['words']} words | {r['chapters']} ch | "
                f"Rated {r['rating']}{status_tag}"
            )
        summary = r.get("summary") or ""
        if summary:
            s = summary if len(summary) <= 120 else summary[:117] + "..."
            print(f"      {s}")
        print()


def _prompt_search_choice(results: list):
    """Prompt for a numeric pick, 'm' for more, or 'q' to quit.

    Returns an integer index (1-based), the string ``"more"``, or
    calls ``sys.exit(0)`` on quit / Ctrl-C — the search loop has no
    fallback path if the user bails out.
    """
    prompt = (
        f"Enter a number (1-{len(results)}) to download, 'm' to load more, "
        f"or 'q' to quit: "
    )
    while True:
        try:
            choice = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        choice = choice.strip().lower()
        if choice == "q":
            sys.exit(0)
        if choice in ("m", "more"):
            return "more"
        try:
            idx = int(choice)
        except ValueError:
            print("Invalid input. Enter a number, 'm', or 'q'.")
            continue
        if not 1 <= idx <= len(results):
            print(f"Pick a number between 1 and {len(results)}.")
            continue
        return idx


def _download_picked_result(picked: dict, args: argparse.Namespace) -> bool:
    """Download one search-pick (work, series, or multi-part) via the
    appropriate handler. Returns the success flag from that handler."""
    print(f"\nDownloading: {picked['title']}")
    print(f"  {picked['url']}\n")

    if args.format is None:
        args.format = "epub"
    if args.output is None:
        args.output = "."

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if picked.get("is_series"):
        args.merge_series = True
        if picked.get("parts_only"):
            part_urls = [
                p["url"] for p in (picked.get("series_parts") or [])
                if p.get("url")
            ]
            return _handle_merge_parts(
                picked.get("title") or "Series",
                picked.get("url") or "",
                part_urls,
                args,
                output_dir,
            )
        return _handle_merge_series([picked["url"]], args, output_dir)
    return _download_one(picked["url"], args, output_dir)


def _handle_search(args: argparse.Namespace) -> None:
    """Interactive search mode: search the chosen site, display results, download on pick."""
    from .search import fetch_until_limit

    site_label, search_fn, filters = _build_search_spec(args)

    query_desc = args.search if args.search else "(no query — list browse)"
    print(f"Searching {site_label} for: {query_desc}")
    if filters:
        print("Filters: " + ", ".join(f"{k}={v}" for k, v in filters.items()))
    print()

    limit = max(1, int(args.limit))
    try:
        raw_fetched, next_page = fetch_until_limit(
            search_fn, args.search,
            limit=limit, start_page=args.start_page, **filters,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not raw_fetched:
        print("No results found.")
        sys.exit(0)

    # Keep the raw uncollapsed list so load-more can re-collapse the
    # full set — series parts that cross page boundaries need to see
    # each other to group correctly.
    raw_results = list(raw_fetched)
    results = _collapse_results(raw_results, args.site)
    _print_search_results(results)

    while True:
        picked_n = _prompt_search_choice(results)
        if picked_n == "more":
            try:
                more_raw, next_page = fetch_until_limit(
                    search_fn, args.search,
                    limit=limit, start_page=next_page, **filters,
                )
            except (RuntimeError, ValueError) as exc:
                print(f"Error loading more: {exc}", file=sys.stderr)
                continue
            if not more_raw:
                print("(No more results.)")
                continue
            raw_results.extend(more_raw)
            results = _collapse_results(raw_results, args.site)
            # Reprint the full list so numbering matches the merged view.
            print()
            _print_search_results(results)
            continue

        picked = results[picked_n - 1]
        ok = _download_picked_result(picked, args)
        sys.exit(0 if ok else 1)


def _handle_update_all(args: argparse.Namespace) -> None:
    """Scan a folder for previously-downloaded exports and update each."""
    folder = Path(args.update_all)
    if not folder.is_dir():
        print(f"Error: {folder} is not a directory.", file=sys.stderr)
        sys.exit(1)

    try:
        check_format_deps(args.format)
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        sys.exit(1)

    iterator = folder.rglob("*") if args.recursive else folder.iterdir()
    files = sorted(
        p for p in iterator
        if p.is_file() and p.suffix.lower() in _FMT_MAP
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

    skipped: list[str] = []

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
        except (OSError, ValueError) as exc:
            logger.debug("count_chapters failed for %s", path, exc_info=True)
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
            except (OSError, ValueError) as exc:
                logger.debug("extract_status failed for %s", path, exc_info=True)
                status = ""
            if status.lower() == "complete":
                print(f"  [skip] {rel}: marked Complete ({local} chapters)")
                skipped.append(rel)
                continue

        probe_queue.append({"path": path, "rel": rel, "url": url, "local": local})

    exit_code = _run_update_queue(
        probe_queue, args, workers, skipped_count=len(skipped),
        label="Update-all",
    )
    sys.exit(exit_code)


_FMT_MAP = {".epub": "epub", ".html": "html", ".txt": "txt"}


def _run_update_queue(
    probe_queue: list[dict],
    args,
    workers: int,
    *,
    skipped_count: int,
    label: str = "Update-all",
    progress=print,
) -> int:
    """Run the probe + download cycle on a pre-built queue.

    ``probe_queue`` entries need ``path`` (absolute), ``rel`` (display
    name), ``url``, and ``local`` (existing chapter count). Phase 1
    (reading each of those from disk or from the library index) is
    the caller's job; this helper owns Phase 2 (concurrent remote
    probes), Phase 3 (serial downloads), and the summary emission.

    ``progress`` is a one-arg callable that receives each line of
    output. Defaults to ``print`` for CLI use; the GUI passes its own
    callback that marshals onto the main thread.

    Returns the exit code: 0 on success, 1 if any story failed.
    """
    from concurrent.futures import ThreadPoolExecutor

    updated: list[str] = []
    up_to_date: list[str] = []
    failed: list[str] = []
    would_update: list[tuple[str, int, int]] = []

    # Phase 2 (concurrent): remote chapter-count probes.
    #
    # Partition by site class so we can (a) share one scraper per site
    # across every probe, which reuses its curl_cffi HTTP/2 connection
    # and skips the ~300–600 ms TLS handshake after the first request;
    # and (b) honour the site's own ``concurrency`` attribute — FFN
    # captcha-bans on bulk regardless of pacing, so its group must
    # stay at 1 worker even when ``--probe-workers`` is higher. The
    # global ``workers`` value is now an upper cap, not a fan-out count.
    if probe_queue:
        total = len(probe_queue)
        progress(f"\nProbing {total} stories for new chapters...")

        _PROBE_EXPECTED_ERRORS = (
            RateLimitError, CloudflareBlockError, StoryNotFoundError,
            AO3LockedError, ValueError,
        )

        by_site: dict[type, list[dict]] = {}
        for entry in probe_queue:
            site_cls = _detect_site(entry["url"])
            by_site.setdefault(site_cls, []).append(entry)

        # Progress output during Phase 2. Without this, a library with
        # 700+ FFN stories goes silent for an hour+ while the serial
        # 6-second-floor probes grind through — the user can't tell
        # whether the app has hung or is just waiting on FFN's rate
        # limit. One line per probe shows liveness and lets them
        # estimate remaining time. Lock-guarded because probe_entry
        # runs inside ThreadPoolExecutor workers.
        probe_progress_lock = threading.Lock()
        completed_count = [0]

        def probe_entry(scraper, entry):
            try:
                entry["remote"] = scraper.get_chapter_count(entry["url"])
                outcome = f"{entry['remote']} chapter(s) upstream"
            except _PROBE_EXPECTED_ERRORS as exc:
                entry["error"] = exc
                outcome = f"probe failed: {exc}"
            except (OSError, RuntimeError) as exc:
                logger.debug("Chapter-count probe failed", exc_info=True)
                entry["error"] = exc
                outcome = f"probe failed: {exc}"
            with probe_progress_lock:
                completed_count[0] += 1
                progress(
                    f"  [probe {completed_count[0]}/{total}] "
                    f"{entry['rel']}: {outcome}"
                )

        def run_site_group(site_cls, entries):
            scraper = _build_scraper(entries[0]["url"], args)
            site_workers = max(1, min(workers, scraper.concurrency))
            progress(
                f"  Probing {len(entries)} {site_cls.site_name} "
                f"stor{'y' if len(entries) == 1 else 'ies'} "
                f"(concurrency={site_workers})..."
            )
            with ThreadPoolExecutor(
                max_workers=site_workers,
                thread_name_prefix=f"probe-{site_cls.site_name}",
            ) as pool:
                for _ in pool.map(
                    lambda e: probe_entry(scraper, e), entries,
                ):
                    pass

        if len(by_site) == 1:
            cls, entries = next(iter(by_site.items()))
            run_site_group(cls, entries)
        else:
            # Run every site group in parallel so a slow-rate-limited
            # group (e.g. FFN, serialised) doesn't gate the others.
            with ThreadPoolExecutor(
                max_workers=len(by_site),
                thread_name_prefix="probe-site",
            ) as outer:
                site_futures = [
                    outer.submit(run_site_group, cls, entries)
                    for cls, entries in by_site.items()
                ]
                for fut in site_futures:
                    fut.result()
        progress("")

    # Phase 3 (serial): apply the decisions. Any actual downloads run
    # one-at-a-time so we don't stack parallel chapter fetches on a
    # single site — that's what the scrapers' per-request pacing guards.
    total = len(probe_queue)
    cancelled = False
    for i, entry in enumerate(probe_queue, 1):
        rel = entry["rel"]
        progress(f"[{i}/{total}] {rel}")

        if "error" in entry:
            progress(f"  Probe failed: {entry['error']}")
            failed.append(rel)
            continue

        local = entry["local"]
        remote = entry["remote"]
        if remote <= local:
            msg = (
                "up to date"
                if remote == local
                else f"remote has fewer chapters ({remote} < {local}) — leaving alone"
            )
            progress(f"  {local} local / {remote} remote — {msg}")
            up_to_date.append(rel)
            continue

        new_count = remote - local
        progress(
            f"  {local} local / {remote} remote — {new_count} new chapter(s)"
        )

        if args.dry_run:
            would_update.append((rel, local, remote))
            continue

        path = entry["path"]
        args.format = _FMT_MAP.get(path.suffix.lower(), "epub")
        args.output = str(path.parent)
        output_dir = Path(args.output)
        try:
            ok = _download_one(
                entry["url"], args, output_dir,
                update_path=path, existing_chapters=local,
            )
        except KeyboardInterrupt:
            progress("\nCancelled.")
            cancelled = True
            break
        if ok:
            updated.append(rel)
        else:
            failed.append(rel)

    if cancelled:
        pass  # summary still emitted below

    progress(f"\n{'='*60}")
    if args.dry_run:
        progress(
            f"Dry run — would update {len(would_update)}, "
            f"{len(up_to_date)} up to date, {len(failed)} failed, "
            f"{skipped_count} skipped."
        )
        if would_update:
            progress("Would update:")
            for name, local, remote in would_update:
                progress(f"  {name}  ({local} -> {remote})")
    else:
        progress(
            f"{label} complete — {len(updated)} updated, "
            f"{len(up_to_date)} up to date, {len(failed)} failed, "
            f"{skipped_count} skipped."
        )
    if failed:
        progress("Failed:")
        for name in failed:
            progress(f"  {name}")
    progress('='*60)
    return 0 if not failed else 1


def _handle_scan_library(args: argparse.Namespace) -> None:
    """Scan a directory, record findings in the library index."""
    from .library.scanner import scan

    root = Path(args.scan_library)
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {root}...")
    # Library scans always recurse — a library is by definition a tree.
    # The --recursive flag is kept only for --update-all's existing
    # per-folder semantics.
    result = scan(
        root,
        recursive=True,
        clear_existing=args.clear_library,
    )
    print(
        f"Scanned {result.total_files} file(s): "
        f"{result.identified_via_url} tracked by URL, "
        f"{result.ambiguous} indexed-only (no embedded URL — run "
        f"--review-library to resolve), "
        f"{result.errors} error(s)."
    )
    if result.duplicates:
        print(
            f"{result.duplicates} file(s) share a source URL with another "
            "copy on disk. The index tracks a primary path per story and "
            "records the extras in `duplicate_relpaths`; review and delete "
            "the copy you don't want."
        )
        _print_duplicate_pairs(result.root)
    if result.error_files:
        print("Errors:")
        for path, msg in result.error_files[:20]:
            try:
                rel = path.relative_to(root.resolve())
            except ValueError:
                rel = path
            print(f"  {rel}: {msg}")
        if len(result.error_files) > 20:
            print(f"  ... and {len(result.error_files) - 20} more")
    sys.exit(0 if result.errors == 0 else 1)


# How many duplicate pairs to print inline before falling back to
# "… and N more" to keep a 800-file library scan's output readable.
_MAX_INLINE_DUPLICATE_PAIRS = 20


def _print_duplicate_pairs(root: Path) -> None:
    """Print ``primary -> duplicate`` pairs for the library at ``root``.

    Reads from the on-disk index rather than the scan result because
    the scanner doesn't keep a per-entry log — the index is where the
    ``duplicate_relpaths`` list was written, so that's where we read
    it back from.
    """
    from .library.index import LibraryIndex

    idx = LibraryIndex.load()
    printed = 0
    total = 0
    for url, entry in idx.stories_in(root.resolve()):
        dupes = entry.get("duplicate_relpaths") or []
        if not dupes:
            continue
        primary = entry.get("relpath") or "(unknown)"
        for dup in dupes:
            total += 1
            if printed < _MAX_INLINE_DUPLICATE_PAIRS:
                print(f"  {primary}  <->  {dup}")
                printed += 1
    remaining = total - printed
    if remaining > 0:
        print(f"  ... and {remaining} more")


def _handle_review_library(args: argparse.Namespace) -> None:
    """Interactive TUI for promoting untrackable library entries."""
    from .library.index import LibraryIndex
    from .library.review import promote_untrackable, untrackable_for_root

    root = Path(args.review_library)
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)
    root_resolved = root.resolve()

    idx = LibraryIndex.load()
    untrackable = untrackable_for_root(idx, root_resolved)
    if not untrackable:
        print(
            f"No untrackable files for {root_resolved}. "
            "(Either everything is identified, or --scan-library hasn't run.)"
        )
        sys.exit(0)

    print(
        f"{len(untrackable)} untrackable file(s) in {root_resolved}.\n"
        "For each, enter a source URL to promote it (blank to skip, "
        "'q' to quit the review).\n"
    )

    promoted = 0
    skipped = 0
    for i, entry in enumerate(untrackable, 1):
        rel = entry.get("relpath") or "(unknown path)"
        title = entry.get("title") or "(unknown title)"
        author = entry.get("author") or "(unknown author)"
        reason = entry.get("reason") or ""
        print(f"[{i}/{len(untrackable)}] {rel}")
        print(f"  Title:  {title}")
        print(f"  Author: {author}")
        if reason:
            print(f"  Note:   {reason}")
        try:
            answer = input("  URL: ").strip()
        except EOFError:
            print("\nCancelled.")
            break
        if answer.lower() == "q":
            print("Stopping review.")
            break
        if not answer:
            print("  (skipped)\n")
            skipped += 1
            continue
        result = promote_untrackable(
            idx, root_resolved, rel, answer, save=False,
        )
        if result.ok:
            print(f"  ✓ Matched {result.adapter} — promoted.\n")
            promoted += 1
        else:
            print(f"  ✗ {result.message}\n")
            skipped += 1

    if promoted:
        idx.save()
    print(
        f"\nReview complete: {promoted} promoted, {skipped} skipped, "
        f"{len(untrackable) - promoted - skipped} not shown."
    )
    sys.exit(0)


def _handle_update_library(args: argparse.Namespace) -> None:
    """Check every indexed story in a library for new chapters upstream."""
    from .library.refresh import build_refresh_queue
    from .library.scanner import scan as rescan_library

    root = Path(args.update_library)
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)
    root_resolved = root.resolve()

    try:
        check_format_deps(args.format)
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        sys.exit(1)

    workers = max(1, int(args.probe_workers or 5))
    mode_bits = []
    if args.dry_run:
        mode_bits.append("dry-run")
    if args.skip_complete:
        mode_bits.append("skipping completed")
    mode_bits.append(f"{workers} probe worker{'s' if workers != 1 else ''}")
    mode = f" ({', '.join(mode_bits)})"

    recheck_interval = 0 if args.force_recheck else int(
        args.recheck_interval or 0
    )
    probe_queue, skipped = build_refresh_queue(
        root_resolved,
        skip_complete=args.skip_complete,
        recheck_interval_s=recheck_interval,
    )
    if not probe_queue and not skipped:
        print(
            f"No indexed stories for {root}. "
            "Run --scan-library first."
        )
        sys.exit(0)

    total_indexed = len(probe_queue) + len(skipped)
    print(
        f"Checking {total_indexed} indexed "
        f"stor{'y' if total_indexed == 1 else 'ies'} "
        f"in {root_resolved}{mode}...\n"
    )

    exit_code = _run_update_queue(
        probe_queue, args, workers,
        skipped_count=len(skipped),
        label="Library update",
    )

    # Stamp last_probed for the URLs we actually touched so the next
    # --update-library pass with a --recheck-interval can skip them.
    # Done before the post-update rescan so a successful rescan picks
    # up the stamp and carries it through into the refreshed entry.
    if probe_queue and not args.dry_run:
        try:
            from .library.index import LibraryIndex
            idx = LibraryIndex.load()
            idx.mark_probed(
                root_resolved, [item["url"] for item in probe_queue],
            )
        except (OSError, ValueError) as exc:
            logger.debug("Failed to stamp last_probed", exc_info=True)
            print(f"\nWarning: could not record probe timestamps: {exc}")

    # Refresh the index so chapter counts reflect any updates we just
    # applied. Cheap compared to the downloads themselves, and keeps
    # the next --update-library run from re-probing unchanged stories.
    if not args.dry_run:
        try:
            rescan_library(root_resolved)
        except (OSError, ValueError) as exc:
            logger.debug("Post-update rescan failed", exc_info=True)
            print(f"\nWarning: post-update index refresh failed: {exc}")

    sys.exit(exit_code)


def _handle_reorganize(args: argparse.Namespace) -> None:
    """Plan (and optionally apply) file moves to match the library template."""
    from .library.reorganizer import apply as apply_moves
    from .library.reorganizer import plan
    from .library.template import DEFAULT_MISC_FOLDER, DEFAULT_TEMPLATE
    from .prefs import (
        KEY_LIBRARY_MISC_FOLDER,
        KEY_LIBRARY_PATH_TEMPLATE,
        Prefs,
    )

    root = Path(args.reorganize)
    if not root.is_dir():
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)

    prefs = Prefs()
    template = prefs.get(KEY_LIBRARY_PATH_TEMPLATE) or DEFAULT_TEMPLATE
    misc_folder = prefs.get(KEY_LIBRARY_MISC_FOLDER) or DEFAULT_MISC_FOLDER

    moves = plan(root, template=template, misc_folder=misc_folder)

    if not moves:
        print(f"Library at {root} is already organized — no moves needed.")
        sys.exit(0)

    root_resolved = root.resolve()
    print(f"{len(moves)} move(s) planned for {root_resolved}:\n")
    for op in moves:
        src_rel = op.source.relative_to(root_resolved) if op.source.is_relative_to(
            root_resolved
        ) else op.source
        tgt_rel = op.target.relative_to(root_resolved)
        arrow = "renamed to" if op.is_rename else "->"
        print(f"  {src_rel}  {arrow}  {tgt_rel}")

    if not args.apply:
        print(
            "\nDry run. Re-run with --apply to execute these moves."
        )
        sys.exit(0)

    print("\nApplying...")
    result = apply_moves(root, moves)
    print(
        f"Applied {result.applied}, skipped {result.skipped}, "
        f"errors {result.errors}."
    )
    if result.messages:
        for msg in result.messages[:20]:
            print(f"  {msg}")
        if len(result.messages) > 20:
            print(f"  ... and {len(result.messages) - 20} more")
    sys.exit(0 if result.errors == 0 else 1)


def _handle_watch(args: argparse.Namespace) -> None:
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

    # Library auto-sort: if --output wasn't given and a library path
    # is configured in prefs, route fresh downloads into the library
    # and let _download_one derive the per-story subdir from metadata.
    # An explicit --output always wins so power users keep their
    # one-off overrides.
    _apply_library_autosort(args)

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

            url = extract_story_url(clip)
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


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CLI.

    All command-line flags are defined here. Kept separate from
    ``main`` so the dispatch logic stays readable and the parser can
    be tested / introspected (e.g. for shell completion) without
    running the full program.
    """
    parser = argparse.ArgumentParser(
        prog="ffn-dl",
        description="Download fanfiction from fanfiction.net and ficwad.com",
        epilog=(
            "Supported sites: fanfiction.net, ficwad.com, "
            "archiveofourown.org, royalroad.com, mediaminer.org, "
            "literotica.com, wattpad.com\n"
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
        "--scan-library",
        metavar="DIR",
        help=(
            "Scan DIR for story files (.epub/.html/.txt) from any source — "
            "ffn-dl, FanFicFare, FicHub, or bare scrapes — and record what "
            "was found in the library index. No moves, no downloads."
        ),
    )
    parser.add_argument(
        "--clear-library",
        action="store_true",
        help=(
            "With --scan-library: drop this library's existing index entries "
            "before scanning, so orphan files (deleted off disk) are removed."
        ),
    )
    parser.add_argument(
        "--reorganize",
        metavar="DIR",
        help=(
            "Plan the moves that would bring DIR into alignment with the "
            "library path template (default: <fandom>/<title> - "
            "<author>.<ext>). Reads from the library index; run "
            "--scan-library first. Dry-run by default — use --apply to "
            "actually move files."
        ),
    )
    parser.add_argument(
        "--update-library",
        metavar="DIR",
        help=(
            "Check every indexed story in DIR for new chapters upstream "
            "and download any updates in place. Uses the library index, "
            "so --scan-library must have run first. Works across all "
            "supported sources (ffn-dl's own exports, FanFicFare, FicHub)."
        ),
    )
    parser.add_argument(
        "--review-library",
        metavar="DIR",
        help=(
            "Walk the untrackable list for DIR's library and prompt for "
            "a source URL per file. Confirmed entries are promoted into "
            "the stories list with MEDIUM confidence so subsequent "
            "--update-library runs pick them up."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "With --reorganize: execute the planned moves instead of just "
            "listing them."
        ),
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="With --update-all or --scan-library: descend into subdirectories",
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
        "--recheck-interval",
        type=int,
        default=0,
        metavar="SECONDS",
        help=(
            "With --update-library: skip stories whose index "
            "last_probed timestamp is within SECONDS of now. Useful "
            "when iterating on a big library — a value like 3600 "
            "makes a second pass minutes later near-instant. "
            "Default: 0 (probe every story)."
        ),
    )
    parser.add_argument(
        "--force-recheck",
        action="store_true",
        help=(
            "With --update-library: ignore --recheck-interval and "
            "probe every story. Equivalent to --recheck-interval 0."
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
        "--speech-rate",
        type=int,
        default=0,
        metavar="PCT",
        help=(
            "Audiobook speech rate delta, integer percent "
            "(e.g. -20 = 20%% slower, +30 = 30%% faster). Default: 0."
        ),
    )
    parser.add_argument(
        "--attribution",
        choices=["builtin", "fastcoref", "booknlp"],
        default="builtin",
        help=(
            "Audiobook speaker attribution backend. 'builtin' is the "
            "default regex parser. 'fastcoref' and 'booknlp' are optional "
            "neural models you must pip-install separately — see "
            "`ffn-dl --install-attribution BACKEND`."
        ),
    )
    parser.add_argument(
        "--attribution-model-size",
        choices=["small", "big"],
        default=None,
        help=(
            "Size variant for attribution backends that offer them "
            "(BookNLP: 'small' ~150 MB or 'big' ~1 GB). Ignored "
            "for 'builtin' and 'fastcoref'."
        ),
    )
    parser.add_argument(
        "--install-attribution",
        choices=["fastcoref", "booknlp"],
        default=None,
        metavar="BACKEND",
        help="Install an optional attribution backend and exit.",
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
        help=(
            "Override the adaptive (AIMD) inter-chapter delay with a fixed "
            "random range. By default the scraper starts fast and only "
            "slows down if the site returns 429/503 (FFN floors at 2s)."
        ),
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=None,
        metavar="SEC",
        help="Upper end of the fixed delay range when --delay-min is set.",
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
            "(default: disabled — FFN now uses a steady 6s/chapter). "
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
            "Mark scene breaks clearly. In HTML/EPUB/TXT output, each "
            "<hr/> becomes a centred '* * *' marker. In audio (-f audio) "
            "output, every scene divider — <hr/> tags plus text-based "
            "dividers like '---', '* * *', 'oOo' — is replaced with a "
            "1.5-second silence clip instead of being read aloud as "
            "'asterisk asterisk asterisk'."
        ),
    )
    parser.add_argument(
        "--strip-notes",
        action="store_true",
        help=(
            "Remove paragraphs that start with 'A/N', \"Author's Note\", etc. "
            "Applies to every output format including audio. Heuristic — "
            "catches the common FFN pattern; AO3's structured notes are "
            "already excluded at scrape time."
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
        help="Search for stories matching QUERY (see --site to pick FFN, AO3, Royal Road, or Literotica)",
    )
    parser.add_argument(
        "--site",
        choices=["ffn", "ao3", "royalroad", "literotica", "wattpad"],
        default="ffn",
        help=(
            "Which site to search (default: ffn). Literotica's public "
            "search is JS-only, so --site literotica browses "
            "tags.literotica.com/<tag> instead."
        ),
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
        "--genre2",
        metavar="G",
        help="FFN-only: second genre (AND filter). Same values as --genre.",
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
        help=(
            f"Sort order. FFN: updated, published, reviews, favorites, "
            f"follows. AO3: {', '.join(list(AO3_SORT)[:4])}, ..."
        ),
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
        "--ao3-category",
        metavar="CAT",
        help="AO3-only relationship category: gen, f/m, m/m, f/f, multi, other",
    )
    parser.add_argument(
        "--ao3-freeform",
        metavar="TAG",
        help="AO3-only: additional free-form tag(s) (comma-separated)",
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
        help="Royal Road-only: comma-separated raw tag slugs (e.g. 'progression,magic')",
    )
    parser.add_argument(
        "--rr-genres",
        metavar="GENRES",
        help=(
            "Royal Road-only: comma-separated genre labels (e.g. "
            "'Fantasy,Sci-fi'). See search.RR_GENRES for the full list."
        ),
    )
    parser.add_argument(
        "--rr-warnings",
        metavar="WARN",
        help=(
            "Royal Road-only: comma-separated content warnings required "
            "(e.g. 'Profanity,Gore'). See search.RR_WARNINGS."
        ),
    )
    parser.add_argument(
        "--rr-min-words",
        metavar="N",
        help="Royal Road-only: minimum word count",
    )
    parser.add_argument(
        "--rr-max-words",
        metavar="N",
        help="Royal Road-only: maximum word count",
    )
    parser.add_argument(
        "--rr-min-pages",
        metavar="N",
        help="Royal Road-only: minimum page count",
    )
    parser.add_argument(
        "--rr-max-pages",
        metavar="N",
        help="Royal Road-only: maximum page count",
    )
    parser.add_argument(
        "--rr-min-rating",
        metavar="R",
        help="Royal Road-only: minimum average rating (0.0-5.0)",
    )
    parser.add_argument(
        "--lit-category",
        metavar="CAT",
        help=(
            "Literotica-only: browse one of Literotica's top-level "
            "categories instead of a query tag (e.g. 'Loving Wives', "
            "'Sci-Fi & Fantasy'). See search.LIT_CATEGORIES."
        ),
    )
    parser.add_argument(
        "--rr-list",
        metavar="LIST",
        help=(
            "Royal Road-only: browse one of RR's curated lists instead of "
            "free-text search. Options: best rated / trending / active "
            "popular / weekly popular / monthly popular / latest updates / "
            "new releases / complete / rising stars. The query argument is "
            "ignored when this is set."
        ),
    )
    parser.add_argument(
        "--lit-page",
        type=int,
        metavar="N",
        help="Literotica-only: which page of tag results to fetch (default 1)",
    )
    parser.add_argument(
        "--wp-mature",
        choices=["any", "exclude", "only"],
        default=None,
        help=(
            "Wattpad-only: filter by mature flag. 'exclude' drops mature "
            "results, 'only' keeps just mature."
        ),
    )
    parser.add_argument(
        "--wp-completed",
        choices=["any", "complete", "in-progress"],
        default=None,
        help="Wattpad-only: filter by completion state.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        metavar="N",
        help="Minimum search results to fetch (default 25). Pages keep "
             "loading until N is reached or the site runs out.",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        metavar="P",
        help="Results page to start from (default 1). Useful for scripted "
             "'load more' workflows that want to pick up where a previous "
             "run left off.",
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

    # --- Watchlist / notifications -----------------------------------------
    # `--watchlist-*` is a separate namespace from `-w/--watch` (clipboard)
    # on purpose: they're unrelated features and sharing the prefix would
    # trip argparse's abbreviation matching.
    watch_group = parser.add_argument_group(
        "watchlist",
        "Subscribe to stories, authors, or saved searches and receive "
        "Pushover/Discord/email alerts when they change. See --watchlist-* "
        "flags below.",
    )
    watch_group.add_argument(
        "--watchlist-add",
        metavar="URL",
        help=(
            "Add a watch for URL. Auto-detects story vs author from the URL; "
            "use --watchlist-label / --watchlist-channel to customise."
        ),
    )
    watch_group.add_argument(
        "--watchlist-add-search",
        nargs=2,
        metavar=("SITE", "QUERY"),
        help=(
            "Add a saved-search watch. SITE is one of ffn/ao3/royalroad/"
            "literotica/wattpad; QUERY is the search string. Pair with "
            "--watchlist-label for a friendly name."
        ),
    )
    watch_group.add_argument(
        "--watchlist-label",
        metavar="LABEL",
        help="Display label for the watch being added (optional).",
    )
    watch_group.add_argument(
        "--watchlist-channel",
        action="append",
        metavar="CHANNEL",
        help=(
            "Notification channel to enable on the watch being added: "
            "pushover, discord, or email. Repeat for multiple channels. "
            "If omitted, every configured channel is used."
        ),
    )
    watch_group.add_argument(
        "--watchlist-list",
        action="store_true",
        help="List all watches with their id, type, target, and status.",
    )
    watch_group.add_argument(
        "--watchlist-remove",
        metavar="ID",
        help="Remove a watch by id (or unambiguous id prefix).",
    )
    watch_group.add_argument(
        "--watchlist-run",
        action="store_true",
        help=(
            "Poll every enabled watch once and dispatch notifications for "
            "new items. Suitable for cron / Windows Task Scheduler."
        ),
    )
    watch_group.add_argument(
        "--watchlist-test",
        metavar="CHANNEL",
        help=(
            "Send a test notification through CHANNEL (pushover, discord, "
            "or email) using the currently-configured credentials."
        ),
    )

    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    return parser


def _handle_install_attribution(backend: str) -> int:
    """Install an optional attribution backend and return an exit code."""
    from . import attribution as _attr

    reason = _attr.install_unsupported_reason(backend)
    if reason:
        # Running as a frozen PyInstaller .exe — surface the
        # explanation rather than attempting a doomed subprocess.
        print(reason)
        return 1
    print(f"Installing {backend} (this may take a minute)...")
    if _attr.install(backend, log_callback=print):
        print(f"\n{backend} installed successfully.")
        return 0
    print(f"\nFailed to install {backend}.")
    return 1


def _is_search_mode(args: argparse.Namespace) -> bool:
    """Return True if the args request an interactive search.

    Most searches need --search, but several flags stand in for a
    free-text query on their own: RR list browse, RR filter-only
    browse (tags/genres/warnings/bounds), and Literotica category.
    """
    rr_filter_only = any(
        getattr(args, attr, None)
        for attr in (
            "rr_list", "rr_tags", "rr_genres", "rr_warnings",
            "rr_min_words", "rr_max_words", "rr_min_pages",
            "rr_max_pages", "rr_min_rating",
        )
    )
    return bool(
        args.search or rr_filter_only or getattr(args, "lit_category", None)
    )


def _handle_update_file(args: argparse.Namespace) -> int:
    """Single-file --update: read source URL, download new chapters, re-export."""
    update_path = Path(args.update)
    url = extract_source_url(update_path)
    existing_chapters = count_chapters(update_path)
    if args.format is None:
        args.format = _FMT_MAP.get(update_path.suffix.lower(), "epub")
    if args.output is None:
        args.output = str(update_path.parent)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        ok = _download_one(
            url, args, output_dir,
            update_path=update_path,
            existing_chapters=existing_chapters,
        )
    except KeyboardInterrupt:
        print("\nCancelled. Re-run the same command to resume.")
        return 130
    return 0 if ok else 1


def _collect_urls(args: argparse.Namespace) -> list[str]:
    """Gather story URLs from positional args and --batch file."""
    urls = list(args.url) if args.url else []
    if args.batch:
        urls.extend(_read_batch_file(args.batch))
    return urls


def _expand_author_and_series_urls(
    urls: list[str], args: argparse.Namespace,
) -> list[str]:
    """Resolve any author-page or series-page URLs into per-story URLs.

    Each author URL expands to the author's own-stories list; each
    AO3/Literotica series URL expands to its constituent works.
    Raises SystemExit on fetch failure — the caller treats these as
    fatal because the user explicitly asked for a collection.
    """
    expanded: list[str] = []
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
    return expanded


def _run_batch(
    urls: list[str], args: argparse.Namespace, output_dir: Path,
) -> int:
    """Download each URL in turn, printing a per-run summary at the end.

    Single-URL case preserves the original exit-code behaviour
    (0/1 from the one download); multi-URL case always prints a
    summary and exits non-zero if any story failed. Interrupts
    surface as exit code 130 with a partial summary.
    """
    if len(urls) == 1:
        try:
            ok = _download_one(urls[0], args, output_dir)
        except KeyboardInterrupt:
            print("\nCancelled. Re-run the same command to resume.")
            return 130
        return 0 if ok else 1

    succeeded = 0
    failed = 0
    failures: list[str] = []
    try:
        for i, url in enumerate(urls, 1):
            print(f"\n{'='*60}")
            print(f"[{i}/{len(urls)}] {url}")
            print(f"{'='*60}")
            if _download_one(url, args, output_dir):
                succeeded += 1
            else:
                failed += 1
                failures.append(url)
    except KeyboardInterrupt:
        print("\nCancelled.")
        remaining = len(urls) - (succeeded + failed)
        print(f"\n{'='*60}")
        print(
            f"Batch interrupted — {succeeded} succeeded, {failed} failed, "
            f"{remaining} not attempted."
        )
        if failures:
            print("Failed URLs:")
            for u in failures:
                print(f"  {u}")
        return 130

    print(f"\n{'='*60}")
    print(
        f"Batch complete — {succeeded} succeeded, {failed} failed "
        f"out of {len(urls)} total."
    )
    if failures:
        print("Failed URLs:")
        for u in failures:
            print(f"  {u}")
    print(f"{'='*60}")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# Watchlist handlers
#
# Each handler is a self-contained exit path: it loads the store, does one
# thing (list / add / remove / poll / test), prints a human-readable result,
# and returns an exit code. None of them return to the regular URL-dispatch
# flow — watchlist commands are their own mode.
# ---------------------------------------------------------------------------

# CLI exit codes. Named so the handlers don't sprinkle 0/1/2 magic integers.
_EXIT_OK = 0
_EXIT_GENERIC_FAILURE = 1
_EXIT_USAGE_ERROR = 2

# How many hex chars of a watch id to show in --watchlist-list. Full ids
# are 32 chars (uuid4().hex); 8 chars is enough to disambiguate in any
# realistic watchlist while staying narrow enough to fit in a terminal.
_WATCHLIST_ID_DISPLAY_CHARS = 8


def _watchlist_channels_from_args(args: argparse.Namespace) -> list[str]:
    """Resolve the channel list for a new watch from ``--watchlist-channel``.

    If the flag was omitted, every supported channel is enabled — the
    user presumably configured the creds they want; letting unused
    channels no-op on missing config is less surprising than a watch
    that silently never notifies.
    """
    from .notifications import ALL_CHANNELS

    requested = args.watchlist_channel or []
    if not requested:
        return list(ALL_CHANNELS)

    valid = set(ALL_CHANNELS)
    cleaned: list[str] = []
    for raw in requested:
        # Accept comma-separated values too — `--watchlist-channel pushover,email`
        # is ergonomically nicer than repeating the flag.
        for chunk in raw.split(","):
            name = chunk.strip().lower()
            if not name:
                continue
            if name not in valid:
                raise ValueError(
                    f"Unknown notification channel: {name!r}. "
                    f"Valid channels: {', '.join(sorted(valid))}."
                )
            if name not in cleaned:
                cleaned.append(name)
    return cleaned


def _handle_watchlist_list() -> int:
    """Print every watch in the store with its type, target, and status."""
    from .watchlist import WatchlistStore

    store = WatchlistStore.load_default()
    watches = store.all()
    if not watches:
        print("Watchlist is empty. Add one with --watchlist-add URL.")
        return _EXIT_OK

    print(f"{len(watches)} watch(es):\n")
    for w in watches:
        short_id = w.id[:_WATCHLIST_ID_DISPLAY_CHARS]
        enabled = "on " if w.enabled else "off"
        channels = ",".join(w.channels) or "(none)"
        last = w.last_checked_at or "never"
        error = f"  ERR: {w.last_error}" if w.last_error else ""
        target = w.target or (f"search: {w.query!r}" if w.type == "search" else "")
        label = w.label or target
        print(
            f"  {short_id}  [{enabled}]  {w.type:7s}  {w.site or '-':10s}  "
            f"{label}"
        )
        print(
            f"             channels={channels}  last_checked={last}{error}"
        )
    return _EXIT_OK


def _handle_watchlist_add(args: argparse.Namespace) -> int:
    """Add an author or story watch for ``args.watchlist_add``."""
    from .watchlist import (
        VALID_WATCH_TYPES,
        Watch,
        WatchlistStore,
        classify_target,
        site_key_for_url,
    )

    url = args.watchlist_add.strip()
    watch_type = classify_target(url)
    if watch_type is None or watch_type not in VALID_WATCH_TYPES:
        print(
            f"Error: {url!r} is neither a recognised author page nor a "
            "story URL on any supported site.",
            file=sys.stderr,
        )
        return _EXIT_USAGE_ERROR

    try:
        channels = _watchlist_channels_from_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_USAGE_ERROR

    store = WatchlistStore.load_default()
    watch = Watch(
        type=watch_type,
        site=site_key_for_url(url),
        target=url,
        label=(args.watchlist_label or "").strip(),
        channels=channels,
    )
    store.add(watch)
    print(
        f"Added {watch_type} watch {watch.id[:_WATCHLIST_ID_DISPLAY_CHARS]} "
        f"for {watch.display_label()}"
    )
    return _EXIT_OK


def _handle_watchlist_add_search(args: argparse.Namespace) -> int:
    """Add a saved-search watch from ``args.watchlist_add_search``."""
    from .watchlist import (
        SEARCH_SUPPORTED_SITES,
        WATCH_TYPE_SEARCH,
        Watch,
        WatchlistStore,
    )

    site_raw, query = args.watchlist_add_search
    site = site_raw.strip().lower()
    if site not in SEARCH_SUPPORTED_SITES:
        print(
            f"Error: search watches not supported on {site!r}. "
            f"Supported: {', '.join(SEARCH_SUPPORTED_SITES)}.",
            file=sys.stderr,
        )
        return _EXIT_USAGE_ERROR

    try:
        channels = _watchlist_channels_from_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_USAGE_ERROR

    store = WatchlistStore.load_default()
    watch = Watch(
        type=WATCH_TYPE_SEARCH,
        site=site,
        target=f"{site} search: {query}",
        label=(args.watchlist_label or "").strip(),
        channels=channels,
        query=query,
    )
    store.add(watch)
    print(
        f"Added search watch {watch.id[:_WATCHLIST_ID_DISPLAY_CHARS]} "
        f"on {site}: {query!r}"
    )
    return _EXIT_OK


def _handle_watchlist_remove(watch_id: str) -> int:
    """Remove the watch matching ``watch_id`` (full id or unambiguous prefix)."""
    from .watchlist import WatchlistStore

    store = WatchlistStore.load_default()
    if store.remove(watch_id):
        print(f"Removed watch {watch_id}.")
        return _EXIT_OK
    print(
        f"No watch matches {watch_id!r}. Use --watchlist-list to see ids.",
        file=sys.stderr,
    )
    return _EXIT_USAGE_ERROR


def _handle_watchlist_run() -> int:
    """Poll every enabled watch once; print a per-watch summary."""
    from .prefs import Prefs
    from .watchlist import WatchlistStore, run_once

    store = WatchlistStore.load_default()
    if not store.all():
        print("Watchlist is empty — nothing to poll.")
        return _EXIT_OK

    prefs = Prefs()
    results = run_once(store, prefs)

    any_error = False
    new_total = 0
    for result in results:
        watch = store.get(result.watch_id)
        label = watch.display_label() if watch else result.watch_id[:_WATCHLIST_ID_DISPLAY_CHARS]
        if not result.ok:
            any_error = True
            print(f"  [!] {label}: {result.error}", file=sys.stderr)
            continue
        if result.new_items:
            new_total += len(result.new_items)
            if result.chapter_delta:
                print(
                    f"  [+] {label}: {result.chapter_delta} new chapter"
                    f"{'s' if result.chapter_delta != 1 else ''}"
                )
            else:
                print(f"  [+] {label}: {len(result.new_items)} new item(s)")
        else:
            print(f"  [=] {label}: no change")

    print(
        f"Poll complete — {len(results)} watch(es) checked, "
        f"{new_total} new item(s)."
    )
    return _EXIT_GENERIC_FAILURE if any_error else _EXIT_OK


def _handle_watchlist_test(channel: str) -> int:
    """Send a test notification through ``channel`` via the current creds."""
    from .notifications import (
        ALL_CHANNELS,
        Notification,
        NotificationError,
        dispatch,
    )
    from .prefs import Prefs

    channel = channel.strip().lower()
    if channel not in ALL_CHANNELS:
        print(
            f"Error: unknown channel {channel!r}. "
            f"Valid: {', '.join(ALL_CHANNELS)}.",
            file=sys.stderr,
        )
        return _EXIT_USAGE_ERROR

    prefs = Prefs()
    notification = Notification(
        title="ffn-dl watchlist test",
        message=(
            "If you're reading this, your ffn-dl notification credentials "
            "for this channel are working."
        ),
        url="https://github.com/matalvernaz/ffn-dl",
    )
    # dispatch() catches NotificationError per-channel and returns a list
    # of (channel, message) failures. We still handle the import-time
    # exception class here as a belt-and-braces.
    try:
        delivered, failures = dispatch([channel], notification, prefs)
    except NotificationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return _EXIT_GENERIC_FAILURE

    if failures:
        for ch, reason in failures:
            print(f"  [!] {ch}: {reason}", file=sys.stderr)
        return _EXIT_GENERIC_FAILURE
    print(f"Test notification delivered via {', '.join(delivered)}.")
    return _EXIT_OK


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Parses args and dispatches to a handler."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if getattr(args, "install_attribution", None):
        sys.exit(_handle_install_attribution(args.install_attribution))

    # --- Watchlist modes: all self-contained (no positional URLs) ---
    # Checked before search / library / URL dispatch so none of those
    # paths treats a watchlist flag as "no arguments, show help".
    if getattr(args, "watchlist_list", False):
        sys.exit(_handle_watchlist_list())
    if getattr(args, "watchlist_run", False):
        sys.exit(_handle_watchlist_run())
    if getattr(args, "watchlist_add", None):
        sys.exit(_handle_watchlist_add(args))
    if getattr(args, "watchlist_add_search", None):
        sys.exit(_handle_watchlist_add_search(args))
    if getattr(args, "watchlist_remove", None):
        sys.exit(_handle_watchlist_remove(args.watchlist_remove))
    if getattr(args, "watchlist_test", None):
        sys.exit(_handle_watchlist_test(args.watchlist_test))

    # --- Search mode ---
    if _is_search_mode(args):
        if not args.search:
            args.search = ""
        _handle_search(args)
        return

    # --- Library / bulk modes: each handler owns its own sys.exit ---
    if args.scan_library:
        _handle_scan_library(args)
        return
    if args.reorganize:
        _handle_reorganize(args)
        return
    if args.update_library:
        _handle_update_library(args)
        return
    if args.review_library:
        _handle_review_library(args)
        return
    if args.update_all:
        _handle_update_all(args)
        return
    if args.watch:
        _handle_watch(args)
        return

    # --- Single-file --update (not batch) ---
    if args.update:
        if args.batch:
            parser.error("--update and --batch cannot be used together")
        sys.exit(_handle_update_file(args))

    # --- --author: fetch the author's own stories, then batch-download ---
    if args.author:
        if args.batch:
            parser.error("--author and --batch cannot be used together")
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

    else:
        try:
            urls = _collect_urls(args)
        except FileNotFoundError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        # --merge-series: peel off series URLs and render each as one file.
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
                urls = [u for u in urls if not _is_series_url(u)]
                if not urls:
                    sys.exit(0 if ok else 1)

        urls = _expand_author_and_series_urls(urls, args)

        if not urls:
            parser.error(
                "either a URL, --batch FILE, --update FILE, or "
                "--author URL is required"
            )

    if args.format is None:
        args.format = "epub"

    # Library auto-sort: if --output wasn't given and a library path is
    # configured, route fresh downloads into the library and let
    # _download_one derive the per-story subdir from metadata. Explicit
    # --output always wins so power users keep their one-off overrides.
    _apply_library_autosort(args)
    if args.output is None:
        args.output = "."
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    sys.exit(_run_batch(urls, args, output_dir))
