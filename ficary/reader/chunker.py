"""Split chapter text into ordered chunks with exact character offsets.

Each :class:`Chunk` carries ``start``/``end`` offsets into the *exact* string
the reader displays, so a highlight range or caret position lines up with the
text. Paragraphs are the natural unit (``html_to_text`` separates them with a
blank line); an oversized paragraph is sub-split at sentence boundaries so
live-TTS first-audio latency stays about one sentence rather than a whole
paragraph.

Splitting is positional — spans are computed directly over the original
string, never by re-finding rejoined pieces (which drifted whenever sentences
were separated by two spaces or newlines and broke the ``text[start:end]``
contract). Used by the screen-reader view (paragraph navigation) and live TTS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A chunk longer than this is sub-split at sentence boundaries.
MAX_CHUNK_CHARS = 400

# End-of-sentence punctuation, optional closing quotes/brackets, then the
# whitespace that separates it from the next sentence.
_SENT_BREAK = re.compile(r'[.!?…]+["\'”’)\]]*\s+')


@dataclass
class Chunk:
    index: int
    start: int
    end: int
    text: str


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Return ordered chunks whose ``text`` equals ``text[start:end]``."""
    chunks: list[Chunk] = []
    index = 0
    for para_start, para_end in _paragraph_spans(text):
        if para_end - para_start <= max_chars:
            spans = [(para_start, para_end)]
        else:
            spans = _packed_sentence_spans(text, para_start, para_end, max_chars)
        for start, end in spans:
            while end > start and text[end - 1] in " \t\n":
                end -= 1
            if end <= start or not text[start:end].strip():
                continue
            chunks.append(Chunk(index=index, start=start, end=end,
                                text=text[start:end]))
            index += 1
    return chunks


def _packed_sentence_spans(text: str, start: int, end: int,
                           max_chars: int) -> list[tuple[int, int]]:
    """Split ``text[start:end]`` into spans of at most ``max_chars``,
    breaking at sentence boundaries, falling back to word boundaries for a
    single overlong sentence."""
    out: list[tuple[int, int]] = []
    cur_start: int | None = None
    for s, e in _sentence_spans(text, start, end):
        if e - s > max_chars:
            if cur_start is not None:
                out.append((cur_start, s))
                cur_start = None
            out.extend(_hard_split(text, s, e, max_chars))
        elif cur_start is None:
            cur_start = s
        elif e - cur_start > max_chars:
            out.append((cur_start, s))
            cur_start = s
        # else: sentence fits in the current chunk — extend by continuing
    if cur_start is not None:
        out.append((cur_start, end))
    return out


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = start
    for match in _SENT_BREAK.finditer(text, start, end):
        spans.append((pos, match.end()))
        pos = match.end()
    if pos < end:
        spans.append((pos, end))
    return spans


def _hard_split(text: str, start: int, end: int,
                max_chars: int) -> list[tuple[int, int]]:
    """A single sentence longer than ``max_chars``: break at the last space
    inside each window, or hard-cut when there is none."""
    out: list[tuple[int, int]] = []
    pos = start
    while end - pos > max_chars:
        window_end = pos + max_chars
        cut = text.rfind(" ", pos + 1, window_end)
        if cut <= pos:
            cut = window_end
        out.append((pos, cut))
        pos = cut
        while pos < end and text[pos] == " ":
            pos += 1
    if pos < end:
        out.append((pos, end))
    return out


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offset pairs for each non-empty paragraph, where
    paragraphs are separated by a blank line."""
    spans: list[tuple[int, int]] = []
    pos, n = 0, len(text)
    while pos < n:
        while pos < n and text[pos] == "\n":
            pos += 1
        if pos >= n:
            break
        nl = text.find("\n\n", pos)
        block_end = n if nl == -1 else nl
        end = block_end
        while end > pos and text[end - 1] in " \t\n":
            end -= 1
        if end > pos:
            spans.append((pos, end))
        pos = block_end
    return spans
