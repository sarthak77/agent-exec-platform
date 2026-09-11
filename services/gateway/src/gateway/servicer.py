"""gRPC servicer: guardrails -> single-model provider call -> response.
Mirrors agent_execution_service/servicer.py's error-mapping decorator.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import grpc

from aep.gateway.v1 import service_pb2, service_pb2_grpc
from gateway.errors import AppError, GuardrailRejected, ProviderError, ValidationError
from gateway.guardrails import screen_and_sanitize
from gateway.models import Message, ToolCall, ToolSpec
from gateway.provider import OpenAIProvider

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: dict[type[Exception], grpc.StatusCode] = {
    GuardrailRejected: grpc.StatusCode.INVALID_ARGUMENT,
    ValidationError: grpc.StatusCode.INVALID_ARGUMENT,
    ProviderError: grpc.StatusCode.UNAVAILABLE,
}

P = ParamSpec("P")
T = TypeVar("T")


def _message_from_proto(m: service_pb2.Message) -> Message:
    return Message(
        role=m.role,
        content=m.content,
        tool_calls=tuple(
            ToolCall(id=tc.id, name=tc.name, arguments=tc.arguments) for tc in m.tool_calls
        ),
        tool_call_id=m.tool_call_id if m.HasField("tool_call_id") else None,
    )


def _tool_calls_to_proto(tool_calls) -> list[service_pb2.ToolCall]:
    return [
        service_pb2.ToolCall(id=tc.id, name=tc.name, arguments=tc.arguments)
        for tc in tool_calls
    ]


def _handle_errors(
    fn: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    @functools.wraps(fn)
    async def wrapper(self, request, context: grpc.aio.ServicerContext):  # noqa: ANN001
        try:
            return await fn(self, request, context)
        except AppError as exc:
            code = _STATUS_BY_ERROR.get(type(exc), grpc.StatusCode.INTERNAL)
            await context.abort(code, str(exc))
        except Exception:
            logger.exception("unhandled error in %s", fn.__name__)
            await context.abort(grpc.StatusCode.INTERNAL, "internal error")

    return wrapper


class GatewayServicer(service_pb2_grpc.GatewayServiceServicer):
    def __init__(self, provider: OpenAIProvider, *, guardrails) -> None:
        self._provider = provider
        self._guardrails = guardrails

    @_handle_errors
    async def Chat(self, request: service_pb2.ChatRequest, context: grpc.aio.ServicerContext):
        messages = [_message_from_proto(m) for m in request.messages]
        sanitized = screen_and_sanitize(
            messages,
            max_input_chars=self._guardrails.max_input_chars,
            blocklist=self._guardrails.blocklist,
        )

        max_tokens = request.model_config.max_tokens if request.model_config.HasField("max_tokens") else None
        temperature = (
            request.model_config.temperature if request.model_config.HasField("temperature") else None
        )
        tools = [
            ToolSpec(name=t.name, description=t.description, parameters=t.parameters)
            for t in request.tools
        ]
        tool_choice = request.tool_choice if request.HasField("tool_choice") else None

        completion = await self._provider.complete(
            sanitized,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )

        proto_tool_calls = _tool_calls_to_proto(completion.tool_calls)
        return service_pb2.ChatResponse(
            message=service_pb2.Message(
                role="assistant",
                content=completion.content,
                tool_calls=proto_tool_calls,
            ),
            token_usage=service_pb2.TokenUsage(
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                total_tokens=completion.usage.total_tokens,
            ),
            finish_reason=completion.finish_reason,
            tool_calls=proto_tool_calls,
        )
