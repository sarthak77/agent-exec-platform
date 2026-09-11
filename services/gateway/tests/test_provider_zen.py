"""Unit tests for the OpenCode Zen provider (ZenProvider), covering the
tool-calling round trip on the Responses API.

No live network: the httpx2 client's `post` is monkeypatched. Async entry
points are driven via asyncio.run(), matching test_provider.py (this service
has no pytest-asyncio runner).
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from gateway.config import ModelSettings
from gateway.errors import ProviderError, ValidationError
from gateway.models import Message, ToolCall, ToolSpec
from gateway.provider_zen import ZenProvider

_MODEL = ModelSettings(
    provider="opencode",
    name="muse-spark-1.3-contributor-free",
    max_tokens=512,
    temperature=0.7,
    timeout_seconds=30,
    base_url="https://opencode.ai/zen/v1",
)


def _provider() -> ZenProvider:
    return ZenProvider(api_key="public", model=_MODEL)


def _response(status: int, body: dict) -> httpx2.Response:
    request = httpx2.Request("POST", "https://opencode.ai/zen/v1/responses")
    return httpx2.Response(status, request=request, json=body)


def _complete(provider, messages=None, **kwargs):
    return asyncio.run(
        provider.complete(
            messages if messages is not None else [Message(role="user", content="hi")],
            max_tokens=kwargs.pop("max_tokens", None),
            temperature=kwargs.pop("temperature", None),
            **kwargs,
        )
    )


def test_plain_request_shape_and_output_text(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def fake_post(url, json):
        captured["url"] = url
        captured["json"] = json
        return _response(
            200,
            {
                "output": [
                    {"type": "reasoning", "encrypted_content": "..."},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello there"}],
                    },
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            },
        )

    monkeypatch.setattr(provider._client, "post", fake_post)
    completion = _complete(provider)

    # Responses API `input` is a typed array; with no tools, no tool kwargs go
    # out, and max_output_tokens is omitted when the caller set no max_tokens.
    assert captured["url"] == "/responses"
    assert captured["json"]["model"] == "muse-spark-1.3-contributor-free"
    assert captured["json"]["input"] == [{"role": "user", "content": "hi"}]
    assert captured["json"]["stream"] is False
    # Reasoning effort is capped to keep free-tier calls fast.
    assert captured["json"]["reasoning"] == {"effort": "low"}
    assert "tools" not in captured["json"]
    assert "tool_choice" not in captured["json"]
    assert "max_output_tokens" not in captured["json"]
    # reasoning items are ignored; message output_text is the content.
    assert completion.content == "hello there"
    assert completion.finish_reason == "stop"
    assert completion.tool_calls == ()
    assert completion.usage.prompt_tokens == 1
    assert completion.usage.completion_tokens == 2
    assert completion.usage.total_tokens == 3


def test_tools_forwarded_flat_and_function_call_parsed(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def fake_post(url, json):
        captured["json"] = json
        return _response(
            200,
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "calling"}],
                    },
                    {
                        "type": "function_call",
                        "id": "fc_x",
                        "call_id": "call_1",
                        "name": "get_weather",
                        "arguments": '{"city":"Paris"}',
                    },
                ],
                "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
            },
        )

    monkeypatch.setattr(provider._client, "post", fake_post)
    completion = _complete(
        provider,
        tools=[
            ToolSpec(
                name="get_weather",
                description="d",
                parameters='{"type":"object","properties":{"city":{"type":"string"}}}',
            )
        ],
        tool_choice="auto",
    )

    # Responses API uses a flat function tool shape (not nested under "function").
    assert captured["json"]["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "d",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
        }
    ]
    assert captured["json"]["tool_choice"] == "auto"
    # The function_call item becomes a ToolCall keyed on call_id (not the fc id).
    assert completion.finish_reason == "tool_calls"
    assert len(completion.tool_calls) == 1
    tc = completion.tool_calls[0]
    assert (tc.id, tc.name, tc.arguments) == ("call_1", "get_weather", '{"city":"Paris"}')
    assert completion.content == "calling"


def test_parallel_function_calls_all_parsed(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(
            200,
            {
                "output": [
                    {"type": "function_call", "call_id": "a", "name": "t", "arguments": "{}"},
                    {"type": "function_call", "call_id": "b", "name": "t", "arguments": "{}"},
                ]
            },
        )

    monkeypatch.setattr(provider._client, "post", fake_post)
    completion = _complete(provider, tools=[ToolSpec(name="t", description="d", parameters="{}")])
    assert [tc.id for tc in completion.tool_calls] == ["a", "b"]


def test_tool_result_relayed_as_function_call_output(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def fake_post(url, json):
        captured["json"] = json
        return _response(
            200,
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "20C"}],
                    }
                ]
            },
        )

    monkeypatch.setattr(provider._client, "post", fake_post)
    messages = [
        Message(role="user", content="weather?"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="call_1", name="get_weather", arguments='{"city":"Paris"}'),),
        ),
        Message(role="tool", content='{"temp":"20C"}', tool_call_id="call_1"),
    ]
    completion = _complete(provider, messages=messages)

    # An assistant tool-call turn with empty text emits only the function_call
    # item; the tool result becomes a function_call_output keyed on call_id.
    assert captured["json"]["input"] == [
        {"role": "user", "content": "weather?"},
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": '{"city":"Paris"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"temp":"20C"}'},
    ]
    assert completion.content == "20C"


def test_assistant_thought_precedes_function_call(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def fake_post(url, json):
        captured["json"] = json
        return _response(200, {"output_text": "ok"})

    monkeypatch.setattr(provider._client, "post", fake_post)
    messages = [
        Message(
            role="assistant",
            content="let me check",
            tool_calls=(ToolCall(id="c1", name="t", arguments="{}"),),
        ),
    ]
    _complete(provider, messages=messages)
    assert captured["json"]["input"] == [
        {"role": "assistant", "content": "let me check"},
        {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
    ]


def test_temperature_and_max_tokens_overrides_forwarded(monkeypatch):
    provider = _provider()
    captured: dict = {}

    async def fake_post(url, json):
        captured["json"] = json
        return _response(200, {"output_text": "hi"})

    monkeypatch.setattr(provider._client, "post", fake_post)
    asyncio.run(
        provider.complete(
            [Message(role="user", content="hi")], max_tokens=256, temperature=0.2
        )
    )
    assert captured["json"]["temperature"] == 0.2
    assert captured["json"]["max_output_tokens"] == 256


def test_output_text_fallback(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(200, {"output_text": "flat answer"})

    monkeypatch.setattr(provider._client, "post", fake_post)
    assert _complete(provider).content == "flat answer"


def test_sends_public_bearer_and_opencode_headers():
    headers = _provider()._client.headers
    assert headers["Authorization"] == "Bearer public"
    assert headers["x-opencode-client"] == "cli"
    assert headers["User-Agent"] == "opencode/local"


def test_malformed_tool_schema_raises_validation_error():
    provider = _provider()
    # Raised while building the request, before any network call.
    with pytest.raises(ValidationError, match="lookup"):
        _complete(
            provider,
            tools=[ToolSpec(name="lookup", description="d", parameters="{not json")],
        )


def test_4xx_maps_to_validation_error(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(400, {"error": "bad"})

    monkeypatch.setattr(provider._client, "post", fake_post)
    with pytest.raises(ValidationError):
        _complete(provider)


def test_5xx_maps_to_provider_error(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(503, {"error": "down"})

    monkeypatch.setattr(provider._client, "post", fake_post)
    with pytest.raises(ProviderError):
        _complete(provider)


def test_error_field_maps_to_provider_error(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(200, {"error": {"message": "boom"}, "output": []})

    monkeypatch.setattr(provider._client, "post", fake_post)
    with pytest.raises(ProviderError):
        _complete(provider)


def test_connection_error_maps_to_provider_error(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        raise httpx2.ConnectError("boom")

    monkeypatch.setattr(provider._client, "post", fake_post)
    with pytest.raises(ProviderError):
        _complete(provider)


def test_empty_output_maps_to_provider_error(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(200, {"output": []})

    monkeypatch.setattr(provider._client, "post", fake_post)
    with pytest.raises(ProviderError):
        _complete(provider)


def test_tool_calls_only_without_text_is_not_an_error(monkeypatch):
    provider = _provider()

    async def fake_post(url, json):
        return _response(
            200,
            {"output": [{"type": "function_call", "call_id": "c", "name": "t", "arguments": "{}"}]},
        )

    monkeypatch.setattr(provider._client, "post", fake_post)
    completion = _complete(provider, tools=[ToolSpec(name="t", description="d", parameters="{}")])
    assert completion.content == ""
    assert len(completion.tool_calls) == 1
