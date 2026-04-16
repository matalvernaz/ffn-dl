"""Command-line interface for ffn-dl."""

import argparse
import logging
import sys
from pathlib import Path

from .exporters import EXPORTERS
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
    )

    try:
        story_id = FFNScraper.parse_story_id(args.url)
        print(f"Downloading story {story_id} from fanfiction.net...")

        def progress(current, total):
            print(f"  [{current}/{total}] downloaded")

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
        path = exporter(story, str(output_dir))
        print(f"\nSaved to: {path}")

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
        print("\nDownload cancelled.")
        sys.exit(130)
