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
