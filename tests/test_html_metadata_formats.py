"""Tests for ``updater._fill_from_html`` across third-party HTML formats.

The library scanner needs to read fanfic metadata out of any HTML
download the user points it at, not just ffn-dl's own exports. These
tests cover the four dominant formats observed in real user libraries:

* ffn-dl's own exports (``<tr><th>Title</th><td>…</td></tr>``)
* FicLab (same shape, but lowercase labels)
* "Simple" paragraph dumps (``<p>Title: …</p>``)
* Bold-prefix paragraph dumps (``<b>Title:</b> …<br/>``)

Plus AO3's native HTML download, which uses ``<dt>Label:</dt><dd>…</dd>``.

Each test uses a small synthetic fixture so the suite stays offline and
doesn't require the user's real library on disk.
"""
from __future__ import annotations

from ffn_dl.updater import (
    _parse_kv_table,
    _parse_paragraph_labels,
    extract_metadata,
)


def _write(tmp_path, name, body):
    """Write ``body`` to ``tmp_path/name`` and return the path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _parse_kv_table
# ---------------------------------------------------------------------------


def test_kv_table_lowercases_labels():
    """``<th>Title</th>`` and ``<th>title</th>`` collapse to the same key."""
    html = """
    <table>
      <tr><th>Title</th><td>Canonical Case</td></tr>
      <tr><th>author</th><td>FicLab Case</td></tr>
    </table>
    """
    kv = _parse_kv_table(html)
    assert kv["title"] == "Canonical Case"
    assert kv["author"] == "FicLab Case"
    # No uppercase keys leaked through.
    assert "Title" not in kv
    assert "Author" not in kv


def test_kv_table_parses_dt_dd():
    """AO3's native HTML uses <dt>Label:</dt><dd>Value</dd>."""
    html = """
    <dl class="tags">
      <dt>Fandom:</dt>
      <dd><a>Naruto</a></dd>
      <dt>Rating:</dt>
      <dd>Explicit</dd>
    </dl>
    """
    kv = _parse_kv_table(html)
    assert kv["fandom"] == "Naruto"
    assert kv["rating"] == "Explicit"


def test_kv_table_unwraps_anchors_in_values():
    """Anchor text survives; the <a> tags don't."""
    html = '<tr><th>source</th><td><a href="https://example/works/1">https://example/works/1</a></td></tr>'
    kv = _parse_kv_table(html)
    assert kv["source"] == "https://example/works/1"


# ---------------------------------------------------------------------------
# _parse_paragraph_labels
# ---------------------------------------------------------------------------


def test_paragraph_labels_simple_p():
    """<p>Label: value</p> paragraph dumps."""
    html = """
    <p>Title: 10th Life</p>
    <p>Author: <a href="/u/1">Woona</a></p>
    <p>Category: Harry Potter + DxD Crossover</p>
    """
    labels = _parse_paragraph_labels(html)
    assert labels["title"] == "10th Life"
    assert labels["author"] == "Woona"
    assert labels["category"] == "Harry Potter + DxD Crossover"


def test_paragraph_labels_bold_br_dump():
    """<b>Label:</b> value<br/> format."""
    html = """
    <b>Story:</b> Iron<br>
    <b>Author:</b> Baked The Author<br/>
    <b>Category:</b> Berserk + Worm Crossover<br>
    <b>Status:</b> In Progress<br>
    """
    labels = _parse_paragraph_labels(html)
    assert labels["story"] == "Iron"
    assert labels["author"] == "Baked The Author"
    assert labels["category"] == "Berserk + Worm Crossover"
    assert labels["status"] == "In Progress"


def test_paragraph_labels_ignores_unknown_labels():
    """Random ``<p>Foo: bar</p>`` lines in chapter text aren't metadata.

    Without this restriction, any dialogue tag or author-note preamble
    that happens to start with ``Word:`` would get harvested as a
    metadata field.
    """
    html = "<p>Harry: So, what now?</p>"
    labels = _parse_paragraph_labels(html)
    assert "harry" not in labels


# ---------------------------------------------------------------------------
# End-to-end extract_metadata() per format
# ---------------------------------------------------------------------------


_FICLAB_HTML = """
<!DOCTYPE html>
<html>
<head><title>A Bewitching Dance</title></head>
<body>
<article>
  <p>This ebook was automatically created by <a href="https://www.ficlab.com/">FicLab</a>
  based on content retrieved from <a href="https://www.fanfiction.net/s/14261003/">www.fanfiction.net/s/14261003/</a>.</p>
</article>
<table>
  <tbody>
    <tr><th>title</th><td>A Bewitching Dance</td></tr>
    <tr><th>author</th><td>Haerrlekin</td></tr>
    <tr><th>source</th><td><a href="https://www.fanfiction.net/s/14261003/">https://www.fanfiction.net/s/14261003/</a></td></tr>
    <tr><th>chapters</th><td>23</td></tr>
    <tr><th>status</th><td>In-Progress</td></tr>
    <tr><th>rating</th><td>Fiction M</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_ficlab_format_extracts_full_metadata(tmp_path):
    path = _write(tmp_path, "bewitching.html", _FICLAB_HTML)
    md = extract_metadata(path)
    assert md.title == "A Bewitching Dance"
    assert md.author == "Haerrlekin"
    assert md.status == "In-Progress"
    assert md.rating == "Fiction M"
    assert md.chapter_count == 23
    assert md.source_url == "https://www.fanfiction.net/s/14261003/"


_SIMPLE_HTML = """
<html>
<head><title>10th Life</title></head>
<body>
<p>Title: 10th Life</p>
<p>Author: <a href="https://www.fanfiction.net/u/7123823/">Woona The Cat</a></p>
<p>Category: Harry Potter + High School DxD Crossover</p>
<p>Status: In-Progress</p>
<p>Rating: M</p>
<p>Chapters: 9</p>
<p>Words: 59,755</p>
<p><a href="https://www.fanfiction.net/s/11504036/1">https://www.fanfiction.net/s/11504036/1</a></p>
<h2>Chapter 1: Prologue</h2>
</body></html>
"""


def test_simple_paragraph_format_extracts_full_metadata(tmp_path):
    path = _write(tmp_path, "10th.html", _SIMPLE_HTML)
    md = extract_metadata(path)
    assert md.title == "10th Life"
    assert md.author == "Woona The Cat"
    assert md.fandoms == ["Harry Potter + High School DxD Crossover"]
    assert md.status == "In-Progress"
    assert md.rating == "M"
    assert md.chapter_count == 9
    # No explicit `Source: URL` row — URL recovered by the fallback
    # regex via sites.extract_story_url.
    assert md.source_url and "11504036" in md.source_url


_BOLD_BR_HTML = """
<html>
<head><meta name="author" content="Baked The Author"><title>Iron</title></head>
<body>
<b>Story:</b> Iron<br>
<b>Storylink:</b> <a href="https://www.fanfiction.net/s/13350076/1/">https://www.fanfiction.net/s/13350076/1/</a><br/>
<b>Category:</b> Berserk + Worm Crossover<br>
<b>Author:</b> Baked The Author<br/>
<b>Rating:</b> M<br/>
<b>Status:</b> In Progress<br/>
<b>Summary:</b> Trapped in the locker...<br>
<h2>Chapter 1</h2>
</body></html>
"""


def test_bold_br_format_extracts_title_author_fandom(tmp_path):
    path = _write(tmp_path, "iron.html", _BOLD_BR_HTML)
    md = extract_metadata(path)
    assert md.title == "Iron"
    assert md.author == "Baked The Author"
    assert md.fandoms == ["Berserk + Worm Crossover"]
    assert md.status == "In Progress"
    assert md.rating == "M"
    assert md.source_url and "13350076" in md.source_url


_AO3_NATIVE_HTML = """
<html>
<head><title>The Last Prayer - GraeFoxx - Naruto</title></head>
<body>
<div id="preface">
<p class="message">
<b>The Last Prayer</b><br/>
Posted originally on the <a href="http://archiveofourown.org/">Archive of Our Own</a>
at <a href="http://archiveofourown.org/works/18163346">http://archiveofourown.org/works/18163346</a>.
</p>
<div class="meta">
<dl class="tags">
<dt>Rating:</dt><dd><a>Explicit</a></dd>
<dt>Fandom:</dt><dd><a>Naruto</a></dd>
</dl>
</div>
</div>
</body></html>
"""


def test_ao3_native_format_extracts_fandom_and_rating(tmp_path):
    path = _write(tmp_path, "ao3.html", _AO3_NATIVE_HTML)
    md = extract_metadata(path)
    assert md.fandoms == ["Naruto"]
    assert md.rating == "Explicit"
    assert md.source_url and "18163346" in md.source_url


_FFNDL_NATIVE_HTML = """
<html>
<body>
<h1>Brightest In Shadow</h1>
<table class="meta-table">
<tr><th>Title</th><td>Brightest In Shadow</td></tr>
<tr><th>Author</th><td>SomeAuthor</td></tr>
<tr><th>Category</th><td>Worm</td></tr>
<tr><th>Rating</th><td>M</td></tr>
<tr><th>Status</th><td>In-Progress</td></tr>
<tr><th>Chapters</th><td>42</td></tr>
<tr><th>Source</th><td><a href="https://www.fanfiction.net/s/99999999/">https://www.fanfiction.net/s/99999999/</a></td></tr>
</table>
</body></html>
"""


def test_ffndl_native_html_still_works(tmp_path):
    """Regression: the lowercase-normalisation refactor must not break
    ffn-dl's own exports, which use capitalised labels."""
    path = _write(tmp_path, "native.html", _FFNDL_NATIVE_HTML)
    md = extract_metadata(path)
    assert md.title == "Brightest In Shadow"
    assert md.author == "SomeAuthor"
    assert md.fandoms == ["Worm"]
    assert md.rating == "M"
    assert md.status == "In-Progress"
    assert md.chapter_count == 42
    assert md.source_url == "https://www.fanfiction.net/s/99999999/"


def test_metadata_chapter_count_beats_dom_count(tmp_path):
    """When the kv-table gives us a chapter count, don't overwrite it
    with count_chapters() which only recognises ffn-dl's own markup
    and would return 0 for every third-party format."""
    html = """
    <html><body>
    <table>
      <tr><th>title</th><td>T</td></tr>
      <tr><th>author</th><td>A</td></tr>
      <tr><th>chapters</th><td>17</td></tr>
      <tr><th>source</th><td><a href="https://www.fanfiction.net/s/1/">url</a></td></tr>
    </table>
    <!-- no <div class="chapter"> markers anywhere — count_chapters returns 0 -->
    </body></html>
    """
    path = _write(tmp_path, "cc.html", html)
    md = extract_metadata(path)
    assert md.chapter_count == 17
