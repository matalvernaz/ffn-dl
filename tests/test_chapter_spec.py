"""Chapter range parsing — pure function."""

import pytest

from ffn_dl.models import chapter_in_spec, parse_chapter_spec


class TestParseChapterSpec:
    def test_none_and_empty_mean_all(self):
        assert parse_chapter_spec(None) is None
        assert parse_chapter_spec("") is None
        assert parse_chapter_spec("   ") is None

    def test_single_chapter(self):
        assert parse_chapter_spec("5") == [(5, 5)]

    def test_closed_range(self):
        assert parse_chapter_spec("1-5") == [(1, 5)]

    def test_left_open_range(self):
        assert parse_chapter_spec("-5") == [(1, 5)]

    def test_right_open_range(self):
        assert parse_chapter_spec("20-") == [(20, None)]

    def test_multiple_tokens(self):
        assert parse_chapter_spec("1,3,5-10,20-") == [
            (1, 1), (3, 3), (5, 10), (20, None),
        ]

    def test_tolerates_whitespace(self):
        assert parse_chapter_spec("  1 - 5 , 10 ") == [(1, 5), (10, 10)]

    def test_rejects_zero_or_negative(self):
        with pytest.raises(ValueError):
            parse_chapter_spec("0")
        with pytest.raises(ValueError):
            parse_chapter_spec("0-5")

    def test_rejects_inverted_range(self):
        with pytest.raises(ValueError):
            parse_chapter_spec("5-3")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            parse_chapter_spec("abc")
        with pytest.raises(ValueError):
            parse_chapter_spec("1-2-3")


class TestChapterInSpec:
    def test_none_spec_matches_everything(self):
        assert chapter_in_spec(1, None)
        assert chapter_in_spec(9999, None)

    def test_single_chapter_match(self):
        spec = parse_chapter_spec("5")
        assert chapter_in_spec(5, spec)
        assert not chapter_in_spec(4, spec)
        assert not chapter_in_spec(6, spec)

    def test_closed_range_match(self):
        spec = parse_chapter_spec("1-5")
        for n in (1, 3, 5):
            assert chapter_in_spec(n, spec)
        for n in (0, 6, 100):
            assert not chapter_in_spec(n, spec)

    def test_right_open_range_match(self):
        spec = parse_chapter_spec("20-")
        assert chapter_in_spec(20, spec)
        assert chapter_in_spec(9999, spec)
        assert not chapter_in_spec(19, spec)

    def test_mixed_spec(self):
        spec = parse_chapter_spec("1,5-10,20-")
        included = [1, 5, 7, 10, 20, 50]
        excluded = [2, 4, 11, 19]
        for n in included:
            assert chapter_in_spec(n, spec), f"expected {n} in spec"
        for n in excluded:
            assert not chapter_in_spec(n, spec), f"expected {n} not in spec"
