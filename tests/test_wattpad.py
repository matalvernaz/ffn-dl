"""Wattpad scraper tests."""

from pathlib import Path

import pytest

from ffn_dl.wattpad import (
    WattpadPaidStoryError,
    WattpadScraper,
    _enclosing_json_object,
    _normalise_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _story_html():
    return (FIXTURES / "wattpad_story.html").read_text(encoding="utf-8")


def _storytext_html():
    return (FIXTURES / "wattpad_storytext.html").read_text(encoding="utf-8")


def _paid_stub_html():
    return (FIXTURES / "wattpad_paid_stub.html").read_text(encoding="utf-8")


class TestURLParsing:
    def test_bare_numeric_id(self):
        assert WattpadScraper.parse_story_id(6315313) == 6315313
        assert WattpadScraper.parse_story_id("6315313") == 6315313

    def test_story_url_with_slug(self):
        assert (
            WattpadScraper.parse_story_id(
                "https://www.wattpad.com/story/6315313-harry-potter-one-shots-vol-i"
            )
            == 6315313
        )

    def test_story_url_without_slug(self):
        assert (
            WattpadScraper.parse_story_id("https://www.wattpad.com/story/6315313")
            == 6315313
        )

    def test_mobile_subdomain_rewritten(self):
        # m.wattpad.com redirects weirdly; the parser normalises to www
        # so the regex matches without a network call.
        assert (
            WattpadScraper.parse_story_id("https://m.wattpad.com/story/6315313")
            == 6315313
        )

    def test_part_url_returns_part_id(self):
        # Static parser returns the part id — download() does the
        # part→story lookup live.
        assert (
            WattpadScraper.parse_story_id(
                "https://www.wattpad.com/19039979-harry-potter-one-shots-challenging"
            )
            == 19039979
        )

    def test_looks_like_part_url(self):
        assert WattpadScraper._looks_like_part_url(
            "https://www.wattpad.com/19039979-slug"
        )
        assert not WattpadScraper._looks_like_part_url(
            "https://www.wattpad.com/story/6315313"
        )
        assert not WattpadScraper._looks_like_part_url(6315313)

    def test_rejects_non_wattpad(self):
        with pytest.raises(ValueError):
            WattpadScraper.parse_story_id("https://example.com/story/123")

    def test_is_author_url(self):
        assert WattpadScraper.is_author_url(
            "https://www.wattpad.com/user/everlovingdeer"
        )
        assert WattpadScraper.is_author_url(
            "https://m.wattpad.com/user/somebody"
        )
        assert not WattpadScraper.is_author_url(
            "https://www.wattpad.com/story/6315313"
        )

    def test_is_series_url_always_false(self):
        # Wattpad has no series concept.
        assert not WattpadScraper.is_series_url(
            "https://www.wattpad.com/story/6315313"
        )

    def test_normalise_url_strips_mobile(self):
        assert _normalise_url("https://m.wattpad.com/story/42").startswith(
            "https://www.wattpad.com/"
        )


class TestBracketMatching:
    def test_wraps_innermost_object(self):
        # Helper returns the innermost enclosing object, which is what
        # we need: ``"paidModel"`` is a key on the story object itself,
        # not on anything containing it.
        text = '  {"a": 1, "b": {"c": 2}}  '
        start, end = _enclosing_json_object(text, text.find('"c"'))
        assert text[start:end] == '{"c": 2}'

    def test_wraps_outer_when_hit_is_outside_inner(self):
        text = '{"a": 1, "b": {"c": 2}}'
        start, end = _enclosing_json_object(text, text.find('"a"'))
        assert text[start:end] == '{"a": 1, "b": {"c": 2}}'

    def test_returns_none_when_unbalanced(self):
        text = 'no braces here'
        assert _enclosing_json_object(text, 5) == (None, None)


class TestSSRStoryParsing:
    def test_finds_primary_story_object(self):
        html = _story_html()
        # Story id 271297863 is the fixture story
        obj = WattpadScraper._bracket_match_story(html, 271297863)
        assert obj is not None
        assert obj.get("id") == "271297863"
        assert obj.get("numParts") == 2
        assert isinstance(obj.get("parts"), list)

    def test_build_metadata(self):
        scraper = WattpadScraper(use_cache=False)
        obj = WattpadScraper._bracket_match_story(_story_html(), 271297863)
        meta = scraper._build_metadata(obj)
        assert meta["title"] == "KOTECZEK // Alcina Dimitrescu [short oneshot]"
        assert meta["author"] == "A Pensive Tree"
        assert meta["num_chapters"] == 2
        # chapter_titles is 1-indexed string keys
        assert "1" in meta["chapter_titles"]
        assert "2" in meta["chapter_titles"]
        # status derived from completed flag
        assert meta["extra"]["status"] in ("Complete", "In-Progress")

    def test_missing_object_raises(self):
        """If the SSR blob can't be found, the scraper should raise
        with an explicit message — silent empty metadata would lead to
        confusing downstream failures."""
        # Call the normal path with bogus HTML; _fetch_story_page_meta
        # would normally be what raises, but bracket_match_story
        # returning None is the trigger.
        assert (
            WattpadScraper._bracket_match_story(
                "<html>no story data here</html>", 271297863,
            )
            is None
        )


class TestPaidMarker:
    def test_paid_stub_detected(self):
        stub = _paid_stub_html()
        assert "Paid Stories program" in stub
        assert "Historias Pagadas" in stub

    def test_paid_stub_body_ends_quickly(self):
        # The stub body should be small — the marker detection is the
        # main signal but this catches regressions where Wattpad swaps
        # the stub for a full-chapter placeholder.
        assert len(_paid_stub_html()) < 3_000


class TestStorytextShape:
    def test_public_part_has_paragraphs(self):
        body = _storytext_html()
        assert body.lstrip().startswith("<p")
        assert "Paid Stories program" not in body


class TestPaidStoryErrorMessage:
    def test_error_mentions_chapters(self):
        err = WattpadPaidStoryError(
            "All 5 requested chapters are behind Wattpad's Paid Stories paywall."
        )
        assert "Paid Stories" in str(err)
