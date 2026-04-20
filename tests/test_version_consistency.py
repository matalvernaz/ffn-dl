"""Guard the two-place version invariant.

``ffn_dl/__init__.py`` exposes ``__version__`` to the running app
(self-updater reads it to decide whether a GitHub release is newer).
``pyproject.toml`` drives the CI build and tag. They MUST match, or
the self-updater sees its own installed build as older than the one
it just downloaded and loops forever. 1.23.10 shipped with the two
values out of sync and bricked the auto-update flow for everyone
who upgraded; this test prevents that regression.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_init_version_matches_pyproject():
    repo = Path(__file__).resolve().parent.parent
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    pkg_init = (repo / "ffn_dl" / "__init__.py").read_text(encoding="utf-8")

    pyproject_match = re.search(
        r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE,
    )
    assert pyproject_match, "pyproject.toml has no version line"
    pyproject_version = pyproject_match.group(1)

    init_match = re.search(
        r'^__version__\s*=\s*"([^"]+)"', pkg_init, re.MULTILINE,
    )
    assert init_match, "ffn_dl/__init__.py has no __version__ line"
    init_version = init_match.group(1)

    assert pyproject_version == init_version, (
        f"Version mismatch: pyproject.toml={pyproject_version!r} but "
        f"ffn_dl/__init__.py={init_version!r}. Bump both together — "
        "the self-updater compares GitHub's latest against "
        "__version__, and a mismatch causes an update loop."
    )
