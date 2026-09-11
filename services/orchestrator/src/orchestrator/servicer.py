"""gRPC servicer: thin adapter between OrchestratorServiceServicer's
generated Chat method and run.py's group-chat logic. Extracts the caller's
tenant id from metadata, converts proto Message <-> run.py's plain
dataclasses, and maps typed errors to gRPC status codes. Mirrors
agent_execution_service/servicer.py's error-mapping decorator.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import grpc

from aep.orchestrator.v1 import service_pb2, service_pb2_grpc
from orchestrator.auth import tenant_id_from_metadata
from orchestrator.errors import AppError, AuthenticationError, NoAgentsError
from orchestrator.run import run_chat

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: dict[type[Exception], grpc.StatusCode] = {
    AuthenticationError: grpc.StatusCode.UNAUTHENTICATED,
    NoAgentsError: grpc.StatusCode.FAILED_PRECONDITION,
}

# Codes from a failed gateway (dependency) RPC that we surface to our caller
# verbatim, so a transient gateway outage reads as retryable rather than a
# generic internal error. Anything else collapses to INTERNAL.
_PROPAGATED_UPSTREAM: frozenset[grpc.StatusCode] = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
    }
)

P = ParamSpec("P")
T = TypeVar("T")


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
        except grpc.aio.AioRpcError as exc:
            upstream = exc.code()
            logger.warning("gateway rpc failed in %s: %s %s", fn.__name__, upstream, exc.details())
            code = upstream if upstream in _PROPAGATED_UPSTREAM else grpc.StatusCode.INTERNAL
            await context.abort(code, f"gateway dependency error: {exc.details()}")
        except Exception:
            logger.exception("unhandled error in %s", fn.__name__)
            await context.abort(grpc.StatusCode.INTERNAL, "internal error")

    return wrapper


class OrchestratorServicer(service_pb2_grpc.OrchestratorServiceServicer):
    @_handle_errors
    async def Chat(self, request: service_pb2.ChatRequest, context: grpc.aio.ServicerContext):
        tenant_id = tenant_id_from_metadata(context)
        result = await run_chat(tenant_id, request.messages)
        return service_pb2.ChatResponse(
            messages=[
                service_pb2.Message(role=m.role, content=m.content, agent=m.agent)
                for m in result.messages
            ],
            token_usage=service_pb2.TokenUsage(
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
            ),
            finish_reason=result.finish_reason,
        )
