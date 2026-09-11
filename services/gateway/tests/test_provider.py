"""Unit tests for OpenAIProvider: tool-schema handling and how provider
failures map to typed errors. No live network access -- the underlying
AsyncOpenAI client's `chat.completions.create` is monkeypatched.

No async test runner (pytest-asyncio) is set up for this service, so each
async entry point is driven directly via asyncio.run() from a plain sync
test function.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from openai import APIConnectionError, BadRequestError

from gateway.config import ModelSettings
from gateway.errors import ProviderError, ValidationError
from gateway.models import Message, ToolSpec
from gateway.provider import OpenAIProvider

_MODEL = ModelSettings(
    provider="openai",
    name="gpt-4o-mini",
    max_tokens=512,
    temperature=0.7,
    timeout_seconds=30,
)


def _provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="sk-test", model=_MODEL)


def test_malformed_tool_schema_raises_validation_error():
    provider = _provider()
    bad_tool = ToolSpec(name="lookup", description="d", parameters="{not json")

    with pytest.raises(ValidationError, match="lookup"):
        asyncio.run(
            provider.complete(
                [Message(role="user", content="hi")],
                max_tokens=None,
                temperature=None,
                tools=[bad_tool],
            )
        )


def test_provider_bad_request_maps_to_validation_error(monkeypatch):
    provider = _provider()

    async def raise_bad_request(**kwargs):
        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx2.Response(400, request=request, json={"error": {"message": "bad"}})
        raise BadRequestError("bad request", response=response, body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", raise_bad_request)

    with pytest.raises(ValidationError):
        asyncio.run(
            provider.complete([Message(role="user", content="hi")], max_tokens=None, temperature=None)
        )


def test_provider_connection_error_maps_to_provider_error(monkeypatch):
    provider = _provider()

    async def raise_connection_error(**kwargs):
        request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
        raise APIConnectionError(request=request)

    monkeypatch.setattr(provider._client.chat.completions, "create", raise_connection_error)

    with pytest.raises(ProviderError):
        asyncio.run(
            provider.complete([Message(role="user", content="hi")], max_tokens=None, temperature=None)
        )
