"""Wattpad scraper tests."""

from pathlib import Path

import pytest

from ffn_dl.wattpad import (
    WattpadPaidStoryError,
    WattpadScraper,
    _MAX_PART_PAGES,
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

    def test_ignores_braces_inside_strings(self):
        # A raw "{" or "}" inside a JSON string literal must not move
        # the depth counter. An older implementation counted every brace
        # and would have split the enclosing object in the middle of the
        # literal.
        text = '{"title": "Wait for it }", "id": 42}'
        start, end = _enclosing_json_object(text, text.find('"id"'))
        assert text[start:end] == text
        # And round-trips through json.loads without slicing errors.
        import json
        assert json.loads(text[start:end]) == {"title": "Wait for it }", "id": 42}

    def test_ignores_escaped_quote_inside_string(self):
        # An escaped quote (\") must NOT close the string, so a later
        # brace inside that string still has to be ignored.
        text = r'{"quote": "he said \"hi }\" then left", "n": 1}'
        start, end = _enclosing_json_object(text, text.find('"n"'))
        import json
        assert json.loads(text[start:end])["n"] == 1

    def test_escaped_backslash_terminates_escape(self):
        # "\\" is a literal backslash followed by a quote that DOES close
        # the string. The following brace must be counted again.
        text = r'{"path": "C:\\", "q": {"r": 1}}'
        start, end = _enclosing_json_object(text, text.find('"r"'))
        import json
        assert json.loads(text[start:end]) == {"r": 1}

    def test_innermost_when_nested_deeply(self):
        text = '{"a": {"b": {"c": {"d": "hit"}}}}'
        start, end = _enclosing_json_object(text, text.find('"hit"'))
        import json
        assert json.loads(text[start:end]) == {"d": "hit"}

    def test_unbalanced_open_brace_returns_none(self):
        # Stray "{" with no matching close must not crash; we return
        # (None, None) rather than hand back a span that doesn't parse.
        text = '  { "a": 1 '
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


class TestTruncationCap:
    """When storytext pagination blows past the safety cap, the chapter
    has to come back marked as truncated. The output HTML carries a
    reader-visible notice and the cache path skips persisting the
    partial body so a future run tries again."""

    def _fake_endless_pages(self, page_size=2000):
        """Return a _fetch that serves a non-trivial body forever.
        The 64-byte "end of part" heuristic won't fire, so only the
        safety cap stops the loop."""
        body = "<p>" + ("x" * page_size) + "</p>"
        return lambda url, session=None: body

    def test_truncation_flag_set_on_cap_hit(self):
        scraper = WattpadScraper(use_cache=False, delay_floor=0.0)
        scraper._fetch = self._fake_endless_pages()
        scraper._delay = lambda: None
        html, is_paid, truncated = scraper._fetch_part_text(999)
        assert truncated is True
        assert is_paid is False
        # Truncation notice is surfaced to the reader.
        assert "truncation" in html.lower() or "truncated" in html.lower()

    def test_truncated_notice_mentions_max_pages(self):
        scraper = WattpadScraper(use_cache=False, delay_floor=0.0)
        scraper._fetch = self._fake_endless_pages()
        scraper._delay = lambda: None
        html, _, truncated = scraper._fetch_part_text(999)
        assert truncated is True
        assert str(_MAX_PART_PAGES) in html

    def test_normal_chapter_not_marked_truncated(self):
        """A chapter that terminates naturally (empty page) must not be
        flagged, or every download would show the truncation notice."""
        scraper = WattpadScraper(use_cache=False, delay_floor=0.0)
        pages = [
            "<p>Real content one.</p>",
            "<p>Real content two.</p>",
            "",  # empty terminates the loop
        ]
        call_count = {"n": 0}

        def fake_fetch(url, session=None):
            body = pages[call_count["n"]] if call_count["n"] < len(pages) else ""
            call_count["n"] += 1
            return body

        scraper._fetch = fake_fetch
        scraper._delay = lambda: None
        html, is_paid, truncated = scraper._fetch_part_text(1)
        assert truncated is False
        assert "truncat" not in html.lower()
        assert "Real content" in html

    def test_truncated_chapter_not_cached(self, tmp_path):
        """Persisting a truncated body would lock the library into the
        bad copy — future runs should refetch in case upstream fixed
        itself. Exercised via the full ``download()`` path to cover
        the caching branch."""
        scraper = WattpadScraper(
            use_cache=True, cache_dir=tmp_path, delay_floor=0.0,
        )
        scraper._delay = lambda: None

        # Fake the SSR meta so download() skips the public story fetch.
        fake_obj = {
            "id": "1", "title": "T", "user": {"name": "A"},
            "description": "d", "numParts": 1,
            "parts": [{"id": 99, "title": "Only Part"}],
        }
        scraper._resolve_story_id = lambda url: 1
        scraper._fetch_story_page_meta = lambda sid: fake_obj

        # Endless-page fake to drive truncation.
        body = "<p>" + ("x" * 5000) + "</p>"
        scraper._fetch = lambda url, session=None: body

        story = scraper.download("https://www.wattpad.com/story/1")
        # Chapter was built and returned with the truncation notice.
        assert len(story.chapters) == 1
        assert "truncat" in story.chapters[0].html.lower()
        # Cache was NOT written for the truncated chapter.
        cache_hit = scraper._load_chapter_cache(1, 1)
        assert cache_hit is None
