"""Command-line interface for ffn-dl."""

import argparse
import logging
import sys
from pathlib import Path

from .exporters import DEFAULT_TEMPLATE, EXPORTERS
from .scraper import (
    CloudflareBlockError,
    FFNScraper,
    RateLimitError,
    StoryNotFoundError,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ffn-dl",
        description="Download fanfiction from fanfiction.net",
        epilog=(
            "Name template placeholders: "
            "{title} {author} {id} {words} {status} {rating} {language} {chapters}"
        ),
    )
    parser.add_argument(
        "url",
        help=(
            "Story URL or numeric ID "
            "(e.g. https://www.fanfiction.net/s/12345 or 12345)"
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
        default=2.0,
        metavar="SEC",
        help="Minimum delay between chapter requests (default: 2)",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Maximum delay between chapter requests (default: 5)",
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

    scraper = FFNScraper(
        delay_range=(args.delay_min, args.delay_max),
        max_retries=args.max_retries,
        use_cache=not args.no_cache,
    )

    def progress(current, total, title, cached):
        tag = " (cached)" if cached else ""
        print(f"  [{current}/{total}] {title}{tag}")

    try:
        story_id = FFNScraper.parse_story_id(args.url)
        print(f"Downloading story {story_id} from fanfiction.net...")

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

        # Clean cache on success
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
