"""Command-line interface for ffn-dl."""

import argparse
import logging
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


def _detect_site(url):
    """Return the appropriate scraper class based on the URL."""
    text = str(url).lower()
    if "ficwad.com" in text:
        return FicWadScraper
    # Default to FFN (handles bare numeric IDs too)
    return FFNScraper


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
        help=(
            "Story URL or numeric ID "
            "(e.g. https://www.fanfiction.net/s/12345, "
            "https://ficwad.com/story/76962, or just 12345)"
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=sorted(EXPORTERS),
        default="epub",
        help="Output format (default: epub)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=".",
        help="Output directory (default: current directory)",
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
        "--no-cache",
        action="store_true",
        help="Disable chapter caching (re-download everything)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    scraper_cls = _detect_site(args.url)

    kwargs = {
        "max_retries": args.max_retries,
        "use_cache": not args.no_cache,
    }
    if args.delay_min is not None and args.delay_max is not None:
        kwargs["delay_range"] = (args.delay_min, args.delay_max)
    elif args.delay_min is not None or args.delay_max is not None:
        # Use site defaults for whichever wasn't set
        d_min = args.delay_min if args.delay_min is not None else 1.0
        d_max = args.delay_max if args.delay_max is not None else 5.0
        kwargs["delay_range"] = (d_min, d_max)

    scraper = scraper_cls(**kwargs)

    def progress(current, total, title, cached):
        tag = " (cached)" if cached else ""
        print(f"  [{current}/{total}] {title}{tag}")

    try:
        story_id = scraper.parse_story_id(args.url)
        print(f"Downloading story {story_id} from {scraper.site_name}...")

        story = scraper.download(args.url, progress_callback=progress)

        words = story.metadata.get("words", "?")
        status = story.metadata.get("status", "Unknown")
        print()
        print(f"  Title:    {story.title}")
        print(f"  Author:   {story.author}")
        print(f"  Chapters: {len(story.chapters)}")
        print(f"  Words:    {words}")
        print(f"  Status:   {status}")

        exporter = EXPORTERS[args.format]
        path = exporter(story, str(output_dir), template=args.name)
        print(f"\nSaved to: {path}")

        if not args.no_cache:
            scraper.clean_cache(story_id)

    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except StoryNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except CloudflareBlockError as exc:
        print(f"Blocked: {exc}", file=sys.stderr)
        sys.exit(2)
    except RateLimitError as exc:
        print(f"\nRate limited: {exc}", file=sys.stderr)
        print(
            "Try increasing --delay-min / --delay-max or wait before retrying.",
            file=sys.stderr,
        )
        sys.exit(2)
    except ImportError as exc:
        print(f"Missing dependency: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nDownload cancelled. Re-run the same command to resume.")
        sys.exit(130)
