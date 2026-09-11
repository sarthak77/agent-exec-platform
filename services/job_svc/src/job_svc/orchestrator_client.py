"""Client to the orchestrator service — the runner's dependency for both
decomposing a job's prompt and executing each resulting sub-prompt.

The runner depends on the small `OrchestratorGateway` protocol below rather than
on the generated stub, so the runner stays protobuf-agnostic and unit tests can
inject a fake without standing up a real orchestrator. `OrchestratorClient` is
the production implementation: it wraps the generated `OrchestratorServiceStub`
over a single, lazily created, process-wide channel (grpc channels multiplex
concurrent RPCs, so one shared channel is safe and cheaper than per-call setup --
the same pattern job_svc's callers use) and translates orchestrator gRPC failures
into a typed `DependencyError`.

The tenant id is forwarded to the orchestrator as the same `x-tenant-id` metadata
header every other service is called with, so the group chat runs under the
owning tenant's configured agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import grpc

from aep.orchestrator.v1 import service_pb2, service_pb2_grpc
from job_svc.config import settings
from job_svc.errors import DependencyError


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One input message to the orchestrator's group chat."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class ChatReply:
    """The slice of a ChatResponse the runner cares about: the final assistant
    message (the group chat's answer for this turn), which of the tenant's
    group-chat agents produced it, and why it stopped."""

    content: str
    finish_reason: str
    agent: str = ""


@runtime_checkable
class OrchestratorGateway(Protocol):
    """What the runner needs from the orchestrator. Kept protobuf-free so it is
    trivial to fake in tests."""

    async def chat(self, *, tenant_id: str, messages: list[ChatTurn]) -> ChatReply: ...


def _md(tenant_id: str) -> list[tuple[str, str]]:
    return [("x-tenant-id", tenant_id)]


def _final_message(response: service_pb2.ChatResponse) -> service_pb2.Message:
    # The transcript is the messages produced by the group chat this turn; the
    # last non-empty one is the consolidated answer. Empty transcript -> "".
    for message in reversed(response.messages):
        if message.content:
            return message
    return service_pb2.Message()


class OrchestratorClient(OrchestratorGateway):
    def __init__(self) -> None:
        self._channel: grpc.aio.Channel | None = None
        self._stub: service_pb2_grpc.OrchestratorServiceStub | None = None

    def _get_stub(self) -> service_pb2_grpc.OrchestratorServiceStub:
        # No await before assignment, so under a single event loop the first
        # caller fully initializes the singleton before any other observes it.
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                f"{settings.orchestrator.host}:{settings.orchestrator.port}"
            )
            self._stub = service_pb2_grpc.OrchestratorServiceStub(self._channel)
        return self._stub

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def chat(self, *, tenant_id: str, messages: list[ChatTurn]) -> ChatReply:
        request = service_pb2.ChatRequest(
            messages=[service_pb2.Message(role=t.role, content=t.content) for t in messages]
        )
        try:
            response = await self._get_stub().Chat(request, metadata=_md(tenant_id))
        except grpc.aio.AioRpcError as exc:
            raise DependencyError(f"orchestrator: {exc.code().name}: {exc.details()}") from exc
        final = _final_message(response)
        return ChatReply(
            content=final.content, finish_reason=response.finish_reason, agent=final.agent
        )

