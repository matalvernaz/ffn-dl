"""Correlation-id logging: tagging, scoping, and no-context no-op."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from ffn_dl.logging_utils import (
    correlation_context,
    current_correlation_id,
    install_correlation_filter,
    new_correlation_id,
)


@pytest.fixture(autouse=True)
def ensure_filter_installed():
    install_correlation_filter()
    yield


@pytest.fixture
def capture_ffn_dl_logs(caplog):
    """Capture ffn_dl.* log records so tests can inspect the ``msg``
    attribute after the LogRecordFactory has munged it."""
    caplog.set_level(logging.DEBUG, logger="ffn_dl")
    return caplog


class TestIDGeneration:
    def test_ids_are_unique(self):
        ids = {new_correlation_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_ids_are_short(self):
        assert len(new_correlation_id()) == 8

    def test_ids_are_hex(self):
        cid = new_correlation_id()
        int(cid, 16)  # raises if not valid hex

    def test_no_context_means_none(self):
        assert current_correlation_id() is None


class TestTagging:
    def test_log_inside_context_gets_prefix(self, capture_ffn_dl_logs):
        logger = logging.getLogger("ffn_dl.test.tagging")
        with correlation_context("abcd1234"):
            logger.info("hello world")
        record = capture_ffn_dl_logs.records[-1]
        assert record.msg.startswith("[dl-abcd1234]")
        assert "hello world" in record.msg

    def test_log_outside_context_has_no_prefix(self, capture_ffn_dl_logs):
        logger = logging.getLogger("ffn_dl.test.outside")
        logger.info("no context here")
        record = capture_ffn_dl_logs.records[-1]
        assert not record.msg.startswith("[dl-")
        assert record.msg == "no context here"

    def test_auto_generated_id_when_omitted(self, capture_ffn_dl_logs):
        logger = logging.getLogger("ffn_dl.test.auto")
        with correlation_context() as cid:
            assert len(cid) == 8
            logger.info("auto-tagged")
        record = capture_ffn_dl_logs.records[-1]
        assert record.msg.startswith(f"[dl-{cid}]")

    def test_child_logger_inherits_prefix(self, capture_ffn_dl_logs):
        """The factory keys on record.name.startswith("ffn_dl") so any
        child logger under the package picks up the tag."""
        with correlation_context("11111111"):
            logging.getLogger("ffn_dl.scraper").info("from scraper")
            logging.getLogger("ffn_dl.erotica.literotica").info("from erotica")
        records = [r for r in capture_ffn_dl_logs.records
                   if r.name.startswith("ffn_dl")]
        for record in records:
            if "from" in record.msg:
                assert "[dl-11111111]" in record.msg

    def test_third_party_logs_not_tagged(self, capture_ffn_dl_logs):
        """A ``[dl-…]`` tag leaking into urllib3 / requests logs would
        be noise. The factory guards on the logger name."""
        capture_ffn_dl_logs.set_level(logging.DEBUG)  # capture all
        with correlation_context("dead1234"):
            logging.getLogger("urllib3.test").info("third-party line")
        records = [r for r in capture_ffn_dl_logs.records
                   if r.name == "urllib3.test"]
        assert records
        assert not any("[dl-" in r.msg for r in records)


class TestScope:
    def test_nested_contexts_restore_on_exit(self):
        with correlation_context("outer_id"):
            assert current_correlation_id() == "outer_id"
            with correlation_context("inner_id"):
                assert current_correlation_id() == "inner_id"
            assert current_correlation_id() == "outer_id"
        assert current_correlation_id() is None

    def test_exception_still_restores(self):
        with pytest.raises(RuntimeError):
            with correlation_context("doomed"):
                assert current_correlation_id() == "doomed"
                raise RuntimeError("oops")
        assert current_correlation_id() is None


class TestThreadIsolation:
    def test_each_thread_has_its_own_id(self):
        """ContextVar guarantees per-thread isolation — two stories
        downloading concurrently in a thread pool don't cross-tag
        each other's log lines."""
        seen: list[tuple[str, str | None]] = []

        def worker(name, cid):
            with correlation_context(cid):
                # Yield to other thread so both are inside their
                # contexts simultaneously.
                time.sleep(0.02)
                seen.append((name, current_correlation_id()))

        t1 = threading.Thread(target=worker, args=("t1", "aaaaaaaa"))
        t2 = threading.Thread(target=worker, args=("t2", "bbbbbbbb"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert dict(seen) == {"t1": "aaaaaaaa", "t2": "bbbbbbbb"}


class TestIdempotent:
    def test_install_is_idempotent(self, capture_ffn_dl_logs):
        install_correlation_filter()
        install_correlation_filter()
        install_correlation_filter()
        # Still works, no double-prefix.
        with correlation_context("cafebabe"):
            logging.getLogger("ffn_dl.idem").info("x")
        record = capture_ffn_dl_logs.records[-1]
        assert record.msg == "[dl-cafebabe] x"
        assert record.msg.count("[dl-") == 1


class TestScraperDownloadWrapping:
    def test_download_wrapped_with_context(self, capture_ffn_dl_logs):
        """A scraper's ``download`` method runs inside a fresh
        correlation context without the caller having to set one up.
        This is the ``__init_subclass__`` hook on BaseScraper paying
        off — every existing callsite stays unchanged."""
        from ffn_dl.scraper import BaseScraper

        captured_cid: list[str | None] = []

        class _Toy(BaseScraper):
            site_name = "toy"

            def download(self, url_or_id, **kw):
                captured_cid.append(current_correlation_id())
                logging.getLogger("ffn_dl.toy").info("inside download")
                return None

        scraper = _Toy(use_cache=False)
        scraper.download("x")
        assert captured_cid[0] is not None
        assert len(captured_cid[0]) == 8

        # Log line carries the tag.
        matches = [
            r for r in capture_ffn_dl_logs.records
            if "inside download" in str(r.msg)
        ]
        assert matches
        assert matches[0].msg.startswith("[dl-")

    def test_two_downloads_get_distinct_ids(self):
        from ffn_dl.scraper import BaseScraper

        seen = []

        class _Toy(BaseScraper):
            site_name = "toy2"

            def download(self, url_or_id, **kw):
                seen.append(current_correlation_id())

        scraper = _Toy(use_cache=False)
        scraper.download("a")
        scraper.download("b")
        assert seen[0] != seen[1]

    def test_caller_cid_still_respected(self):
        """If the caller already opened a correlation context (e.g.
        a CLI ``--update-all`` pass wrapping batch downloads), the
        wrapper's fresh id takes precedence inside ``download`` —
        consistent with "each individual download is its own unit"."""
        from ffn_dl.scraper import BaseScraper

        outer = []
        inner = []

        class _Toy(BaseScraper):
            site_name = "toy3"

            def download(self, url_or_id, **kw):
                inner.append(current_correlation_id())

        scraper = _Toy(use_cache=False)
        with correlation_context("outer123"):
            outer.append(current_correlation_id())
            scraper.download("a")
            outer.append(current_correlation_id())

        assert outer == ["outer123", "outer123"]
        assert inner[0] != "outer123"
        assert inner[0] is not None
