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


_FLAG_HTML = """
<!DOCTYPE html>
<html><head>
<title>FLAG :: The Sealed Kunai by Kenchi618</title>
</head><body>
<h1>The Sealed Kunai by Kenchi618</h1>
<p>This book was automatically created by <a href="http://www.flagfic.com/">FLAG</a>
based on content retrieved from <span id="crSource">
<a href="http://www.fanfiction.net/s/6051938/">http://www.fanfiction.net/s/6051938/</a>
</span>.</p>
<p>The content in this book is copyrighted by
<span id="crAuthor">Kenchi618</span> or their authorised agents(s).</p>
<h2>Chapter 1</h2>
</body></html>
"""


def test_flag_format_extracts_title_and_author(tmp_path):
    """flagfic.com output — `<span id="crAuthor">` + `<h1>Title by Author</h1>`."""
    path = _write(tmp_path, "kunai.html", _FLAG_HTML)
    md = extract_metadata(path)
    assert md.title == "The Sealed Kunai"
    assert md.author == "Kenchi618"
    assert md.source_url and "6051938" in md.source_url


_SPAN_CLASS_HTML = """
<!doctype html>
<html><body>
<h1><span class="title">Sealkeeper: He Who Binds</span></h1>
<h2>by <span class="author">Syynistyre</span></h2>
<p>Original source:
<a href="https://www.fanfiction.net/s/11651066/1/">https://www.fanfiction.net/s/11651066/1/</a></p>
</body></html>
"""


def test_span_class_format_extracts_title_and_author(tmp_path):
    path = _write(tmp_path, "sealkeeper.html", _SPAN_CLASS_HTML)
    md = extract_metadata(path)
    assert md.title == "Sealkeeper: He Who Binds"
    assert md.author == "Syynistyre"
    assert md.source_url and "11651066" in md.source_url


_AO3_NATIVE_FULL_HTML = """
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
<dt>Rating:</dt><dd>Explicit</dd>
<dt>Fandom:</dt><dd>Naruto</dd>
</dl>
</div>
</div>
</body></html>
"""


def test_ao3_native_fallback_recovers_title_and_author(tmp_path):
    """AO3 native HTML has no kv-table for title/author — pulled from
    the ``<title>`` tag's ``Title - Author - Fandom`` convention."""
    path = _write(tmp_path, "ao3native.html", _AO3_NATIVE_FULL_HTML)
    md = extract_metadata(path)
    assert md.title == "The Last Prayer"
    assert md.author == "GraeFoxx"
    assert md.fandoms == ["Naruto"]  # from <dt>Fandom:</dt>
    assert md.rating == "Explicit"


def test_fallback_never_overwrites_structured_values(tmp_path):
    """If a kv-table already gave us a title, a `<title>` tag with a
    different value mustn't clobber it."""
    html = """
    <html>
    <head><title>Some Wrong Thing - Someone - Fandom</title></head>
    <body>
    <table>
      <tr><th>title</th><td>Correct Title From Table</td></tr>
      <tr><th>author</th><td>Correct Author</td></tr>
      <tr><th>source</th><td><a href="https://archiveofourown.org/works/1">x</a></td></tr>
    </table>
    </body></html>
    """
    path = _write(tmp_path, "mixed.html", html)
    md = extract_metadata(path)
    assert md.title == "Correct Title From Table"
    assert md.author == "Correct Author"


def test_ficlab_crossover_fandom_extracted_from_tags(tmp_path):
    """FicLab has no dedicated fandom field, but FFN's crossover
    convention gives us a reliable ``"X + Y Crossover"`` entry inside
    the tags row. For a fic in ``misc/`` (where the folder-fandom
    fallback correctly refuses to help), extracting this tag is the
    only way to get a meaningful fandom."""
    html = """
    <html><body>
    <p>FicLab v1.0 — source https://www.fanfiction.net/s/123/</p>
    <table><tbody>
      <tr><th>title</th><td>Crossover Story</td></tr>
      <tr><th>author</th><td>Someone</td></tr>
      <tr><th>source</th><td><a href="https://www.fanfiction.net/s/123/">url</a></td></tr>
      <tr><th>tags</th><td>Adventure, Fanfiction, Harry P., Harry Potter + Dragon Age Crossover, In-Progress, Morrigan</td></tr>
    </tbody></table>
    </body></html>
    """
    path = _write(tmp_path, "crossover.html", html)
    md = extract_metadata(path)
    assert md.fandoms == ["Harry Potter + Dragon Age Crossover"]


def test_non_crossover_ficlab_leaves_fandom_for_folder_fallback(tmp_path):
    """A FicLab file with no ``Crossover`` tag keeps fandoms empty —
    the folder-fandom backfill in identify() is the right place to
    populate it, not the tags-blob heuristic."""
    html = """
    <html><body>
    <table><tbody>
      <tr><th>title</th><td>Single Fandom Story</td></tr>
      <tr><th>author</th><td>Someone</td></tr>
      <tr><th>source</th><td><a href="https://www.fanfiction.net/s/123/">url</a></td></tr>
      <tr><th>tags</th><td>Adventure, Fanfiction, Harry P., Naruto, In-Progress</td></tr>
    </tbody></table>
    </body></html>
    """
    path = _write(tmp_path, "single.html", html)
    md = extract_metadata(path)
    # Empty — no reliable way to pick "Naruto" out of a tags blob that
    # mixes genre/character/status tokens. The folder-fandom fallback
    # in library.identifier takes over when the file is scanned.
    assert md.fandoms == []


def test_bold_br_chapter_count_from_content_phrase(tmp_path):
    """bold-br files put their chapter count in the Content field as
    "Chapter X to Y of N chapters"; extracting ``N`` lets library
    refresh (--update-library) know the local count instead of
    skipping the story as "chapter count unknown"."""
    html = """
    <html><body>
    <b>Story:</b> Living with Danger<br>
    <b>Storylink:</b> <a href="https://www.fanfiction.net/s/2109424/1/">...</a><br/>
    <b>Author:</b> whydoyouneedtoknow<br/>
    <b>Category:</b> Harry Potter<br>
    <b>Rating:</b> T<br/>
    <b>Status:</b> Complete<br/>
    <b>Content:</b> Chapter 1 to 50 of 50 chapters<br/>
    </body></html>
    """
    path = _write(tmp_path, "lwd.html", html)
    md = extract_metadata(path)
    assert md.chapter_count == 50


def test_ao3_native_chapter_count_from_stats_slash(tmp_path):
    """AO3 native HTML has ``Chapters: 43/?`` in the Stats block.
    Take the first number — the count of published chapters."""
    html = """
    <html><body>
    <p>Posted on <a href="https://archiveofourown.org/works/1">AO3</a></p>
    <dl>
      <dt>Stats:</dt>
      <dd>
      Published: 2019-03-19
      Updated: 2024-01-17
      Words: 588,850
      Chapters: 43/?
      </dd>
    </dl>
    </body></html>
    """
    path = _write(tmp_path, "ao3.html", html)
    md = extract_metadata(path)
    assert md.chapter_count == 43


def test_flag_chapter_count_from_toc_anchors(tmp_path):
    """FLAG/flagfic.com files that don't spell out "N chapters" still
    expose the count via their ``<a href="#chapter_N">`` TOC links."""
    html = """
    <html><body>
    <p>Created by <a href="http://www.flagfic.com/">FLAG</a>
    from <a href="https://www.fanfiction.net/s/1/">source</a></p>
    <div id="toc">
      <ol>
        <li><a href="#chapter_1">First</a></li>
        <li><a href="#chapter_2">Second</a></li>
        <li><a href="#chapter_3">Third</a></li>
      </ol>
    </div>
    <h2>Chapter 1</h2>
    <h2>Chapter 2</h2>
    <h2>Chapter 3</h2>
    </body></html>
    """
    path = _write(tmp_path, "flag.html", html)
    md = extract_metadata(path)
    assert md.chapter_count == 3


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
