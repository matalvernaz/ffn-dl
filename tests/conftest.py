"""Pytest fixtures: load saved HTML samples once per session."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def ao3_work_full_html():
    return _load("ao3_work_full.html")


@pytest.fixture(scope="session")
def ao3_work_bare_html():
    return _load("ao3_work_bare.html")


@pytest.fixture(scope="session")
def ao3_series_html():
    return _load("ao3_series.html")


@pytest.fixture(scope="session")
def ao3_search_html():
    return _load("ao3_search.html")


@pytest.fixture(scope="session")
def ffn_story_html():
    return _load("ffn_story.html")


@pytest.fixture(scope="session")
def ffn_story_not_found_html():
    return _load("ffn_story_not_found.html")


@pytest.fixture(scope="session")
def ffn_search_html():
    return _load("ffn_search.html")


@pytest.fixture(scope="session")
def ficwad_story_html():
    return _load("ficwad_story.html")
