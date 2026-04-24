"""Tests for cross-site mirror detection."""

from __future__ import annotations

from pathlib import Path

from ffn_dl.library.index import LibraryIndex
from ffn_dl.library.mirrors import (
    FIRST_CHAPTER_OVERLAP_THRESHOLD,
    MIN_FIRST_CHAPTER_TOKENS,
    TITLE_JACCARD_THRESHOLD,
    find_mirrors,
    jaccard,
    normalise_author,
    normalise_title,
    summarise,
)
from ffn_dl.library.scanner import scan
from ffn_dl.models import Chapter, Story


# ── Pure helpers ────────────────────────────────────────────────


def test_normalise_title_folds_case_accents_punctuation():
    # Apostrophes drop silently so "Witch's" matches "Witchs"; other
    # punctuation collapses to whitespace (word-boundary preserved).
    assert normalise_title("The Witch's Daughter!") == "the witchs daughter"
    assert normalise_title("Café Renée  ") == "cafe renee"
    assert normalise_title("He said: it's fine") == "he said its fine"
    assert normalise_title(None) == ""


def test_normalise_title_preserves_non_ascii_letters():
    """CJK and Cyrillic titles must survive normalisation.
    Regression: an earlier draft used an ASCII-only character class
    that silently erased them, making the empty-title guard drop
    every Japanese or Russian fic pair before comparison."""
    assert normalise_title("失われた物語") == "失われた物語"
    # Cyrillic "А" (not ASCII "A") stays a letter
    result = normalise_title("А Tale")
    assert "tale" in result
    # Single-token CJK title still yields a non-empty token set
    from ffn_dl.library.mirrors import _token_set
    assert _token_set(normalise_title("失われた物語"))


def test_normalise_author_matches_title_rules():
    # J.K. splits on the period (keeps the "initials" convention
    # recognisable); apostrophe collapse is inherited from titles.
    assert normalise_author("J.K. Rowling") == "j k rowling"
    assert normalise_author("O'Reilly") == "oreilly"


def test_jaccard_returns_zero_on_empty():
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a"}, set()) == 0.0


def test_jaccard_matches_expected_values():
    a = {"the", "dragon", "wakes"}
    b = {"the", "dragon", "sleeps"}
    # intersection 2, union 4 → 0.5
    assert abs(jaccard(a, b) - 0.5) < 1e-9


# ── find_mirrors integration ────────────────────────────────────


def _index_path(tmp_path: Path) -> Path:
    return tmp_path / "idx.json"


def _write_long_chapter_story(
    tmp_path: Path,
    *,
    title: str,
    author: str,
    url: str,
    first_chapter_body: str,
    story_id: int,
    status: str = "In-Progress",
) -> Path:
    """Build an EPUB whose first chapter has enough prose to clear the
    MIN_FIRST_CHAPTER_TOKENS floor — the fixture's default 7-word
    chapter is too short for the first-chapter signal to fire. Writes
    into a per-story subdir so mirror pairs sharing a title+author
    don't overwrite each other's files."""
    from ffn_dl.exporters import export_epub

    sub = tmp_path / f"s{story_id}"
    sub.mkdir(exist_ok=True)

    story = Story(
        id=story_id, title=title, author=author,
        summary="test", url=url,
    )
    story.metadata = {
        "category": "Harry Potter",
        "rating": "T",
        "status": status,
        "genre": "Drama",
        "characters": "Harry",
    }
    story.chapters.append(Chapter(
        number=1, title="One",
        html=f"<p>{first_chapter_body}</p>",
    ))
    story.chapters.append(Chapter(
        number=2, title="Two",
        html="<p>Second chapter body.</p>",
    ))
    return Path(export_epub(story, str(sub)))


def _drabble_epub(
    tmp_path: Path,
    *,
    title: str,
    author: str,
    url: str,
    story_id: int,
) -> Path:
    """Fixture EPUB with the stock 7-word first chapter (too short to
    clear the first-chapter signal threshold). Per-story subdir so
    title+author twins don't clobber each other on disk."""
    from .library_fixtures import ffndl_epub as _ffndl_epub

    sub = tmp_path / f"s{story_id}"
    sub.mkdir(exist_ok=True)
    return _ffndl_epub(
        sub, title=title, author=author, url=url, story_id=story_id,
    )


_LONG_BODY_A = " ".join([
    "The dragon descended on the valley as the moon rose.",
    "Harry gripped his wand tight and whispered the incantation again.",
    "Below them the villagers scattered toward the forest edge.",
    "Hermione called out a warning that echoed across the stones.",
    "Ron steadied himself against the ruined wall and nocked an arrow.",
    "The dragon's breath scorched a path through the wheat field.",
    "They had planned for this, rehearsed every move a dozen times.",
    "Still, no rehearsal could have prepared them for the heat.",
])

_LONG_BODY_B_EDITED = " ".join([
    "The dragon descended on the valley as the moon ascended.",
    "Harry gripped his wand firmly and whispered the incantation once more.",
    "Below them the villagers fled toward the forest edge.",
    "Hermione called out a warning that echoed across the stones.",
    "Ron steadied himself against the ruined wall and nocked a bolt.",
    "The dragon's breath scorched a path through the cornfield.",
    "They had planned for this for weeks, rehearsing every move.",
    "Still, no rehearsal could have prepared them for the heat.",
])

_LONG_BODY_UNRELATED = " ".join([
    "Elena opened the cafe door at dawn and flipped the sign to open.",
    "Steam curled from the espresso machine as the first customer wandered in.",
    "She wiped the counter clean, arranged the pastries, checked the till.",
    "A postman leaned through the doorway and smiled a greeting.",
    "The bell over the door jingled softly each time someone entered.",
    "By noon the queue stretched around the corner past the bakery.",
    "Her regulars took their usual tables near the window overlooking the square.",
    "She carried out another tray of scones and asked about the weather.",
])


def test_exact_title_and_author_match_across_sites_is_flagged(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _write_long_chapter_story(
        lib,
        title="The Dragon Wakes",
        author="Test Author",
        url="https://www.fanfiction.net/s/100/1/",
        first_chapter_body=_LONG_BODY_A,
        story_id=100,
    )
    _write_long_chapter_story(
        lib,
        title="The Dragon Wakes",
        author="Test Author",
        url="https://archiveofourown.org/works/200",
        first_chapter_body=_LONG_BODY_B_EDITED,
        story_id=200,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert len(candidates) == 1
    c = candidates[0]
    assert set(c.signals) >= {"title", "author"}
    assert c.a.url != c.b.url


def test_same_site_duplicates_are_not_flagged(tmp_path: Path):
    """The library doctor owns within-site dup detection; this module
    stays strictly cross-site so the two reports don't overlap."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write_long_chapter_story(
        lib,
        title="Same Site Story",
        author="Author",
        url="https://www.fanfiction.net/s/300/1/",
        first_chapter_body=_LONG_BODY_A,
        story_id=300,
    )
    _write_long_chapter_story(
        lib,
        title="Same Site Story",
        author="Author",
        url="https://www.fanfiction.net/s/301/1/",
        first_chapter_body=_LONG_BODY_A,
        story_id=301,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert candidates == []


def test_single_signal_is_not_enough_to_flag(tmp_path: Path):
    """Only the title matches; authors differ and first chapters don't
    overlap. The destructive-heuristics rule requires ≥2 signals."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write_long_chapter_story(
        lib,
        title="A Common Title",
        author="Author One",
        url="https://www.fanfiction.net/s/400/1/",
        first_chapter_body=_LONG_BODY_A,
        story_id=400,
    )
    _write_long_chapter_story(
        lib,
        title="A Common Title",
        author="Author Two",
        url="https://archiveofourown.org/works/500",
        first_chapter_body=_LONG_BODY_UNRELATED,
        story_id=500,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert candidates == []


def test_punctuation_drift_title_plus_author_still_flags(tmp_path: Path):
    """Normalisation must collapse punctuation/accent differences so
    "Renée's Cafe!" on one site matches "Renees Cafe" on another."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write_long_chapter_story(
        lib,
        title="Renée's Cafe!",
        author="Same Pen",
        url="https://www.fanfiction.net/s/600/1/",
        first_chapter_body=_LONG_BODY_UNRELATED,
        story_id=600,
    )
    _write_long_chapter_story(
        lib,
        title="Renees Cafe",
        author="Same Pen",
        url="https://archiveofourown.org/works/700",
        first_chapter_body=_LONG_BODY_A,  # unrelated body — only title+author signal
        story_id=700,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert len(candidates) == 1
    signals = set(candidates[0].signals)
    assert "title" in signals and "author" in signals


def test_metadata_only_mode_skips_first_chapter(tmp_path: Path):
    """``use_first_chapter=False`` must short-circuit the file-parse
    path so scripted callers can do fast metadata-only sweeps."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _write_long_chapter_story(
        lib,
        title="Exact Title Match",
        author="Same Author",
        url="https://www.fanfiction.net/s/800/1/",
        first_chapter_body=_LONG_BODY_A,
        story_id=800,
    )
    _write_long_chapter_story(
        lib,
        title="Exact Title Match",
        author="Same Author",
        url="https://archiveofourown.org/works/900",
        first_chapter_body=_LONG_BODY_B_EDITED,
        story_id=900,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib], use_first_chapter=False)
    assert len(candidates) == 1
    assert "first_chapter" not in candidates[0].signals


def test_short_first_chapter_falls_back_to_metadata_signals(tmp_path: Path):
    """A first chapter shorter than MIN_FIRST_CHAPTER_TOKENS can't
    produce a reliable overlap — the signal is dropped. The pair still
    flags when title + author match, which is the common case for
    drabbles or flash fiction."""
    lib = tmp_path / "lib"
    lib.mkdir()
    # The base fixture uses a 7-word first chapter; that's well below
    # MIN_FIRST_CHAPTER_TOKENS, so the first-chapter signal drops.
    _drabble_epub(
        lib,
        title="Drabble Twin",
        author="Micro Author",
        url="https://www.fanfiction.net/s/1000/1/",
        story_id=1000,
    )
    _drabble_epub(
        lib,
        title="Drabble Twin",
        author="Micro Author",
        url="https://archiveofourown.org/works/1100",
        story_id=1100,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert len(candidates) == 1
    # first_chapter must NOT be in signals — the token count was below
    # the floor, so it was intentionally skipped.
    assert "first_chapter" not in candidates[0].signals
    assert {"title", "author"}.issubset(candidates[0].signals)


def test_summary_reports_pair_count_and_signals(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    _drabble_epub(
        lib,
        title="Twin A",
        author="Same",
        url="https://www.fanfiction.net/s/2000/1/",
        story_id=2000,
    )
    _drabble_epub(
        lib,
        title="Twin A",
        author="Same",
        url="https://archiveofourown.org/works/2100",
        story_id=2100,
    )
    scan(lib, index_path=_index_path(tmp_path))
    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    text = summarise(candidates)
    assert "1 possible mirror pair" in text
    assert "signals:" in text
    assert "verify before deleting" in text


def test_summary_handles_empty_list():
    assert summarise([]).startswith("No cross-site mirror")


def test_empty_library_returns_no_candidates(tmp_path: Path):
    lib = tmp_path / "lib"
    lib.mkdir()
    scan(lib, index_path=_index_path(tmp_path))
    idx = LibraryIndex.load(_index_path(tmp_path))
    assert find_mirrors(idx, roots=[lib]) == []


def test_non_ascii_title_pair_is_still_detectable(tmp_path: Path):
    """A cross-site pair with a CJK title must be detectable. Before
    the Unicode-aware normalisation fix, both titles collapsed to
    empty strings and the empty-title guard dropped the pair before
    comparison — silently excluding every non-Latin fanfic mirror
    from the detector."""
    lib = tmp_path / "lib"
    lib.mkdir()
    _drabble_epub(
        lib,
        title="失われた物語",
        author="Test Sensei",
        url="https://www.fanfiction.net/s/5000/1/",
        story_id=5000,
    )
    _drabble_epub(
        lib,
        title="失われた物語",
        author="Test Sensei",
        url="https://archiveofourown.org/works/5100",
        story_id=5100,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert len(candidates) == 1
    assert {"title", "author"}.issubset(candidates[0].signals)


def test_ordering_is_stable_and_by_signal_strength(tmp_path: Path):
    """Stronger matches (three signals) outrank weaker ones (two)
    regardless of bucket iteration order."""
    lib = tmp_path / "lib"
    lib.mkdir()
    # Two-signal pair: title + author
    _drabble_epub(
        lib, title="Alpha Beta",
        author="Auth",
        url="https://www.fanfiction.net/s/3000/1/",
        story_id=3000,
    )
    _drabble_epub(
        lib, title="Alpha Beta",
        author="Auth",
        url="https://archiveofourown.org/works/3100",
        story_id=3100,
    )
    # Three-signal pair: title + author + first_chapter
    _write_long_chapter_story(
        lib, title="Gamma Delta",
        author="Auth2",
        url="https://www.fanfiction.net/s/4000/1/",
        first_chapter_body=_LONG_BODY_A,
        story_id=4000,
    )
    _write_long_chapter_story(
        lib, title="Gamma Delta",
        author="Auth2",
        url="https://archiveofourown.org/works/4100",
        first_chapter_body=_LONG_BODY_B_EDITED,
        story_id=4100,
    )
    scan(lib, index_path=_index_path(tmp_path))

    idx = LibraryIndex.load(_index_path(tmp_path))
    candidates = find_mirrors(idx, roots=[lib])
    assert len(candidates) == 2
    # Three-signal pair should come first
    assert candidates[0].signal_count == 3
    assert candidates[1].signal_count == 2
