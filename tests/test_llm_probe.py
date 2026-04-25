"""Tests for ``attribution.probe_llm_endpoint`` and the
``ollama_install`` helpers behind the LLM settings dialog's
Test/Install/Download buttons.

The probe is a small HTTP GET against each provider's inventory
surface — Ollama ``/api/tags``, OpenAI/compatible ``/models``,
Anthropic ``/models`` — so the user can find out before kicking off a
download whether their endpoint is actually reachable. The installer
shells out to ``winget install Ollama.Ollama`` on Windows. Both are
unit-testable without network or subprocess access by stubbing
``urllib.request.urlopen`` and ``subprocess.Popen`` respectively.
"""

from __future__ import annotations

import io
import json

import pytest

from ffn_dl import attribution, ollama_install


# ── probe_llm_endpoint ────────────────────────────────────────────


class _FakeResp:
    """Minimal stand-in for ``urllib.request.urlopen``'s return value
    in the success path. The probe only reads ``status`` and
    ``read()``."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


def _stub_urlopen(monkeypatch, response):
    """``response`` is either a ``_FakeResp`` or an Exception to raise."""
    captured: list = []

    def fake(req, timeout=None):
        captured.append({
            "url": req.full_url,
            "headers": dict(req.headers),
            "method": req.get_method(),
        })
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("urllib.request.urlopen", fake)
    return captured


class TestProbeOllama:
    def test_lists_installed_models_on_success(self, monkeypatch):
        body = json.dumps(
            {"models": [
                {"name": "llama3.1:8b"},
                {"name": "qwen2.5:14b"},
            ]}
        ).encode()
        captured = _stub_urlopen(monkeypatch, _FakeResp(body))

        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        assert result.ok
        assert result.models == ["llama3.1:8b", "qwen2.5:14b"]
        assert "2 model(s) available" in result.detail
        assert "llama3.1:8b" in result.detail
        # Hits the inventory endpoint, not /api/chat.
        assert captured[0]["url"].endswith("/api/tags")
        assert captured[0]["method"] == "GET"

    def test_no_installed_models_offers_pull_hint(self, monkeypatch):
        _stub_urlopen(monkeypatch, _FakeResp(json.dumps({"models": []}).encode()))
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        assert result.ok
        assert "no models are installed" in result.detail.lower()
        assert "ollama pull" in result.detail

    def test_connection_refused_returns_friendly_hint(self, monkeypatch):
        _stub_urlopen(
            monkeypatch,
            ConnectionRefusedError(
                "[WinError 10061] No connection could be made"
            ),
        )
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        assert not result.ok
        # The user sees both the raw error (so they can google it) and
        # an actionable next step pointing at the Install button.
        assert "unreachable" in result.detail.lower()
        assert "install ollama" in result.detail.lower()

    def test_blank_endpoint_falls_through_to_default(self, monkeypatch):
        captured = _stub_urlopen(
            monkeypatch, _FakeResp(json.dumps({"models": []}).encode()),
        )
        attribution.probe_llm_endpoint(provider="ollama", endpoint=None)
        # Default endpoint applies — the helper hits the documented
        # 11434 port without the user having to type it.
        assert captured[0]["url"] == "http://localhost:11434/api/tags"


class TestProbeOpenAI:
    def test_requires_api_key(self, monkeypatch):
        # No urlopen stub — we never get that far.
        result = attribution.probe_llm_endpoint(
            provider="openai", endpoint="https://api.openai.com/v1",
            api_key="",
        )
        assert not result.ok
        assert "api key" in result.detail.lower()

    def test_success_lists_models_by_id(self, monkeypatch):
        body = json.dumps(
            {"data": [
                {"id": "gpt-4o-mini"},
                {"id": "gpt-4o"},
            ]}
        ).encode()
        captured = _stub_urlopen(monkeypatch, _FakeResp(body))

        result = attribution.probe_llm_endpoint(
            provider="openai", endpoint="https://api.openai.com/v1",
            api_key="sk-test",
        )
        assert result.ok
        assert result.models == ["gpt-4o-mini", "gpt-4o"]
        # Bearer auth, not x-api-key.
        assert captured[0]["headers"]["Authorization"] == "Bearer sk-test"

    def test_401_is_auth_failure_not_unreachable(self, monkeypatch):
        import urllib.error
        _stub_urlopen(
            monkeypatch,
            urllib.error.HTTPError(
                url="https://api.openai.com/v1/models",
                code=401, msg="Unauthorized", hdrs=None,
                fp=io.BytesIO(b""),
            ),
        )
        result = attribution.probe_llm_endpoint(
            provider="openai", endpoint="https://api.openai.com/v1",
            api_key="sk-wrong",
        )
        assert not result.ok
        assert result.status == 401
        assert "api key" in result.detail.lower()


class TestProbeAnthropic:
    def test_uses_x_api_key_header(self, monkeypatch):
        body = json.dumps(
            {"data": [{"id": "claude-haiku-4-5"}]}
        ).encode()
        captured = _stub_urlopen(monkeypatch, _FakeResp(body))

        result = attribution.probe_llm_endpoint(
            provider="anthropic", endpoint="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
        )
        assert result.ok
        # Anthropic uses x-api-key + anthropic-version, not Bearer.
        # urllib normalises header names to title-case.
        headers = {k.lower(): v for k, v in captured[0]["headers"].items()}
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == "2023-06-01"


class TestProbeEdgeCases:
    """Cases the dialog will hit in practice with weird providers or
    user-typed endpoints. None of these should crash; all should
    produce a useful detail string."""

    def test_openai_compatible_without_api_key_still_attempts_probe(
        self, monkeypatch,
    ):
        # vLLM / Ollama-compatible servers serve OpenAI-shaped responses
        # without requiring auth. Don't reject up front — try the call.
        body = json.dumps({"data": [{"id": "local-model"}]}).encode()
        captured = _stub_urlopen(monkeypatch, _FakeResp(body))

        result = attribution.probe_llm_endpoint(
            provider="openai-compatible",
            endpoint="http://localhost:8000/v1",
            api_key="",
        )
        assert result.ok
        # No Authorization header attached when there's no key.
        assert "Authorization" not in captured[0]["headers"]

    def test_endpoint_trailing_slash_normalised(self, monkeypatch):
        # ``_llm_normalize_endpoint`` rstrips the slash; the probe
        # mustn't double it (``//api/tags``) when the user pastes a
        # URL with a trailing slash from a config file.
        captured = _stub_urlopen(
            monkeypatch, _FakeResp(json.dumps({"models": []}).encode()),
        )
        attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434/",
        )
        assert captured[0]["url"] == "http://localhost:11434/api/tags"

    def test_malformed_json_response_is_treated_as_reachable(
        self, monkeypatch,
    ):
        # Some reverse proxies serve plain text "OK" on health-check
        # endpoints. The probe should treat this as "endpoint is up,
        # not a real LLM" — useful info for the user, not a crash.
        _stub_urlopen(monkeypatch, _FakeResp(b"not json at all"))
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        assert result.ok
        assert "wasn't json" in result.detail.lower() or "json" in result.detail.lower()

    def test_response_without_models_key_doesnt_crash(self, monkeypatch):
        # An unusual provider that returns a non-dict JSON body. Don't
        # AttributeError trying to ``.get("models")``.
        _stub_urlopen(monkeypatch, _FakeResp(json.dumps([1, 2, 3]).encode()))
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        # Still ok=True (server replied), just no model list to show.
        assert result.ok
        assert result.models == [] or result.models is None

    def test_500_error_reports_status_not_unreachable(self, monkeypatch):
        import urllib.error
        _stub_urlopen(
            monkeypatch,
            urllib.error.HTTPError(
                url="http://localhost:11434/api/tags",
                code=500, msg="Internal Server Error", hdrs=None,
                fp=io.BytesIO(b"db down"),
            ),
        )
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        # Server replied — endpoint is reachable but unhealthy. Don't
        # send the user to "is the daemon running?", that's misleading.
        assert not result.ok
        assert result.status == 500
        assert "500" in result.detail
        assert "is the ollama daemon running" not in result.detail.lower()

    def test_dns_failure_reports_unreachable(self, monkeypatch):
        import urllib.error
        _stub_urlopen(
            monkeypatch,
            urllib.error.URLError(
                "[Errno -2] Name or service not known"
            ),
        )
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://nope.invalid:11434",
        )
        assert not result.ok
        assert "unreachable" in result.detail.lower()

    def test_dict_models_with_neither_name_nor_id_skipped(self, monkeypatch):
        # Some local stacks return ``{"object": "model"}`` without a
        # name field. Don't TypeError; just skip those entries.
        body = json.dumps(
            {"models": [
                {"object": "model"},  # no name, no id
                {"name": "actual-model"},
            ]}
        ).encode()
        _stub_urlopen(monkeypatch, _FakeResp(body))
        result = attribution.probe_llm_endpoint(
            provider="ollama", endpoint="http://localhost:11434",
        )
        assert result.ok
        assert result.models == ["actual-model"]

    def test_anthropic_without_api_key_short_circuits(self):
        # No urlopen stub — we must not reach the network when the key
        # is missing for a provider that requires it.
        result = attribution.probe_llm_endpoint(
            provider="anthropic",
            endpoint="https://api.anthropic.com/v1",
            api_key=None,
        )
        assert not result.ok
        assert "api key" in result.detail.lower()


# ── ollama_install ────────────────────────────────────────────────


class TestWingetCommand:
    def test_command_includes_silent_and_accept_flags(self):
        cmd = ollama_install.winget_install_command()
        assert cmd[0] == "winget"
        assert "install" in cmd
        assert "--id" in cmd
        assert ollama_install.WINGET_PACKAGE_ID in cmd
        # Silent + accept flags are mandatory — without them the
        # subprocess would block forever waiting for stdin or for the
        # user to click through the Ollama installer GUI.
        assert "--silent" in cmd
        assert "--accept-source-agreements" in cmd
        assert "--accept-package-agreements" in cmd
        assert "--disable-interactivity" in cmd


class TestWingetSupported:
    def test_returns_false_on_non_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        assert ollama_install.winget_supported() is False

    def test_returns_false_when_winget_missing_on_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("shutil.which", lambda _name: None)
        assert ollama_install.winget_supported() is False

    def test_returns_true_when_winget_on_path(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "shutil.which", lambda name: r"C:\winget.exe" if name == "winget" else None,
        )
        assert ollama_install.winget_supported() is True


class TestWingetUnavailableReason:
    def test_message_for_non_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "darwin")
        msg = ollama_install.winget_unavailable_reason()
        assert "Windows-only" in msg or "windows-only" in msg.lower()
        assert "ollama.com" in msg

    def test_message_for_windows_without_winget(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr("shutil.which", lambda _name: None)
        msg = ollama_install.winget_unavailable_reason()
        assert "winget" in msg.lower()
        # The dialog should point at the Download Ollama button as an
        # escape hatch on machines where winget can't be added.
        assert "download ollama" in msg.lower()

    def test_empty_when_supported(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "shutil.which", lambda name: r"C:\winget.exe" if name == "winget" else None,
        )
        assert ollama_install.winget_unavailable_reason() == ""


class TestWingetExitClassification:
    def test_zero_is_success(self):
        assert ollama_install._winget_exit_is_success(0) is True

    def test_already_installed_signed_code_is_success(self):
        # winget reports "no upgrade applicable" as -1978335189 on the
        # Windows builds that hand back a signed int. Users who already
        # had Ollama and clicked Install shouldn't see a red error.
        assert ollama_install._winget_exit_is_success(-1978335189) is True

    def test_already_installed_unsigned_code_is_success(self):
        assert ollama_install._winget_exit_is_success(0x8A15002B) is True

    def test_other_nonzero_is_failure(self):
        assert ollama_install._winget_exit_is_success(1) is False

    def test_none_is_failure(self):
        # ``Popen.returncode`` can be ``None`` if the process was
        # killed weirdly. Don't paper over it as success.
        assert ollama_install._winget_exit_is_success(None) is False


class _FakePopen:
    """Stand-in for ``subprocess.Popen`` so we can drive
    :func:`_consume_winget_output` without a real subprocess."""

    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


class TestConsumeWingetOutput:
    def test_streams_each_line_to_callback_and_reports_success(self):
        captured: list[str] = []
        proc = _FakePopen(
            lines=["Found Ollama [Ollama.Ollama]\n", "  Successfully installed\n"],
            returncode=0,
        )
        ok = ollama_install._consume_winget_output(proc, captured.append)
        assert ok is True
        assert captured == [
            "Found Ollama [Ollama.Ollama]",
            "  Successfully installed",
        ]

    def test_failure_exit_code_returns_false(self):
        proc = _FakePopen(lines=["error: something\n"], returncode=2)
        ok = ollama_install._consume_winget_output(proc, lambda _: None)
        assert ok is False

    def test_already_installed_exit_code_still_succeeds(self):
        proc = _FakePopen(
            lines=["No applicable upgrade found\n"],
            returncode=-1978335189,
        )
        assert ollama_install._consume_winget_output(proc, lambda _: None) is True


class TestInstallOllamaUnsupportedPlatform:
    def test_logs_download_url_and_returns_false(self, monkeypatch):
        # No winget on this machine — the helper must NOT try to
        # invoke a missing binary; it must hand back the download URL.
        monkeypatch.setattr(ollama_install, "winget_supported", lambda: False)
        captured: list[str] = []
        ok = ollama_install.install_ollama_via_winget(
            log_callback=captured.append,
        )
        assert ok is False
        assert any(
            ollama_install.OLLAMA_DOWNLOAD_URL in line for line in captured
        )


# ── pull_ollama_model ─────────────────────────────────────────────


class _FakePullStream:
    """Drives :func:`_consume_ollama_pull_stream` with a fixed list of
    JSON-line bytes. Mirrors the urlopen response interface enough to
    let the helper iterate and ``with``-enter."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(self._lines)


def _evt(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


class TestConsumePullStream:
    def test_success_event_returns_true(self):
        captured: list[str] = []
        stream = _FakePullStream([
            _evt({"status": "pulling manifest"}),
            _evt({"status": "success"}),
        ])
        ok = ollama_install._consume_ollama_pull_stream(
            stream, captured.append,
        )
        assert ok is True
        # The phase change is logged exactly once.
        assert any("pulling manifest" in line for line in captured)
        assert any("success" in line for line in captured)

    def test_no_success_event_returns_false(self):
        # Stream that ended mid-download (network drop, server killed).
        # No "success" status means the pull is unfinished.
        stream = _FakePullStream([
            _evt({"status": "downloading", "total": 100, "completed": 50}),
        ])
        assert ollama_install._consume_ollama_pull_stream(
            stream, lambda _: None,
        ) is False

    def test_error_event_returns_false_and_logs_detail(self):
        captured: list[str] = []
        stream = _FakePullStream([
            _evt({"error": "model not found"}),
        ])
        ok = ollama_install._consume_ollama_pull_stream(
            stream, captured.append,
        )
        assert ok is False
        assert any("model not found" in line for line in captured)

    def test_progress_deduplicates_to_5pct_buckets(self):
        # The pull API emits 200+ updates a second on a fast download.
        # Logging every one would drown the dialog, so the helper
        # collapses to 5%-step buckets per phase. Drive enough updates
        # that a naive logger would emit dozens, then assert the
        # collapsed log has only a handful.
        events = [_evt({"status": "pulling manifest"})]
        for completed in range(0, 100, 1):  # 100 raw progress updates
            events.append(
                _evt({
                    "status": "downloading",
                    "total": 100, "completed": completed,
                })
            )
        events.append(_evt({"status": "success"}))

        captured: list[str] = []
        stream = _FakePullStream(events)
        ok = ollama_install._consume_ollama_pull_stream(
            stream, captured.append,
        )
        assert ok is True
        downloading_lines = [l for l in captured if "downloading:" in l]
        # 0%, 5%, 10%, ..., 95% → 20 buckets. Allow ±1 for the bucket
        # rounding edge cases.
        assert 19 <= len(downloading_lines) <= 21, (
            f"Got {len(downloading_lines)} lines, expected ~20"
        )

    def test_phase_transitions_emit_immediately(self):
        # Multiple layers download in sequence — each "downloading"
        # event with a new digest is conceptually a new phase, but the
        # current model just collapses by status string. That's fine
        # because the progress percentage resets and the bucket
        # transition will fire. This test pins that subsequent layers
        # still show some progress lines rather than being eaten by
        # the dedupe on a stale status==last_status.
        events = [
            _evt({"status": "pulling manifest"}),
            _evt({"status": "downloading", "total": 100, "completed": 0}),
            _evt({"status": "downloading", "total": 100, "completed": 50}),
            _evt({"status": "verifying"}),
            _evt({"status": "writing"}),
            _evt({"status": "success"}),
        ]
        captured: list[str] = []
        stream = _FakePullStream(events)
        ollama_install._consume_ollama_pull_stream(stream, captured.append)
        joined = "\n".join(captured)
        for phase in ("pulling manifest", "downloading", "verifying",
                      "writing", "success"):
            assert phase in joined, (
                f"phase {phase!r} missing from log: {joined}"
            )

    def test_malformed_json_line_logged_not_crashed(self):
        captured: list[str] = []
        stream = _FakePullStream([
            b"not json\n",
            _evt({"status": "success"}),
        ])
        ok = ollama_install._consume_ollama_pull_stream(
            stream, captured.append,
        )
        assert ok is True
        # The garbage line is logged verbatim rather than crashing.
        assert any("not json" in line for line in captured)


class TestPullOllamaModel:
    def test_unreachable_endpoint_logs_hint_and_returns_false(
        self, monkeypatch,
    ):
        def boom(req, timeout=None):
            raise ConnectionRefusedError("nope")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        captured: list[str] = []
        ok = ollama_install.pull_ollama_model(
            endpoint="http://localhost:11434",
            model="llama3.1:8b",
            progress_callback=captured.append,
        )
        assert ok is False
        assert any("unreachable" in line.lower() for line in captured)
        assert any("start ollama" in line.lower() for line in captured)

    def test_404_from_server_returns_false_with_status(
        self, monkeypatch,
    ):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                url="http://x/api/pull", code=404,
                msg="Not Found", hdrs=None,
                fp=io.BytesIO(b"model not found in registry"),
            )

        monkeypatch.setattr("urllib.request.urlopen", boom)
        captured: list[str] = []
        ok = ollama_install.pull_ollama_model(
            endpoint="http://localhost:11434",
            model="not-a-real-model",
            progress_callback=captured.append,
        )
        assert ok is False
        assert any("404" in line for line in captured)

    def test_success_streams_to_callback(self, monkeypatch):
        events = [
            _evt({"status": "pulling manifest"}),
            _evt({
                "status": "downloading",
                "total": 1000, "completed": 500,
            }),
            _evt({"status": "success"}),
        ]
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=None: _FakePullStream(events),
        )
        captured: list[str] = []
        ok = ollama_install.pull_ollama_model(
            endpoint="http://localhost:11434",
            model="llama3.1:8b",
            progress_callback=captured.append,
        )
        assert ok is True
        # Sanity: the post body the dialog will send to a real Ollama
        # has stream=true. Without that the API returns a single
        # non-streamed JSON and the progress UX collapses.
        # (Probed indirectly by checking progress lines arrived.)
        assert any("downloading" in line for line in captured)


class TestComputeModelChoices:
    """Pure data-shaping for the dialog's Model combo dropdown."""

    def test_preserves_curated_order_via_sort(self):
        result = attribution.compute_model_choices(
            curated=["llama3.1:8b", "phi3.5:3.8b"],
            extra=[],
            current="",
        )
        # Case-insensitive alphabetical → llama before phi.
        assert result == ["llama3.1:8b", "phi3.5:3.8b"]

    def test_extra_models_merged_uniquely(self):
        result = attribution.compute_model_choices(
            curated=["llama3.1:8b"],
            extra=["llama3.1:8b", "qwen2.5:14b"],  # duplicate filtered
            current="",
        )
        assert result == ["llama3.1:8b", "qwen2.5:14b"]

    def test_current_typed_value_appears_even_if_uncurated(self):
        # The user typed a custom Ollama tag — it must survive the
        # merge so they don't have to retype after a probe re-renders
        # the dropdown.
        result = attribution.compute_model_choices(
            curated=["llama3.1:8b"],
            extra=[],
            current="my-custom:latest",
        )
        assert "my-custom:latest" in result
        assert "llama3.1:8b" in result

    def test_blank_current_is_no_op(self):
        # Empty string mustn't appear as a phantom dropdown entry.
        result = attribution.compute_model_choices(
            curated=["llama3.1:8b"],
            extra=[],
            current="",
        )
        assert "" not in result

    def test_whitespace_current_treated_as_blank(self):
        result = attribution.compute_model_choices(
            curated=["llama3.1:8b"],
            extra=[],
            current="   ",
        )
        assert "   " not in result
        assert result == ["llama3.1:8b"]

    def test_case_insensitive_sort(self):
        # Two models that differ only in capitalisation should land
        # adjacent rather than at opposite ends of the dropdown.
        result = attribution.compute_model_choices(
            curated=["Zephyr:7b", "alpaca:7b"],
            extra=[],
            current="",
        )
        # Case-insensitive: "alpaca" before "Zephyr".
        assert result == ["alpaca:7b", "Zephyr:7b"]

    def test_filters_empty_strings_in_inputs(self):
        # Defensive: probe might hand back a list with a "" element
        # if a provider's response contains a malformed entry. Skip
        # those rather than letting an empty dropdown row through.
        result = attribution.compute_model_choices(
            curated=["llama3.1:8b", ""],
            extra=["", "qwen2.5:7b"],
            current="",
        )
        assert "" not in result
        assert result == ["llama3.1:8b", "qwen2.5:7b"]


class TestHumanBytes:
    @pytest.mark.parametrize("size,expected", [
        (0, "0B"),
        (512, "512B"),
        (1024, "1.0KB"),
        (1024 * 1024, "1.0MB"),
        (int(1.5 * 1024 * 1024), "1.5MB"),
        (2 * 1024 ** 3, "2.0GB"),
    ])
    def test_formats_common_sizes(self, size, expected):
        assert ollama_install._human_bytes(size) == expected
