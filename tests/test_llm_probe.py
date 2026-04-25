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
