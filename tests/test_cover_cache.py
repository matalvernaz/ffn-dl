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
    # probable error page. Includes the real PNG magic prefix so the
    # post-2.4.14 magic-byte validation accepts the body.
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * size


def _jpeg_bytes(size=2048):
    """JPEG magic + filler. Used in tests that need an image whose
    bytes match the claimed ``image/jpeg`` content-type."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * size


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
                200, _jpeg_bytes(), {"content-type": "image/jpeg"},
            ),
        ):
            exporters._fetch_cover_image(url)
        # Second call should read from cache with the same CT.
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


class TestV2414CoverValidation:
    """Magic-byte/content-type validation added in v2.4.14 — protects
    against bot-protection HTML pages being silently embedded as EPUB
    covers."""

    def test_lying_content_type_is_rejected(self, cover_cache_dir):
        """Server returns HTML body but claims ``image/jpeg`` — must be
        rejected at fetch time and not cached."""
        url = "https://example.invalid/lying.jpg"
        html_body = b"<html><body>Just a moment...</body></html>" + b"\x00" * 600
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(200, html_body, {"content-type": "image/jpeg"}),
        ):
            result = exporters._fetch_cover_image(url)
        assert result is None
        # And nothing got cached.
        assert not list(cover_cache_dir.glob("covers/*"))

    def test_unknown_content_type_dropped(self, cover_cache_dir):
        """``text/html`` content-type must not pass the cover gate even
        if the body coincidentally has more than 500 bytes."""
        url = "https://example.invalid/html.jpg"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, b"<html>" + b"a" * 600, {"content-type": "text/html"},
            ),
        ):
            result = exporters._fetch_cover_image(url)
        assert result is None

    def test_content_type_with_charset_normalised(self, cover_cache_dir):
        """``image/png; charset=utf-8`` must normalise to ``image/png``
        — used to leak the parameter string into the EPUB cover
        filename and break ebooklib's manifest."""
        url = "https://example.invalid/charset.png"
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _png_bytes(), {"content-type": "image/png; charset=utf-8"},
            ),
        ):
            result = exporters._fetch_cover_image(url)
        assert result is not None
        _content, ct = result
        assert ct == "image/png"

    def test_old_poisoned_cache_entry_dropped_on_read(self, cover_cache_dir):
        """An entry cached by a prior build before fetch-time validation
        existed (HTML body in a file labelled ``image/jpeg``) must be
        evicted on read rather than poisoning re-exports until TTL."""
        url = "https://example.invalid/poison.jpg"
        # Manually plant a bad cache entry to simulate the prior bug.
        cache_path = exporters._cover_cache_path(url)
        assert cache_path is not None
        bad_html = b"<html>not an image</html>" + b"\x00" * 600
        from ffn_dl.atomic import atomic_write_bytes
        atomic_write_bytes(cache_path, b"image/jpeg\n" + bad_html)
        # Cache read should reject + evict; fetch path should be invoked.
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(
                200, _jpeg_bytes(), {"content-type": "image/jpeg"},
            ),
        ) as m:
            result = exporters._fetch_cover_image(url)
        assert result is not None
        assert m.call_count == 1  # the bad cache entry forced a refetch


class TestV2415WebPMagic:
    """Regression for the convergence-pass WebP fix: bare ``RIFF`` would
    accept WAV/AVI bodies mislabelled as image/webp."""

    def test_webp_with_full_magic_accepted(self, cover_cache_dir):
        url = "https://example.invalid/real.webp"
        body = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 600
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(200, body, {"content-type": "image/webp"}),
        ):
            result = exporters._fetch_cover_image(url)
        assert result is not None
        content, ct = result
        assert ct == "image/webp"

    def test_riff_wav_mislabelled_as_webp_rejected(self, cover_cache_dir):
        # RIFF...WAVE — a wav file mislabelled image/webp. Must NOT pass
        # the magic-byte check.
        url = "https://example.invalid/fake.webp"
        body = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 600
        with patch(
            "curl_cffi.requests.get",
            return_value=_FakeResp(200, body, {"content-type": "image/webp"}),
        ):
            result = exporters._fetch_cover_image(url)
        assert result is None
