"""Updater — URL/status extraction and chapter counting."""

from pathlib import Path

from ffn_dl.exporters import export_epub, export_html, export_txt
from ffn_dl.models import Chapter, Story
from ffn_dl.updater import count_chapters, extract_source_url, extract_status


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
        import pytest
        with pytest.raises(ValueError):
            extract_source_url(path)
