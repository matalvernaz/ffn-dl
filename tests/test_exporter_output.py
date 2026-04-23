"""End-to-end tests for what exporters actually write to disk.

The existing ``test_exporters.py`` covers helper functions (filename
templating, hr-as-stars substitution, author-note stripping) but not
the full file produced by ``export_txt`` / ``export_html`` /
``export_epub``. That's where the reader-facing bugs live:

* a missing metadata field that stops showing up in a generated TOC,
* a chapter numbering regression that silently loses the TOC anchor,
* an EPUB with broken mimetype ordering that refuses to open on
  strict readers (Kobo in particular).

Where possible we check structural invariants rather than byte-exact
output, so a CSS tweak or a cover-image addition doesn't need every
golden test regenerated.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from ffn_dl.exporters import export_epub, export_html, export_txt
from ffn_dl.models import Chapter, Story


def _sample_story(
    *,
    url: str = "https://www.fanfiction.net/s/424242",
    num_chapters: int = 3,
    title: str = "A Sample Story",
    author: str = "An Author",
    unicode_title: bool = False,
) -> Story:
    if unicode_title:
        title = "世界 — a story with « diacritics » and 日本語"
    s = Story(
        id=424242,
        title=title,
        author=author,
        summary="Three paragraphs of prose spread across the chapters.",
        url=url,
        author_url="https://www.fanfiction.net/u/12345/An-Author",
    )
    s.metadata.update({
        "words": "5,000",
        "status": "Complete",
        "rating": "T",
        "language": "English",
    })
    for i in range(1, num_chapters + 1):
        s.chapters.append(Chapter(
            number=i,
            title=f"Chapter {i} — The {'First' if i == 1 else 'Next'} Bit",
            html=(
                f"<p>Opening paragraph of chapter {i}.</p>"
                "<hr/>"
                f"<p>Middle paragraph of chapter {i} with <em>emphasis</em>.</p>"
                f"<p>Closing paragraph — the end of chapter {i}.</p>"
            ),
        ))
    return s


# ── TXT ───────────────────────────────────────────────────────────

class TestTxtExportStructure:
    def test_metadata_block_includes_title_author_and_summary(self, tmp_path):
        path = export_txt(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert "Title: A Sample Story" in body
        assert "Author: An Author" in body
        assert "Summary:" in body

    def test_separator_between_metadata_and_chapters(self, tmp_path):
        path = export_txt(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        # One line of "=" characters separates metadata from prose.
        assert "=" * 60 in body

    def test_each_chapter_rendered_with_marked_header(self, tmp_path):
        path = export_txt(_sample_story(num_chapters=3), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert body.count("--- Chapter 1") == 1
        assert body.count("--- Chapter 2") == 1
        assert body.count("--- Chapter 3") == 1

    def test_all_chapter_bodies_present(self, tmp_path):
        path = export_txt(_sample_story(num_chapters=3), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        for i in (1, 2, 3):
            assert f"Opening paragraph of chapter {i}" in body
            assert f"Closing paragraph — the end of chapter {i}" in body

    def test_unicode_title_survives_round_trip(self, tmp_path):
        path = export_txt(_sample_story(unicode_title=True), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert "世界" in body and "日本語" in body

    def test_html_tags_stripped_from_chapter_body(self, tmp_path):
        path = export_txt(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        # No raw HTML survives into the TXT stream.
        assert "<p>" not in body
        assert "<em>" not in body


# ── HTML ──────────────────────────────────────────────────────────

class TestHtmlExportStructure:
    def test_is_well_formed_html5(self, tmp_path):
        path = export_html(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert body.startswith("<!DOCTYPE html>")
        assert "<html lang=\"en\">" in body
        assert body.rstrip().endswith("</html>")

    def test_has_title_tag_with_author_and_title(self, tmp_path):
        path = export_html(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert "<title>A Sample Story by An Author</title>" in body

    def test_meta_table_has_author_source_and_summary_rows(self, tmp_path):
        path = export_html(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert "<th>Author</th>" in body
        assert "<th>Source</th>" in body
        assert "<th>Summary</th>" in body

    def test_author_cell_is_a_link(self, tmp_path):
        path = export_html(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert (
            '<a href="https://www.fanfiction.net/u/12345/An-Author">'
            'An Author</a>'
        ) in body

    def test_toc_has_one_anchor_per_chapter(self, tmp_path):
        story = _sample_story(num_chapters=5)
        path = export_html(story, output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        for i in range(1, 6):
            assert f'href="#chapter-{i}"' in body
            assert f'id="chapter-{i}"' in body

    def test_chapter_body_is_preserved(self, tmp_path):
        path = export_html(_sample_story(), output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        # The em tag from the source HTML survives the exporter's
        # "prepare chapter HTML" pass.
        assert "<em>emphasis</em>" in body

    def test_title_is_html_escaped(self, tmp_path):
        s = _sample_story()
        s.title = "Fred <script>alert('xss')</script> Story"
        path = export_html(s, output_dir=str(tmp_path))
        body = path.read_text(encoding="utf-8")
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body

    def test_unicode_title_kept_verbatim(self, tmp_path):
        path = export_html(
            _sample_story(unicode_title=True), output_dir=str(tmp_path),
        )
        body = path.read_text(encoding="utf-8")
        assert "世界" in body and "日本語" in body


# ── EPUB structural validation ────────────────────────────────────

def _epub_contents(path: Path) -> dict[str, bytes]:
    """Return ``filename → bytes`` for every entry in the EPUB zip."""
    with zipfile.ZipFile(path, "r") as zf:
        return {name: zf.read(name) for name in zf.namelist()}


class TestEpubStructure:
    """Pin the bits of EPUB3 that matter for strict readers.

    Kobo and many e-ink readers reject an EPUB whose ZIP entries
    don't start with an uncompressed ``mimetype`` file, or whose
    ``META-INF/container.xml`` doesn't point at a valid OPF. None of
    this is covered by the exporter helper tests. If ebooklib ever
    regresses (it has historically), we catch it here before shipping."""

    @pytest.fixture
    def epub_path(self, tmp_path):
        return export_epub(_sample_story(num_chapters=3), output_dir=str(tmp_path))

    def test_file_created_and_non_empty(self, epub_path):
        assert epub_path.exists()
        assert epub_path.stat().st_size > 1000

    def test_is_a_valid_zip(self, epub_path):
        assert zipfile.is_zipfile(epub_path)

    def test_mimetype_is_first_entry_and_uncompressed(self, epub_path):
        """EPUB3 Packaging §2.3: the ZIP's first file must be named
        ``mimetype``, STORED (not DEFLATED), and contain exactly
        ``application/epub+zip`` with no BOM or line break."""
        with zipfile.ZipFile(epub_path, "r") as zf:
            first = zf.infolist()[0]
            assert first.filename == "mimetype"
            assert first.compress_type == zipfile.ZIP_STORED
            assert zf.read("mimetype") == b"application/epub+zip"

    def test_container_xml_points_at_an_opf(self, epub_path):
        body = _epub_contents(epub_path)
        assert "META-INF/container.xml" in body
        container = body["META-INF/container.xml"].decode("utf-8")
        assert "<rootfile" in container
        assert 'media-type="application/oebps-package+xml"' in container
        # The rootfile's path must itself exist in the archive.
        import re
        m = re.search(r'full-path="([^"]+)"', container)
        assert m is not None
        assert m.group(1) in body

    def test_opf_declares_all_chapters_as_items(self, epub_path):
        """One ``<item>`` per chapter plus the NCX/nav + titlepage +
        CSS. This catches the class of regression where a chapter is
        silently dropped from the manifest — the file is there in the
        ZIP but the reader can't navigate to it."""
        body = _epub_contents(epub_path)
        opf_name = next(k for k in body if k.endswith(".opf"))
        opf = body[opf_name].decode("utf-8")
        # Three story chapter bodies are registered in the manifest.
        import re
        hrefs = re.findall(r'href="([^"]+\.xhtml)"', opf)
        story_chapter_hrefs = [
            h for h in hrefs
            if "chap_" in h or "chapter" in h.lower()
        ]
        assert len(story_chapter_hrefs) >= 3

    def test_every_manifest_item_has_content_in_zip(self, epub_path):
        """If the OPF manifests a file that isn't actually in the zip,
        readers silently fail to open the work. We re-check here."""
        body = _epub_contents(epub_path)
        opf_name = next(k for k in body if k.endswith(".opf"))
        opf_dir = opf_name.rsplit("/", 1)[0] if "/" in opf_name else ""
        opf = body[opf_name].decode("utf-8")
        import re
        for href in re.findall(r'href="([^"]+)"', opf):
            if href.startswith("http"):
                continue
            full = f"{opf_dir}/{href}" if opf_dir else href
            assert full in body, f"manifested {href!r} missing from zip"

    def test_nav_document_lists_each_chapter(self, epub_path):
        """The EPUB3 navigation document (nav.xhtml) is what readers
        use to render the TOC. Each chapter's title should appear."""
        body = _epub_contents(epub_path)
        nav_candidates = [
            v for k, v in body.items()
            if k.endswith("nav.xhtml") or k.endswith("nav.html")
        ]
        assert nav_candidates, "no nav document in EPUB"
        nav = nav_candidates[0].decode("utf-8")
        assert "Chapter 1" in nav
        assert "Chapter 2" in nav
        assert "Chapter 3" in nav


class TestEpubAtomicWrite:
    """The exporter routes through ``atomic_path`` so a crash during
    ``ebooklib.write_epub`` leaves the existing file intact. Hard to
    simulate a real crash in-process; we verify the indirect observable:
    no stray ``.tmp`` file in the output dir after a successful export."""

    def test_no_tmp_residue_on_success(self, tmp_path):
        export_epub(_sample_story(), output_dir=str(tmp_path))
        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".") and p.name.endswith(".tmp")
        ]
        assert leftovers == []

    def test_existing_file_preserved_if_writer_raises(self, tmp_path, monkeypatch):
        """Monkey-patch ``ebooklib.epub.write_epub`` to raise after a
        partial write. The pre-existing target must survive."""
        target_name = "A Sample Story - An Author.epub"
        target = tmp_path / target_name
        target.write_bytes(b"EXISTING CONTENT, KEEP ME")

        from ebooklib import epub as _epub_mod

        def partial_then_raise(path, book, *args, **kw):
            Path(path).write_bytes(b"partial garbage")
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(_epub_mod, "write_epub", partial_then_raise)
        with pytest.raises(RuntimeError):
            export_epub(_sample_story(), output_dir=str(tmp_path))

        assert target.read_bytes() == b"EXISTING CONTENT, KEEP ME"
