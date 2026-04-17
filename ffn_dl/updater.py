"""Update mode — count chapters in existing files, detect new chapters."""

import re
from pathlib import Path

from bs4 import BeautifulSoup


def count_chapters(filepath):
    """Count chapters in an existing export file."""
    path = Path(filepath)
    suffix = path.suffix.lower()

    if suffix == ".html":
        text = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        return len(soup.find_all("div", class_="chapter"))

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        return len(re.findall(r"^--- .+ ---$", text, re.MULTILINE))

    if suffix == ".epub":
        try:
            from ebooklib import epub

            book = epub.read_epub(str(path))
            return sum(
                1
                for item in book.get_items()
                if hasattr(item, "file_name")
                and item.file_name.startswith("chapter_")
            )
        except Exception:
            return 0

    return 0


def extract_status(filepath) -> str:
    """Return the story's completion status ('Complete' / 'In-Progress' / '')
    by reading the metadata block of an ffn-dl export. Empty string if not
    recognisable, so callers can treat unknown as "not complete."
    """
    path = Path(filepath)
    if not path.exists():
        return ""
    suffix = path.suffix.lower()

    if suffix == ".html":
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"<th>Status</th><td>([^<]+)</td>", text)
        if match:
            return match.group(1).strip()
        return ""

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^Status:\s*(.+)$", text, re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    if suffix == ".epub":
        try:
            from ebooklib import epub
            book = epub.read_epub(str(path))
            # Status lands in the description block we render into the title
            # page. ebooklib exposes the first-chapter / title-page HTML via
            # get_items; scan the first HTML item's body.
            for item in book.get_items():
                if not hasattr(item, "file_name"):
                    continue
                if not item.file_name.startswith("title"):
                    continue
                body = item.content.decode("utf-8", errors="replace")
                match = re.search(r"<th>Status</th><td>([^<]+)</td>", body)
                if match:
                    return match.group(1).strip()
        except Exception:
            pass

    return ""


def extract_source_url(filepath):
    """Read an existing export file and extract the source URL."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".html":
        match = re.search(
            r'<th>Source</th><td><a href="([^"]+)">', text
        )
        if match:
            return match.group(1)

    if suffix == ".txt":
        match = re.search(r"^Source:\s*(https?://\S+)", text, re.MULTILINE)
        if match:
            return match.group(1)

    if suffix == ".epub":
        try:
            from ebooklib import epub

            book = epub.read_epub(str(path))
            dc = book.metadata.get("http://purl.org/dc/elements/1.1/", {})
            sources = dc.get("source", [])
            if sources:
                return sources[0][0]
        except Exception:
            pass

    # Fallback: look for any supported story URL
    match = re.search(
        r"https?://(?:www\.)?("
        r"fanfiction\.net/s/\d+"
        r"|ficwad\.com/story/\d+"
        r"|(?:archiveofourown\.org|ao3\.org)/works/\d+"
        r")",
        text,
    )
    if match:
        return match.group(0)

    raise ValueError(
        f"Could not find a source URL in {path.name}. "
        "Is this a file exported by ffn-dl?"
    )
