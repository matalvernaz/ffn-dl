"""Cover-image cache — skips the network on repeat exports."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ffn_dl import exporters


class _FakeResp:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


@pytest.fixture
def cover_cache_dir(tmp_path, monkeypatch):
    """Point the cover cache at a tmpdir so tests don't scribble over
    the real ``~/.cache/ffn-dl``."""
    from ffn_dl import portable
    monkeypatch.setattr(portable, "cache_dir", lambda: tmp_path)
    return tmp_path


def _png_bytes(size=2048):
    # Enough bytes to pass the >500 threshold and not be rejected as a
    # probable error page.
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * size


class TestCacheHit:
    def test_first_fetch_goes_to_network(self, cover_cache_dir):
        url = "https://example.invalid/cover1.jpg"
        calls = []

        def fake_get(u, **kw):
            calls.append(u)
            return _FakeResp(200, _png_bytes(), {"content-type": "image/png"})

        with patch("curl_cffi.requests.get", side_effect=fake_get):
            result = exporters._fetch_cover_image(url)
        assert result is not None
        content, ct = result
        assert ct == "image/png"
        assert len(calls) == 1

    def test_second_fetch_hits_cache(self, cover_cache_dir):
        url = "https://example.invalid/cover2.jpg"
        calls = []

        def fake_get(u, **kw):
            calls.append(u)
            return _FakeResp(200, _png_bytes(), {"content-type": "image/png"})

        with patch("curl_cffi.requests.get", side_effect=fake_get):
            exporters._fetch_cover_image(url)
            exporters._fetch_cover_image(url)
            exporters._fetch_cover_image(url)
        assert len(calls) == 1  # three calls, one network hit

    def test_cache_returns_correct_content_type(self, cover_cache_dir):
        url = "https://example.invalid/cover3.jpg"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _png_bytes(), {"content-type": "image/jpeg"},
            ),
        ):
            exporters._fetch_cover_image(url)
        # Second call should read from cache with same CT.
        result = exporters._fetch_cover_image(url)
        assert result is not None
        content, ct = result
        assert ct == "image/jpeg"
        assert len(content) > 500

    def test_cache_keys_are_url_distinct(self, cover_cache_dir):
        """Two stories with different cover URLs must NOT share a cache
        entry — hash collisions would silently serve the wrong cover."""
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _png_bytes(), {"content-type": "image/png"},
            ),
        ) as m:
            exporters._fetch_cover_image("https://example.invalid/a.jpg")
            exporters._fetch_cover_image("https://example.invalid/b.jpg")
        assert m.call_count == 2

    def test_use_cache_false_skips_cache(self, cover_cache_dir):
        url = "https://example.invalid/nocache.jpg"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _png_bytes(), {"content-type": "image/png"},
            ),
        ) as m:
            exporters._fetch_cover_image(url, use_cache=False)
            exporters._fetch_cover_image(url, use_cache=False)
        assert m.call_count == 2  # no cache involvement


class TestTTL:
    def test_expired_entry_refetches(self, cover_cache_dir):
        url = "https://example.invalid/expiring.jpg"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _png_bytes(), {"content-type": "image/png"},
            ),
        ) as m:
            exporters._fetch_cover_image(url)
            # Age the cache entry past the TTL.
            cache_path = exporters._cover_cache_path(url)
            old = time.time() - exporters._COVER_CACHE_TTL_S - 10
            import os
            os.utime(cache_path, (old, old))

            exporters._fetch_cover_image(url)
        assert m.call_count == 2  # re-fetched because expired


class TestFailureHandling:
    def test_network_failure_returns_none(self, cover_cache_dir):
        url = "https://example.invalid/fails.jpg"
        with patch(
            "curl_cffi.requests.get",
            side_effect=ConnectionError("boom"),
        ):
            assert exporters._fetch_cover_image(url) is None

    def test_small_content_rejected(self, cover_cache_dir):
        """A <500 byte response is probably an error page or a
        1×1 tracking pixel, not a real cover. Treat as failure."""
        url = "https://example.invalid/tiny.jpg"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(200, b"x" * 100, {"content-type": "image/png"}),
        ):
            assert exporters._fetch_cover_image(url) is None

    def test_non_200_returns_none(self, cover_cache_dir):
        url = "https://example.invalid/404.jpg"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(404, b"not found", {}),
        ):
            assert exporters._fetch_cover_image(url) is None

    def test_corrupt_cache_entry_falls_through_to_network(self, cover_cache_dir):
        """A truncated / half-written cache entry shouldn't make the
        cover permanently unavailable — the next call should refetch."""
        url = "https://example.invalid/corrupted.jpg"
        cache_path = exporters._cover_cache_path(url)
        # Write a garbage entry with no newline terminator.
        cache_path.write_bytes(b"no-newline-here-this-is-corrupt")

        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _png_bytes(), {"content-type": "image/png"},
            ),
        ) as m:
            result = exporters._fetch_cover_image(url)
        assert result is not None
        assert m.call_count == 1
