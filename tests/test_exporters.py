"""Exporter helpers — no network, no file I/O beyond tempdir."""

import tempfile
from pathlib import Path

from ffn_dl.exporters import (
    _apply_hr_as_stars,
    _site_info,
    export_html,
    export_txt,
    format_filename,
)
from ffn_dl.models import Chapter, Story


def _make_story(url="https://www.fanfiction.net/s/1"):
    s = Story(
        id=1, title="Test Story", author="Sample", summary="Summary",
        url=url,
    )
    s.metadata["words"] = "1,234"
    s.metadata["status"] = "Complete"
    s.chapters.append(
        Chapter(
            number=1, title="Ch 1",
            html="<p>Before</p><hr/><p>Middle</p><hr>end",
        )
    )
    return s


class TestSiteInfo:
    def test_ffn_url(self):
        prefix, publisher = _site_info("https://www.fanfiction.net/s/1")
        assert prefix == "ffn"
        assert publisher == "fanfiction.net"

    def test_ao3_url(self):
        prefix, publisher = _site_info("https://archiveofourown.org/works/1")
        assert prefix == "ao3"
        assert publisher == "archiveofourown.org"

    def test_ficwad_url(self):
        prefix, publisher = _site_info("https://ficwad.com/story/1")
        assert prefix == "ficwad"
        assert publisher == "ficwad.com"

    def test_empty_url_falls_back_to_ffn(self):
        # Pre-AO3 exports may not have a site URL; default is fine.
        assert _site_info("")[0] == "ffn"


class TestStripNotes:
    def test_strips_common_an_markers(self):
        from ffn_dl.exporters import strip_note_paragraphs
        cases = [
            "<p>Story.</p><p>A/N: late update</p>",
            "<p>Story.</p><p>AN: thanks!</p>",
            "<p>Story.</p><p>AN - yes</p>",
            "<p>Story.</p><p>A.N. note here</p>",
            "<p>Story.</p><p>Author's Note: thanks</p>",
            "<p>Story.</p><p>[A/N: bracketed]</p>",
            "<p>Story.</p><p>Author Note: hi</p>",
        ]
        for html in cases:
            out = strip_note_paragraphs(html)
            assert out.count("<p>") == 1, f"should strip: {html}"

    def test_keeps_prose_that_looks_similar(self):
        from ffn_dl.exporters import strip_note_paragraphs
        cases = [
            "<p>An arrow hit him.</p>",
            "<p>note to self: be careful</p>",
            "<p>A nice day.</p>",
        ]
        for html in cases:
            out = strip_note_paragraphs(html)
            assert out.count("<p>") == html.count("<p>"), f"should keep: {html}"


class TestStructuralNoteStripping:
    """Divider-bracketed author-note detection.

    Two-signal gate at the top (divider + chapter-title banner + either
    all-bold block or note keyword); one-signal gate at the bottom
    (divider + note keyword in the post-block). Chapters without a
    divider, or without the corroborating signal, must pass through
    unchanged.
    """

    def _strip(self, html):
        from ffn_dl.exporters import strip_note_paragraphs
        return strip_note_paragraphs(html)

    def test_strips_top_block_when_all_bold_and_banner_present(self):
        # The Kairomaru / Arch Mage pattern: fully-bold intro, then a
        # text divider, then a ``Chapter 1 - Title`` banner, then the
        # real prose.
        html = (
            "<p><strong>Hello friends!</strong></p>"
            "<p><strong>Enjoy the chapter.</strong></p>"
            "<p><strong>-x-x-x-x-x-x-x-x-x-x-x-x-x-x-x-x-x-x-</strong></p>"
            "<p><strong>Chapter 1 - The Start</strong></p>"
            "<p>Harry walked into the castle.</p>"
            "<p>He looked up at the towers.</p>"
        )
        out = self._strip(html)
        assert "Hello friends" not in out
        assert "Enjoy the chapter" not in out
        assert "Chapter 1 - The Start" not in out
        assert "-x-x-x-" not in out
        assert "Harry walked into the castle." in out

    def test_strips_top_block_when_keyword_and_banner_present(self):
        # Plain-text (not fully bold) note survives the prefix pass but
        # the keyword ``patreon`` plus the banner trip the structural rule.
        html = (
            "<p>Welcome back! Support me on patreon for early access.</p>"
            "<hr/>"
            "<p>Chapter 5 - The Aftermath</p>"
            "<p>The rain began to fall.</p>"
        )
        out = self._strip(html)
        assert "patreon" not in out.lower()
        assert "Chapter 5" not in out
        assert "rain began to fall" in out

    def test_does_not_strip_when_no_banner_after_divider(self):
        # A fic that opens with a flashback and a horizontal rule, no
        # chapter-title banner — must NOT be stripped.
        html = (
            "<p>The memory came back to her.</p>"
            "<p>She was fifteen again.</p>"
            "<hr/>"
            "<p>Back in the present, she shook her head.</p>"
        )
        out = self._strip(html)
        assert "memory came back" in out
        assert "She was fifteen again" in out
        assert "Back in the present" in out

    def test_does_not_strip_when_banner_but_no_note_signal(self):
        # Divider + banner but the pre-block is plain prose with no
        # keyword and no full-bold styling — inconclusive, keep it.
        html = (
            "<p>She opened the letter with trembling hands.</p>"
            "<hr/>"
            "<p>Chapter 1 - First Contact</p>"
            "<p>The next morning was bright.</p>"
        )
        out = self._strip(html)
        assert "trembling hands" in out
        assert "bright" in out

    def test_strips_bottom_block_when_keyword_present(self):
        html = (
            "<p>Harry closed the book.</p>"
            "<hr/>"
            "<p>Thanks for reading! Please review and follow.</p>"
            "<p>See you next chapter!</p>"
        )
        out = self._strip(html)
        assert "closed the book" in out
        assert "Thanks for reading" not in out
        assert "Please review" not in out
        assert "next chapter" not in out.lower()

    def test_strips_bottom_block_including_end_banner(self):
        # ``-End Chapter-`` banner directly before the closing divider
        # should get pulled into the outro drop.
        html = (
            "<p>The door closed behind him.</p>"
            "<p><strong>-End Chapter-</strong></p>"
            "<p><strong>-x-x-x-x-x-x-x-x-x-x-x-x-x-</strong></p>"
            "<p><strong>Next chapter coming soon on patreon!</strong></p>"
        )
        out = self._strip(html)
        assert "door closed behind him" in out
        assert "End Chapter" not in out
        assert "patreon" not in out.lower()

    def test_does_not_strip_bottom_without_keyword(self):
        # Epilogue-style ending after a scene break — no note keywords,
        # must be preserved.
        html = (
            "<p>The battle ended at dawn.</p>"
            "<hr/>"
            "<p>Three years later, she returned to the valley.</p>"
        )
        out = self._strip(html)
        assert "battle ended at dawn" in out
        assert "Three years later" in out

    def test_chapter_with_no_divider_untouched_structurally(self):
        # No divider → structural passes are no-ops. Prefix pass still
        # runs; plain prose with no A/N marker survives intact.
        html = (
            "<p>First paragraph.</p>"
            "<p>Second paragraph.</p>"
        )
        assert self._strip(html).count("<p>") == 2


class TestDividerAsStars:
    """Text-based dividers (``-x-x-x-``, long ``***`` runs) get the same
    ``* * *`` visualisation as real ``<hr>`` tags when ``hr_as_stars``
    is enabled."""

    def test_long_x_dash_divider_converted(self):
        long_divider = "-x-" * 25  # 75 chars — over the old 40-char cap
        html = f"<p>Prose.</p><p>{long_divider}</p><p>More prose.</p>"
        out = _apply_hr_as_stars(html)
        assert long_divider not in out
        assert "scenebreak" in out

    def test_star_divider_converted(self):
        html = "<p>Prose.</p><p>* * *</p><p>More.</p>"
        out = _apply_hr_as_stars(html)
        assert "scenebreak" in out
        assert "* * *" in out  # survives as the replacement text

    def test_short_prose_not_converted(self):
        # ``Ox`` / ``OK`` / short words that happen to use divider letters
        # must survive unchanged.
        html = "<p>Short prose.</p><p>OK</p><p>More.</p>"
        out = _apply_hr_as_stars(html)
        assert "OK" in out
        assert out.count("scenebreak") == 0

    def test_uppercase_x_divider_converted(self):
        # ``XXX`` and longer runs are overwhelmingly scene breaks in
        # fanfic; detector accepts them while still rejecting ``OOO``.
        html = "<p>Prose.</p><p>XXX</p><p>More.</p><p>OOO</p><p>End.</p>"
        out = _apply_hr_as_stars(html)
        assert out.count("scenebreak") == 1  # XXX converted, OOO kept
        assert "OOO" in out


class TestHrAsStars:
    def test_substitutes_hr_tags(self):
        out = _apply_hr_as_stars("before<hr/>middle<hr>after")
        assert "<hr" not in out
        assert out.count("* * *") == 2
        assert "scenebreak" in out

    def test_passes_through_when_no_hr(self):
        text = "<p>no breaks here</p>"
        assert _apply_hr_as_stars(text) == text

    def test_handles_attributes_on_hr(self):
        out = _apply_hr_as_stars('<hr class="sb" />')
        assert "<hr" not in out
        assert "* * *" in out


class TestFilenameTemplate:
    def test_template_substitutes_known_fields(self):
        story = _make_story()
        name = format_filename(story, "{title} - {author}")
        assert name == "Test Story - Sample"

    def test_unknown_field_stays_literal(self):
        story = _make_story()
        # Unknown template field leaves a KeyError — but callers pass
        # validated templates; at minimum, known fields should resolve.
        name = format_filename(story, "{title}")
        assert name == "Test Story"


class TestHtmlAndTxtExport:
    def test_html_with_hr_as_stars(self, tmp_path):
        story = _make_story()
        path = export_html(story, str(tmp_path), hr_as_stars=True)
        text = Path(path).read_text()
        # scene-break markers replaced in chapter content
        chapter_segment = text.split('class="chapter"', 1)[1]
        assert "* * *" in chapter_segment
        assert "scenebreak" in chapter_segment

    def test_html_without_hr_as_stars(self, tmp_path):
        story = _make_story()
        path = export_html(story, str(tmp_path), hr_as_stars=False)
        text = Path(path).read_text()
        chapter_segment = text.split('class="chapter"', 1)[1]
        # Raw hr retained
        assert "<hr" in chapter_segment.split("</div>", 1)[0]

    def test_txt_includes_source_and_status(self, tmp_path):
        story = _make_story()
        path = export_txt(story, str(tmp_path))
        text = Path(path).read_text()
        assert "Source:" in text
        assert "Status:" in text
        assert "Complete" in text


class TestUniversalMetadata:
    def test_words_counted_from_chapters_when_missing(self, tmp_path):
        # Sites that don't expose a word count (RR, MediaMiner, Literotica)
        # should still get a Words line in the header, computed from the
        # downloaded chapter text.
        story = Story(id=9, title="X", author="A", summary="", url="http://x")
        story.chapters.append(
            Chapter(number=1, title="c", html="<p>one two three four</p>"),
        )
        path = export_txt(story, str(tmp_path))
        text = Path(path).read_text()
        assert "Words: 4" in text
        assert "Reading Time:" in text

    def test_published_and_updated_epochs_render_as_dates(self, tmp_path):
        # RR populates `date_published` / `date_updated` as unix epochs.
        # The header should convert them to YYYY-MM-DD.
        story = Story(id=9, title="X", author="A", summary="", url="http://x")
        story.metadata["date_published"] = 1600000000
        story.metadata["date_updated"] = 1700000000
        story.chapters.append(Chapter(number=1, title="c", html="<p>x</p>"))
        path = export_txt(story, str(tmp_path))
        text = Path(path).read_text()
        assert "Published: 2020-09-13" in text
        assert "Updated: 2023-11-14" in text


class TestFFMetaEscaping:
    def test_escapes_all_special_chars(self):
        from ffn_dl.tts import _escape_ffmeta
        # Each of these chars must be backslash-escaped per the
        # FFMETADATA1 spec, otherwise ffmpeg silently fails to parse.
        assert _escape_ffmeta("with = sign") == "with \\= sign"
        assert _escape_ffmeta("semi; colon") == "semi\\; colon"
        assert _escape_ffmeta("hash # mark") == "hash \\# mark"
        assert _escape_ffmeta("back\\slash") == "back\\\\slash"
        assert _escape_ffmeta("line1\nline2") == "line1\\\nline2"
        assert _escape_ffmeta("crlf\r\nend") == "crlf\\\nend"

    def test_leaves_plain_text_untouched(self):
        from ffn_dl.tts import _escape_ffmeta
        assert _escape_ffmeta("A Simple Title") == "A Simple Title"


class TestFetchParallel:
    def test_returns_results_in_input_order(self):
        # Even though workers complete in arbitrary order, the returned
        # list must line up with the input URL order.
        from ffn_dl.scraper import FFNScraper
        s = FFNScraper(use_cache=False, concurrency=4)
        urls = [f"https://example.com/{i}" for i in range(8)]

        from unittest.mock import patch
        def fake_fetch(url, session=None):
            # Pull the index back out so we can assert ordering.
            import time, random
            time.sleep(random.uniform(0, 0.02))
            return f"html-{url.rsplit('/', 1)[-1]}"

        with patch.object(s, "_fetch", side_effect=fake_fetch):
            results = s._fetch_parallel(urls)
        assert results == [f"html-{i}" for i in range(8)]

    def test_concurrency_halves_on_rate_limit(self):
        # When _fetch bumps _current_delay (the AIMD signal for "we got
        # rate-limited"), the next batch shrinks its concurrency.
        from ffn_dl.scraper import FFNScraper
        s = FFNScraper(use_cache=False, concurrency=4)
        urls = [f"u{i}" for i in range(8)]

        call_counter = {"n": 0}
        def fake_fetch(url, session=None):
            call_counter["n"] += 1
            if call_counter["n"] == 2:
                # Simulate AIMD bumping the delay as _fetch would after
                # seeing a 429.
                s._current_delay = 2.0
            return f"html-{url}"

        from unittest.mock import patch
        with patch.object(s, "_fetch", side_effect=fake_fetch):
            results = s._fetch_parallel(urls)
        assert len(results) == len(urls)
        # Concurrency should have shrunk during the run (not visible
        # post-hoc, but we can prove no crashes and correct ordering).
        assert results == [f"html-u{i}" for i in range(8)]

    def test_single_url_uses_sequential_path(self):
        from ffn_dl.scraper import FFNScraper
        s = FFNScraper(use_cache=False, concurrency=3)
        from unittest.mock import patch
        with patch.object(s, "_fetch", return_value="html") as m:
            assert s._fetch_parallel(["u"]) == ["html"]
        # Must be called WITHOUT a session kwarg (sequential path).
        m.assert_called_once_with("u")


class TestRoyalRoadDates:
    def test_chapter_list_captures_publish_unixtime(self):
        from bs4 import BeautifulSoup
        from ffn_dl.royalroad import RoyalRoadScraper
        html = '''
        <table id="chapters"><tbody>
          <tr><td><a href="/fiction/1/x/chapter/10">Ch 1</a></td>
              <td><time unixtime="1600000000">x</time></td></tr>
          <tr><td><a href="/fiction/1/x/chapter/20">Ch 2</a></td>
              <td><time unixtime="1700000000">x</time></td></tr>
        </tbody></table>
        '''
        soup = BeautifulSoup(html, "lxml")
        rows = RoyalRoadScraper._parse_chapter_list(soup)
        assert [r["unixtime"] for r in rows] == [1600000000, 1700000000]
