"""Search URL building + filter resolution — pure functions, no network."""

import pytest

from ffn_dl.search import (
    _build_ao3_search_url,
    _build_rr_search_url,
    _build_search_url,
    _parse_ao3_results,
    _parse_results,
    _resolve_filter,
    AO3_RATING,
    FFN_GENRE,
    FFN_RATING,
    FFN_STATUS,
    collapse_ao3_series,
)


class TestFFNFilterResolution:
    def test_labels_resolve_to_ids(self):
        assert _resolve_filter("K", FFN_RATING, "rating") == 1
        assert _resolve_filter("complete", FFN_STATUS, "status") == 2
        assert _resolve_filter("romance", FFN_GENRE, "genre") == 2

    def test_labels_are_case_insensitive(self):
        assert _resolve_filter("k+", FFN_RATING, "rating") == 2
        assert _resolve_filter("COMPLETE", FFN_STATUS, "status") == 2

    def test_raw_numeric_id_is_accepted(self):
        assert _resolve_filter("3", FFN_GENRE, "genre") == 3

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            _resolve_filter("neverseen", FFN_GENRE, "genre")


class TestFFNSearchURL:
    def test_bare_query_url(self):
        url = _build_search_url("harry", {})
        assert url.startswith("https://www.fanfiction.net/search/?")
        assert "keywords=harry" in url
        assert "type=story" in url

    def test_filters_append_params(self):
        url = _build_search_url(
            "harry",
            {"rating": "K", "status": "complete", "genre": "romance"},
        )
        assert "censorid=1" in url
        assert "statusid=2" in url
        assert "genreid=2" in url


class TestAO3SearchURL:
    def test_bare_query_url(self):
        url = _build_ao3_search_url("harry", {})
        assert url.startswith("https://archiveofourown.org/works/search?")
        assert "work_search" in url

    def test_rating_filter_translates(self):
        url = _build_ao3_search_url("harry", {"rating": "Teen"})
        # Teen resolves to 11 in AO3_RATING
        assert "rating_ids" in url
        assert str(AO3_RATING["teen"]) in url

    def test_freetext_word_count_passes_through(self):
        url = _build_ao3_search_url(
            "harry", {"word_count": "1000-5000", "fandom": "Harry Potter"},
        )
        assert "word_count" in url
        # Spaces are encoded, + or %20 both valid
        assert "Harry" in url and "Potter" in url


class TestPagination:
    def test_ffn_page_one_has_no_ppage(self):
        url = _build_search_url("harry", {})
        assert "ppage=" not in url

    def test_ffn_higher_page_adds_ppage(self):
        url = _build_search_url("harry", {}, page=3)
        assert "ppage=3" in url

    def test_ffn_sort_translates(self):
        url = _build_search_url("harry", {"sort": "favorites"})
        assert "sortid=4" in url

    def test_ao3_page_one_has_no_page(self):
        url = _build_ao3_search_url("harry", {})
        assert "page=" not in url

    def test_ao3_higher_page_adds_page(self):
        url = _build_ao3_search_url("harry", {}, page=2)
        assert "page=2" in url

    def test_rr_higher_page_adds_page(self):
        url = _build_rr_search_url("magic", {}, page=4)
        assert "page=4" in url


class TestAO3ResultParsing:
    def test_series_membership_appears_in_results(self, ao3_search_html):
        results = _parse_ao3_results(ao3_search_html)
        with_series = [r for r in results if r.get("series")]
        assert with_series, "expected at least one result with series info"
        first = with_series[0]["series"][0]
        assert first["id"].isdigit()
        assert first["url"].startswith("https://archiveofourown.org/series/")
        assert first["name"]


class TestCollapseSeries:
    def test_lone_series_work_stays_as_work(self):
        # A work that's in a series but is the only part appearing in
        # the results should stay as a regular work row — promoting it
        # to a "series" label hides the work's own title behind the
        # series title with no other parts to show alongside it.
        results = [
            {
                "title": "Part One",
                "author": "A",
                "url": "u1",
                "summary": "",
                "words": "1000",
                "chapters": "1",
                "rating": "T",
                "fandom": "",
                "status": "Complete",
                "series": [
                    {"id": "99", "name": "Saga", "url": "s/99", "part": 1},
                ],
            },
        ]
        collapsed = collapse_ao3_series(results)
        assert len(collapsed) == 1
        assert collapsed[0].get("is_series") is not True
        assert collapsed[0]["title"] == "Part One"

    def test_multi_membership_work_stays_as_work(self):
        results = [
            {
                "title": "Part",
                "series": [
                    {"id": "1", "name": "A", "url": "s/1", "part": 1},
                    {"id": "2", "name": "B", "url": "s/2", "part": 3},
                ],
            },
        ]
        collapsed = collapse_ao3_series(results)
        assert collapsed == results

    def test_parts_of_same_series_merge_into_one_row(self):
        results = [
            {"title": "P1", "series": [{"id": "7", "name": "S", "url": "s/7"}]},
            {"title": "P2", "series": [{"id": "7", "name": "S", "url": "s/7"}]},
            {"title": "Standalone", "series": []},
        ]
        collapsed = collapse_ao3_series(results)
        assert len(collapsed) == 2
        series_row = next(r for r in collapsed if r.get("is_series"))
        assert len(series_row["series_parts"]) == 2


class TestCollapseLiteroticaSeries:
    def test_two_parts_same_slug_collapse(self):
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Sample Story Ch. 06",
                "author": "Author1",
                "url": "https://www.literotica.com/s/sample-story-ch-06",
                "rating": "4.7", "fandom": "Fetish", "summary": "",
            },
            {
                "title": "Standalone Story",
                "author": "someone",
                "url": "https://www.literotica.com/s/standalone-story",
                "rating": "4", "fandom": "Mature", "summary": "",
            },
            {
                "title": "Sample Story Ch. 07",
                "author": "Author1",
                "url": "https://www.literotica.com/s/sample-story-ch-07",
                "rating": "4.6", "fandom": "Fetish", "summary": "",
            },
        ]
        collapsed = collapse_literotica_series(results)
        # Two parts collapse, standalone is preserved separately
        assert len(collapsed) == 2
        series_row = next(r for r in collapsed if r.get("is_series"))
        assert series_row["title"] == "Sample Story"
        assert series_row["parts_only"] is True
        assert len(series_row["series_parts"]) == 2
        assert series_row["series_id"] == "lit:sample-story"

    def test_lone_chapter_stays_as_work(self):
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Lone Part Ch. 03",
                "author": "X",
                "url": "https://www.literotica.com/s/lone-part-ch-03",
            },
        ]
        collapsed = collapse_literotica_series(results)
        assert len(collapsed) == 1
        assert collapsed[0].get("is_series") is not True

    def test_different_authors_do_not_collapse(self):
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Shared Slug Ch. 01",
                "author": "A",
                "url": "https://www.literotica.com/s/shared-slug-ch-01",
            },
            {
                "title": "Shared Slug Ch. 02",
                "author": "B",
                "url": "https://www.literotica.com/s/shared-slug-ch-02",
            },
        ]
        collapsed = collapse_literotica_series(results)
        assert len(collapsed) == 2
        assert all(not r.get("is_series") for r in collapsed)

    def test_bare_title_adopted_as_part_one(self):
        # Literotica's convention: Part 1 is posted with no suffix, then
        # Pt. 02 / Ch. 02 / etc. show up later. The bare-titled work
        # needs to be grouped with its own subsequent parts.
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Miss Abby Pt. 02",
                "author": "Author1",
                "url": "https://www.literotica.com/s/miss-abby-pt-02",
            },
            {
                "title": "Miss Abby",
                "author": "Author1",
                "url": "https://www.literotica.com/s/miss-abby",
            },
        ]
        collapsed = collapse_literotica_series(results)
        assert len(collapsed) == 1
        row = collapsed[0]
        assert row.get("is_series") is True
        assert len(row["series_parts"]) == 2
        # The bare-titled work should be ordered first (part 1)
        assert row["series_parts"][0]["url"].endswith("/miss-abby")
        assert row["series_parts"][1]["url"].endswith("/miss-abby-pt-02")

    def test_dash_number_suffix_collapses(self):
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Housewife Comes Out - 5",
                "author": "Author1",
                "url": "https://www.literotica.com/s/housewife-comes-out-5",
            },
            {
                "title": "Housewife Comes Out - 6",
                "author": "Author1",
                "url": "https://www.literotica.com/s/housewife-comes-out-6",
            },
        ]
        collapsed = collapse_literotica_series(results)
        assert len(collapsed) == 1
        assert collapsed[0].get("is_series") is True
        assert len(collapsed[0]["series_parts"]) == 2

    def test_annual_year_slugs_not_treated_as_series(self):
        # /s/foo-2023 and /s/foo-2024 are common for annual one-shots.
        # Without a chapter marker in the title, they should NOT be
        # collapsed into a series.
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "New Year's Eve 2023",
                "author": "Author1",
                "url": "https://www.literotica.com/s/new-years-eve-2023",
            },
            {
                "title": "New Year's Eve 2024",
                "author": "Author1",
                "url": "https://www.literotica.com/s/new-years-eve-2024",
            },
        ]
        collapsed = collapse_literotica_series(results)
        assert len(collapsed) == 2
        assert all(not r.get("is_series") for r in collapsed)

    def test_bare_title_not_adopted_when_group_already_has_part_1(self):
        # Edge: standalone `/s/foo` coexists with a later unrelated serial
        # `/s/foo-ch-01, /s/foo-ch-02` by the same author. The standalone
        # should stay standalone — its slug stem collision with the serial
        # is accidental, and the serial already has its own explicit
        # chapter 1.
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Foo",
                "author": "Author1",
                "url": "https://www.literotica.com/s/foo",
            },
            {
                "title": "Foo Ch. 01",
                "author": "Author1",
                "url": "https://www.literotica.com/s/foo-ch-01",
            },
            {
                "title": "Foo Ch. 02",
                "author": "Author1",
                "url": "https://www.literotica.com/s/foo-ch-02",
            },
        ]
        collapsed = collapse_literotica_series(results)
        # One standalone row + one collapsed series row (2 parts)
        assert len(collapsed) == 2
        series_row = next(r for r in collapsed if r.get("is_series"))
        assert len(series_row["series_parts"]) == 2
        standalone = next(r for r in collapsed if not r.get("is_series"))
        assert standalone["title"] == "Foo"

    def test_compact_p_suffix_collapses(self):
        from ffn_dl.search import collapse_literotica_series
        results = [
            {
                "title": "Under the Heels of Eleonora Vane P3",
                "author": "Author1",
                "url": "https://www.literotica.com/s/under-the-heels-of-eleonora-vane-p3",
            },
            {
                "title": "Under the Heels of Eleonora Vane P4",
                "author": "Author1",
                "url": "https://www.literotica.com/s/under-the-heels-of-eleonora-vane-p4",
            },
        ]
        collapsed = collapse_literotica_series(results)
        assert len(collapsed) == 1
        assert collapsed[0].get("is_series") is True
