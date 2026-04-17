"""Search URL building + filter resolution — pure functions, no network."""

import pytest

from ffn_dl.search import (
    _build_ao3_search_url,
    _build_search_url,
    _resolve_filter,
    AO3_RATING,
    FFN_GENRE,
    FFN_RATING,
    FFN_STATUS,
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
