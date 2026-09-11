"""Unit tests for GatewayChatCompletionClient, exercised against a fake
gateway stub. Guards usage accounting (actual = last call, total =
cumulative) and finish_reason mapping onto AutoGen's allowed literals.
"""

from __future__ import annotations

import json

import pytest
from autogen_core import FunctionCall
from autogen_core.models import (
    AssistantMessage,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    UserMessage,
)
from autogen_core.tools import ToolSchema

from aep.gateway.v1 import service_pb2
from orchestrator.model_client import GatewayChatCompletionClient


class _FakeStub:
    """Stands in for GatewayServiceStub; returns queued responses in order."""

    def __init__(self, *responses: service_pb2.ChatResponse) -> None:
        self._responses = list(responses)
        self.requests: list[service_pb2.ChatRequest] = []

    async def Chat(self, request: service_pb2.ChatRequest, timeout=None):  # noqa: ANN001, N802
        self.requests.append(request)
        return self._responses.pop(0)


def _response(content: str, prompt: int, completion: int, finish: str) -> service_pb2.ChatResponse:
    return service_pb2.ChatResponse(
        message=service_pb2.Message(role="assistant", content=content),
        token_usage=service_pb2.TokenUsage(prompt_tokens=prompt, completion_tokens=completion),
        finish_reason=finish,
    )


async def test_create_returns_content_and_maps_temperature_into_request() -> None:
    stub = _FakeStub(_response("hi", 10, 5, "stop"))
    client = GatewayChatCompletionClient(temperature=0.3, stub=stub)

    result = await client.create([UserMessage(content="q", source="user")])

    assert result.content == "hi"
    assert result.finish_reason == "stop"
    # proto stores temperature as float32, so compare with tolerance
    assert stub.requests[0].model_config.temperature == pytest.approx(0.3)
    assert stub.requests[0].messages[0].role == "user"


async def test_usage_actual_is_last_call_total_is_cumulative() -> None:
    stub = _FakeStub(_response("a", 10, 5, "stop"), _response("b", 3, 7, "stop"))
    client = GatewayChatCompletionClient(temperature=0.0, stub=stub)

    await client.create([UserMessage(content="one", source="user")])
    await client.create([UserMessage(content="two", source="user")])

    assert (client.actual_usage().prompt_tokens, client.actual_usage().completion_tokens) == (3, 7)
    assert (client.total_usage().prompt_tokens, client.total_usage().completion_tokens) == (13, 12)


async def test_unknown_finish_reason_maps_to_unknown() -> None:
    stub = _FakeStub(_response("x", 1, 1, "tool_calls"))
    client = GatewayChatCompletionClient(temperature=0.0, stub=stub)

    result = await client.create([UserMessage(content="q", source="user")])

    assert result.finish_reason == "unknown"


async def test_tools_are_forwarded_as_gateway_tools() -> None:
    stub = _FakeStub(_response("hi", 1, 1, "stop"))
    client = GatewayChatCompletionClient(temperature=0.0, stub=stub)

    schema: ToolSchema = {
        "name": "http_request",
        "description": "make an http call",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
    }
    await client.create([UserMessage(content="q", source="user")], tools=[schema])

    sent = stub.requests[0]
    assert [t.name for t in sent.tools] == ["http_request"]
    assert sent.tools[0].description == "make an http call"
    assert json.loads(sent.tools[0].parameters)["properties"] == {"url": {"type": "string"}}
    # tool_choice is only set when tools are present.
    assert sent.HasField("tool_choice")
    assert sent.tool_choice == "auto"


async def test_no_tool_choice_when_no_tools() -> None:
    stub = _FakeStub(_response("hi", 1, 1, "stop"))
    client = GatewayChatCompletionClient(temperature=0.0, stub=stub)

    await client.create([UserMessage(content="q", source="user")])

    assert not stub.requests[0].HasField("tool_choice")


async def test_tool_calls_in_response_become_function_calls() -> None:
    resp = service_pb2.ChatResponse(
        message=service_pb2.Message(role="assistant", content=""),
        token_usage=service_pb2.TokenUsage(prompt_tokens=1, completion_tokens=1),
        finish_reason="tool_calls",
        tool_calls=[
            service_pb2.ToolCall(id="c1", name="http_request", arguments='{"url":"x"}')
        ],
    )
    client = GatewayChatCompletionClient(temperature=0.0, stub=_FakeStub(resp))

    result = await client.create([UserMessage(content="q", source="user")])

    assert result.finish_reason == "function_calls"
    assert isinstance(result.content, list)
    (call,) = result.content
    assert isinstance(call, FunctionCall)
    assert (call.id, call.name, call.arguments) == ("c1", "http_request", '{"url":"x"}')


async def test_assistant_tool_calls_and_results_serialize_to_wire() -> None:
    stub = _FakeStub(_response("done", 1, 1, "stop"))
    client = GatewayChatCompletionClient(temperature=0.0, stub=stub)

    messages = [
        UserMessage(content="call it", source="user"),
        AssistantMessage(
            content=[FunctionCall(id="c1", name="http_request", arguments="{}")],
            source="assistant",
        ),
        FunctionExecutionResultMessage(
            content=[
                FunctionExecutionResult(
                    content="200 OK", call_id="c1", name="http_request", is_error=False
                )
            ]
        ),
    ]
    await client.create(messages)

    wire = stub.requests[0].messages
    assert [m.role for m in wire] == ["user", "assistant", "tool"]
    assert wire[1].tool_calls[0].id == "c1"
    assert wire[1].tool_calls[0].name == "http_request"
    assert wire[2].content == "200 OK"
    assert wire[2].tool_call_id == "c1"
