"""Chapter-content hashing — normalisation, ordering, diffing."""

from __future__ import annotations

from ffn_dl.content_hash import (
    diff_hashes,
    hash_chapter,
    hash_chapters,
    normalise_chapter_html,
    story_chapter_hashes,
)
from ffn_dl.models import Chapter, Story


class TestNormalise:
    def test_collapses_runs_of_whitespace(self):
        assert normalise_chapter_html("<p>hello    world</p>") == "<p>hello world</p>"

    def test_trims_outer_whitespace(self):
        assert normalise_chapter_html("   \n<p>x</p>\n\n") == "<p>x</p>"

    def test_newlines_treated_as_whitespace(self):
        assert normalise_chapter_html("<p>\nhi\n\nthere\n</p>") == "<p> hi there </p>"

    def test_none_and_empty_treated_identically(self):
        assert normalise_chapter_html(None) == ""
        assert normalise_chapter_html("") == ""
        assert normalise_chapter_html("   ") == ""


class TestHashChapter:
    def test_same_input_same_hash(self):
        assert hash_chapter("<p>hi</p>") == hash_chapter("<p>hi</p>")

    def test_different_input_different_hash(self):
        assert hash_chapter("<p>hi</p>") != hash_chapter("<p>bye</p>")

    def test_whitespace_changes_do_not_shift_hash(self):
        """The most common false-positive — a pretty-printer inserts
        newlines between tags on re-export. Hash must stay stable."""
        a = "<p>line one.</p><p>line two.</p>"
        b = "<p>line one.</p>\n\n<p>line two.</p>"
        c = "  <p>line one.</p>\n<p>line two.</p>  "
        assert hash_chapter(a) == hash_chapter(b) == hash_chapter(c)

    def test_real_content_edit_shifts_hash(self):
        a = "<p>Alice smiled.</p>"
        b = "<p>Alice scowled.</p>"
        assert hash_chapter(a) != hash_chapter(b)

    def test_hash_is_hex_digest(self):
        h = hash_chapter("x")
        assert len(h) == 64
        int(h, 16)  # raises if not valid hex

    def test_none_hash_equals_empty_hash(self):
        assert hash_chapter(None) == hash_chapter("")


class TestHashChapters:
    def test_orders_by_chapter_number(self):
        chapters = [
            Chapter(number=3, title="C3", html="<p>three</p>"),
            Chapter(number=1, title="C1", html="<p>one</p>"),
            Chapter(number=2, title="C2", html="<p>two</p>"),
        ]
        hashes = hash_chapters(chapters)
        # Should equal hashes of [ch1, ch2, ch3] in that order.
        expected = [
            hash_chapter("<p>one</p>"),
            hash_chapter("<p>two</p>"),
            hash_chapter("<p>three</p>"),
        ]
        assert hashes == expected

    def test_empty_list_yields_empty_hashes(self):
        assert hash_chapters([]) == []


class TestStoryChapterHashes:
    def test_delegates_to_hash_chapters(self):
        story = Story(
            id=1, title="T", author="A", summary="", url="",
        )
        story.chapters = [
            Chapter(number=2, title="B", html="<p>two</p>"),
            Chapter(number=1, title="A", html="<p>one</p>"),
        ]
        hashes = story_chapter_hashes(story)
        assert hashes == [hash_chapter("<p>one</p>"), hash_chapter("<p>two</p>")]


class TestDiffHashes:
    def test_no_changes_empty_diff(self):
        assert diff_hashes(["a", "b", "c"], ["a", "b", "c"]) == []

    def test_detects_single_change(self):
        assert diff_hashes(["a", "b", "c"], ["a", "X", "c"]) == [2]

    def test_detects_multiple_changes(self):
        assert diff_hashes(["a", "b", "c"], ["X", "b", "Y"]) == [1, 3]

    def test_length_mismatch_diffs_common_prefix(self):
        # Count-change is the caller's problem; we still report any
        # differences in the prefix both lists share.
        assert diff_hashes(["a", "b"], ["a", "X", "c"]) == [2]

    def test_shorter_fresh_diffs_common_prefix(self):
        assert diff_hashes(["a", "b", "c"], ["a", "X"]) == [2]

    def test_both_empty(self):
        assert diff_hashes([], []) == []
