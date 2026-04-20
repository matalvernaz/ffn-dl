"""Updater — URL/status extraction and chapter counting."""

from pathlib import Path

import pytest

from ffn_dl.exporters import export_epub, export_html, export_txt
from ffn_dl.models import Chapter, Story
from ffn_dl.updater import (
    ChaptersNotReadableError,
    count_chapters,
    extract_source_url,
    extract_status,
    read_chapters,
)


def _story(url):
    s = Story(
        id=1, title="Probe", author="A",
        summary="S", url=url,
    )
    s.metadata["status"] = "Complete"
    s.chapters.append(Chapter(number=1, title="Ch1", html="<p>hello</p>"))
    s.chapters.append(Chapter(number=2, title="Ch2", html="<p>world</p>"))
    return s


class TestRoundTripTxt:
    def test_txt_roundtrips_url_status_count(self, tmp_path):
        story = _story("https://www.fanfiction.net/s/4242")
        path = export_txt(story, str(tmp_path))
        assert count_chapters(path) == 2
        assert extract_source_url(path) == "https://www.fanfiction.net/s/4242"
        assert extract_status(path) == "Complete"


class TestRoundTripHtml:
    def test_html_roundtrips_url_status_count(self, tmp_path):
        story = _story("https://archiveofourown.org/works/4242")
        path = export_html(story, str(tmp_path))
        assert count_chapters(path) == 2
        assert extract_source_url(path) == "https://archiveofourown.org/works/4242"
        assert extract_status(path) == "Complete"

    def test_count_chapters_html_tolerates_attribute_variants(self, tmp_path):
        """The regex that replaced BS4 has to match real-world markup
        variants: attribute order (class before id vs after), whitespace
        around ``=``, and class lists containing ``chapter`` plus other
        tokens. These forms all appear in past ffn-dl outputs and
        hand-edited exports users send us."""
        path = tmp_path / "variants.html"
        path.write_text(
            "<html><body>\n"
            '<div class="chapter">one</div>\n'
            '<div id="x" class="chapter">two</div>\n'
            '<div class="fancy chapter">three</div>\n'
            '<div class = "chapter">four</div>\n'
            '<DIV CLASS="chapter">five</DIV>\n'
            # Non-chapter div should not match
            '<div class="chapterish">nope</div>\n'
            '<div class="chapter-title">nope</div>\n'
            "</body></html>\n",
            encoding="utf-8",
        )
        assert count_chapters(path) == 5


class TestRoundTripEpub:
    def test_epub_roundtrips_url_and_count(self, tmp_path):
        story = _story("https://archiveofourown.org/works/4242")
        try:
            path = export_epub(story, str(tmp_path))
        except ImportError:
            import pytest
            pytest.skip("ebooklib not installed in this environment")
        assert count_chapters(path) == 2
        assert extract_source_url(path) == "https://archiveofourown.org/works/4242"


class TestFallbackURL:
    def test_plain_ao3_url_in_body_is_found(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("Random preamble\nsee https://archiveofourown.org/works/9999 here\n")
        assert extract_source_url(path) == "https://archiveofourown.org/works/9999"

    def test_no_url_raises(self, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_text("nothing here\n")
        with pytest.raises(ValueError):
            extract_source_url(path)


class TestReadChapters:
    """read_chapters() recovers ordered Chapter objects from existing exports.

    Used by the merge-in-place update flow to avoid re-downloading all
    chapters just to re-export a file. Round-trip correctness (title,
    number, body HTML) is what makes the shortcut safe.
    """

    def _story(self):
        s = Story(
            id=1, title="T", author="A", summary="S",
            url="https://www.fanfiction.net/s/1",
        )
        s.metadata["status"] = "In-Progress"
        s.chapters = [
            Chapter(number=1, title="First", html="<p>one</p><p>two</p>"),
            Chapter(number=2, title="Second", html="<p>three</p>"),
            Chapter(number=3, title="Third", html="<p>four</p>"),
        ]
        return s

    def test_html_roundtrip_preserves_number_title_and_body(self, tmp_path):
        path = export_html(self._story(), str(tmp_path))
        back = read_chapters(path)
        assert [c.number for c in back] == [1, 2, 3]
        assert [c.title for c in back] == ["First", "Second", "Third"]
        assert "<p>one</p>" in back[0].html
        assert "<p>two</p>" in back[0].html
        assert "<p>three</p>" in back[1].html

    def test_epub_roundtrip_preserves_number_title_and_body(self, tmp_path):
        try:
            path = export_epub(self._story(), str(tmp_path))
        except ImportError:
            pytest.skip("ebooklib not installed")
        back = read_chapters(path)
        assert [c.number for c in back] == [1, 2, 3]
        assert [c.title for c in back] == ["First", "Second", "Third"]
        assert "<p>one</p>" in back[0].html

    def test_html_re_export_after_roundtrip_has_all_chapters(self, tmp_path):
        """Read chapters out, add a new one, re-export, count should match."""
        original = export_html(self._story(), str(tmp_path), template="orig")
        recovered = read_chapters(original)

        merged_story = self._story()
        merged_story.chapters = recovered + [
            Chapter(number=4, title="Fourth", html="<p>five</p>"),
        ]
        re_exported = export_html(merged_story, str(tmp_path), template="merged")
        assert count_chapters(re_exported) == 4

    def test_txt_refuses_with_clear_error(self, tmp_path):
        path = export_txt(self._story(), str(tmp_path))
        with pytest.raises(ChaptersNotReadableError, match="lossy"):
            read_chapters(path)

    def test_unsupported_extension_raises(self, tmp_path):
        path = tmp_path / "story.mobi"
        path.write_text("unused")
        with pytest.raises(ChaptersNotReadableError, match="Unsupported"):
            read_chapters(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ChaptersNotReadableError, match="not found"):
            read_chapters(tmp_path / "nope.html")

    def test_non_ffn_dl_html_raises(self, tmp_path):
        """A random HTML file with no ffn-dl chapter markup can't merge."""
        path = tmp_path / "foreign.html"
        path.write_text(
            "<html><body><h1>Not an ffn-dl file</h1>"
            "<p>Just some text.</p></body></html>"
        )
        with pytest.raises(ChaptersNotReadableError, match="chapter blocks"):
            read_chapters(path)

    def test_html_chapter_title_with_entities_roundtrips(self, tmp_path):
        """Chapter titles containing ``&``, ``<``, ``"`` go through the
        exporter's ``escape()`` into ``&amp;`` / ``&lt;`` / ``&quot;``
        and must come back as the same escaped text — the re-exporter
        will re-escape on output, so double-escape would corrupt titles.
        """
        s = Story(
            id=1, title="T", author="A", summary="S",
            url="https://www.fanfiction.net/s/1",
        )
        s.metadata["status"] = "In-Progress"
        s.chapters = [
            Chapter(number=1, title='Amy & Bob "Run"', html="<p>x</p>"),
            Chapter(number=2, title="A < B", html="<p>y</p>"),
        ]
        path = export_html(s, str(tmp_path))
        back = read_chapters(path)
        # The exporter writes &amp; etc.; read_chapters returns the
        # escaped form so the next export's escape() doesn't double-up.
        assert "&amp;" in back[0].title
        assert "&lt;" in back[1].title

    def test_html_chapter_body_with_nested_divs(self, tmp_path):
        """Chapter bodies containing nested ``<div>`` blocks (authors
        often wrap scene breaks or notes in divs) must not confuse the
        block terminator — the regex anchors on ``</div><hr>``, which
        only appears at the *outer* chapter boundary in ffn-dl output.
        """
        s = Story(
            id=1, title="T", author="A", summary="S",
            url="https://www.fanfiction.net/s/1",
        )
        s.metadata["status"] = "In-Progress"
        s.chapters = [
            Chapter(
                number=1, title="Nested",
                html='<div class="note"><p>A/N</p></div><p>Body.</p>',
            ),
            Chapter(number=2, title="Next", html="<p>next</p>"),
        ]
        path = export_html(s, str(tmp_path))
        back = read_chapters(path)
        assert len(back) == 2
        assert "A/N" in back[0].html
        assert "Body." in back[0].html
        assert "next" in back[1].html

    def test_epub_with_nav_cover_items_ignored(self, tmp_path):
        """EPUB files contain more than chapters (nav, cover, title
        page, CSS). Only ``chapter_N.xhtml`` items should be returned,
        and the non-chapter items must not raise or count."""
        try:
            path = export_epub(self._story(), str(tmp_path))
        except ImportError:
            pytest.skip("ebooklib not installed")
        back = read_chapters(path)
        # _story() has 3 chapters — anything else (nav, title, cover)
        # must be filtered out.
        assert len(back) == 3
