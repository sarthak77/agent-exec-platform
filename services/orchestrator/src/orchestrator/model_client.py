"""Custom AutoGen ChatCompletionClient that routes every inference call
through the gateway service's gRPC GatewayService.Chat (the platform's
single funnel to the LLM; see gateway/servicer.py).

The gateway supports OpenAI-style function calling (aep.gateway.v1.ChatRequest
carries `tools`/`tool_choice`; ChatResponse carries `tool_calls`), so this
client advertises `function_calling=True` and bridges AutoGen's tool protocol
to the gateway wire format:

- tool schemas (from the per-agent MCP workbench, see mcp_workbench.py) are
  forwarded as gateway `Tool`s;
- an AssistantMessage carrying FunctionCalls and a FunctionExecutionResultMessage
  carrying results are translated to assistant `tool_calls` and role="tool"
  messages respectively;
- gateway `tool_calls` in the response become a CreateResult whose content is a
  list of FunctionCalls (finish_reason "function_calls"), which AutoGen then
  executes via the workbench and feeds back to the next turn.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from autogen_core import CancellationToken, FunctionCall
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    ModelCapabilities,
    ModelFamily,
    ModelInfo,
    RequestUsage,
)
from autogen_core.tools import Tool, ToolSchema

from aep.gateway.v1 import service_pb2, service_pb2_grpc
from orchestrator.config import settings

_ASSUMED_MAX_TOKENS = 8192

# CreateResult.finish_reason is a constrained literal; map the gateway's
# free-form reason onto it and fall back to "unknown" for anything else.
_FINISH_REASONS = frozenset({"stop", "length", "function_calls", "content_filter", "unknown"})

_VALID_TOOL_CHOICES = frozenset({"auto", "none", "required"})


def _to_gateway_messages(message: LLMMessage) -> list[service_pb2.Message]:
    """Translate one AutoGen LLMMessage into the gateway wire messages it maps
    to. Most map 1:1; a FunctionExecutionResultMessage fans out to one
    role="tool" message per result."""
    if message.type == "SystemMessage":
        return [service_pb2.Message(role="system", content=str(message.content))]

    if message.type == "UserMessage":
        return [service_pb2.Message(role="user", content=_content_str(message.content))]

    if message.type == "AssistantMessage":
        # content is either free text or a list of FunctionCalls the model made.
        if isinstance(message.content, list):
            return [
                service_pb2.Message(
                    role="assistant",
                    content=message.thought or "",
                    tool_calls=[
                        service_pb2.ToolCall(
                            id=fc.id, name=fc.name, arguments=fc.arguments
                        )
                        for fc in message.content
                    ],
                )
            ]
        return [service_pb2.Message(role="assistant", content=str(message.content))]

    if message.type == "FunctionExecutionResultMessage":
        return [
            service_pb2.Message(
                role="tool", content=result.content, tool_call_id=result.call_id
            )
            for result in message.content
        ]

    # Unknown message type: fall back to a user turn so the request is still
    # well-formed rather than silently dropped.
    return [service_pb2.Message(role="user", content=_content_str(message.content))]


def _content_str(content: Any) -> str:
    """UserMessage content can be a list (multimodal); this basic path is
    text-only, so stringify anything that isn't already a string."""
    return content if isinstance(content, str) else str(content)


def _to_gateway_tool(tool: Tool | ToolSchema) -> service_pb2.Tool:
    schema: ToolSchema = tool.schema if isinstance(tool, Tool) else tool
    parameters = schema.get("parameters")
    return service_pb2.Tool(
        name=schema["name"],
        description=schema.get("description", ""),
        # gateway keeps `parameters` opaque (a JSON-Schema string); AutoGen's
        # ParametersSchema is a plain dict, so JSON-encode it here.
        parameters=json.dumps(dict(parameters)) if parameters else "",
    )


def _to_gateway_tool_choice(tool_choice: Tool | str) -> str:
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in _VALID_TOOL_CHOICES else "auto"
    # A specific Tool was requested; the gateway's tool_choice is a plain
    # string, so approximate "must call a tool" as "required".
    return "required"


class GatewayChatCompletionClient(ChatCompletionClient):
    """One instance per participant so each carries its own temperature."""

    def __init__(self, *, temperature: float, stub: service_pb2_grpc.GatewayServiceStub) -> None:
        self._temperature = temperature
        self._stub = stub
        self._actual_usage = RequestUsage(prompt_tokens=0, completion_tokens=0)
        self._total_usage = RequestUsage(prompt_tokens=0, completion_tokens=0)

    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | str = "auto",
        json_output: bool | type | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        proto_messages: list[service_pb2.Message] = []
        for m in messages:
            proto_messages.extend(_to_gateway_messages(m))

        request = service_pb2.ChatRequest(
            messages=proto_messages,
            model_config=service_pb2.ModelConfig(temperature=self._temperature),
            tools=[_to_gateway_tool(t) for t in tools],
        )
        # Only send a tool_choice when tools are actually offered — the gateway
        # (like OpenAI) rejects a tool_choice with no tools.
        if tools:
            request.tool_choice = _to_gateway_tool_choice(tool_choice)

        response = await self._stub.Chat(request, timeout=settings.gateway.timeout_seconds)

        usage = RequestUsage(
            prompt_tokens=response.token_usage.prompt_tokens,
            completion_tokens=response.token_usage.completion_tokens,
        )
        # actual_usage() is this call; total_usage() accumulates across calls.
        self._actual_usage = usage
        self._total_usage = RequestUsage(
            prompt_tokens=self._total_usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self._total_usage.completion_tokens + usage.completion_tokens,
        )

        # If the model asked to call tools, surface them as FunctionCalls so
        # AutoGen executes them via the workbench; otherwise it's plain text.
        if response.tool_calls:
            content: str | list[FunctionCall] = [
                FunctionCall(id=tc.id, name=tc.name, arguments=tc.arguments)
                for tc in response.tool_calls
            ]
            finish_reason = "function_calls"
        else:
            content = response.message.content
            finish_reason = (
                response.finish_reason
                if response.finish_reason in _FINISH_REASONS
                else "unknown"
            )

        return CreateResult(
            finish_reason=finish_reason, content=content, usage=usage, cached=False
        )

    async def create_stream(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = [],
        tool_choice: Tool | str = "auto",
        json_output: bool | type | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ):
        yield await self.create(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            json_output=json_output,
            extra_create_args=extra_create_args,
            cancellation_token=cancellation_token,
        )

    async def close(self) -> None:
        pass  # the grpc.aio.Channel is shared and closed by the caller

    def actual_usage(self) -> RequestUsage:
        return self._actual_usage

    def total_usage(self) -> RequestUsage:
        return self._total_usage

    def count_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return sum(len(str(m.content)) for m in messages) // 4

    def remaining_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return max(0, _ASSUMED_MAX_TOKENS - self.count_tokens(messages, tools=tools))

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(vision=False, function_calling=True, json_output=False)

    @property
    def model_info(self) -> ModelInfo:
        return ModelInfo(
            vision=False,
            function_calling=True,
            json_output=False,
            family=ModelFamily.UNKNOWN,
            structured_output=False,
        )
