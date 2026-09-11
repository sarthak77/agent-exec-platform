"""Unit tests for settings loading/validation.

Note these import gateway.config without OPENAI_API_KEY or config.toml being
present — a regression guard for the lazy-loading refactor (importing the
module must not trigger a load).
"""

from __future__ import annotations

import textwrap

import pytest

from gateway.config import _load, get_settings

_VALID = """\
[grpc]
host = "0.0.0.0"
port = 50054

[model]
provider = "openai"
name = "gpt-4o-mini"
max_tokens = 512
temperature = 0.7
timeout_seconds = 30

[guardrails]
max_input_chars = 8000
blocklist = ["jailbreak"]
"""


def _write(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body))
    return path


def test_load_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = _load(_write(tmp_path, _VALID))
    assert settings.model.name == "gpt-4o-mini"
    assert settings.grpc.port == 50054
    assert settings.api_key == "sk-test"
    # OpenAI provider uses the SDK's default endpoint (no base_url override).
    assert settings.model.base_url is None


def test_missing_api_key_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        _load(_write(tmp_path, _VALID))


def test_groq_provider_uses_groq_key_and_default_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    body = _VALID.replace('provider = "openai"', 'provider = "groq"')
    settings = _load(_write(tmp_path, body))
    assert settings.api_key == "gsk-test"
    assert settings.model.base_url == "https://api.groq.com/openai/v1"


def test_missing_groq_key_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    body = _VALID.replace('provider = "openai"', 'provider = "groq"')
    with pytest.raises(RuntimeError):
        _load(_write(tmp_path, body))


def test_explicit_base_url_overrides_provider_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = _VALID.replace(
        'timeout_seconds = 30',
        'timeout_seconds = 30\nbase_url = "https://proxy.example/v1"',
    )
    settings = _load(_write(tmp_path, body))
    assert settings.model.base_url == "https://proxy.example/v1"


def test_unsupported_provider_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = _VALID.replace('provider = "openai"', 'provider = "anthropic"')
    with pytest.raises(RuntimeError):
        _load(_write(tmp_path, body))


def test_get_settings_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GATEWAY_CONFIG_FILE", str(_write(tmp_path, _VALID)))
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
