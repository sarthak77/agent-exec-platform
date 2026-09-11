"""Client to job_svc — every task is submitted as (and driven through) a job.

`TaskService` depends on the small `JobGateway` protocol below rather than on a
concrete stub, so the business logic stays protobuf-agnostic and unit tests can
inject a fake without standing up a real job_svc. `JobClient` is the production
implementation: it wraps the generated `JobServiceStub` over a single, lazily
created, process-wide channel (grpc channels multiplex concurrent RPCs, so one
shared channel is safe and cheaper than per-call setup — the same pattern the
orchestrator uses for its gateway client) and translates job_svc's gRPC status
codes back into this service's typed errors so the servicer maps them uniformly.

The tenant id is forwarded to job_svc as the same `x-tenant-id` metadata header
this service itself is called with, so jobs are created and mutated under the
caller's tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import grpc

from aep.job.v1 import service_pb2, service_pb2_grpc
from agent_execution_service.config import settings
from agent_execution_service.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)


@dataclass(frozen=True, slots=True)
class JobRef:
    """The slice of a job_svc Job this service cares about: its id and current
    status (a job_svc status string such as ``"queued"`` / ``"running"``)."""

    id: str
    status: str


@runtime_checkable
class JobGateway(Protocol):
    """What TaskService needs from job_svc. Kept protobuf-free so it is trivial
    to fake in tests."""

    async def create_job(self, *, tenant_id: str, input: str) -> JobRef: ...

    async def start_job(self, *, tenant_id: str, job_id: str) -> JobRef: ...

    async def retry_job(self, *, tenant_id: str, job_id: str) -> JobRef: ...


# job_svc gRPC status code -> our typed error. Anything unmapped bubbles up as a
# generic AppError (INTERNAL) rather than being silently swallowed.
_ERROR_BY_CODE: dict[grpc.StatusCode, type[AppError]] = {
    grpc.StatusCode.NOT_FOUND: NotFoundError,
    grpc.StatusCode.INVALID_ARGUMENT: ValidationError,
    grpc.StatusCode.FAILED_PRECONDITION: StateError,
    grpc.StatusCode.ALREADY_EXISTS: ConflictError,
    grpc.StatusCode.ABORTED: ConflictError,
}


def _md(tenant_id: str) -> list[tuple[str, str]]:
    return [("x-tenant-id", tenant_id)]


class JobClient(JobGateway):
    def __init__(self) -> None:
        self._channel: grpc.aio.Channel | None = None
        self._stub: service_pb2_grpc.JobServiceStub | None = None

    def _get_stub(self) -> service_pb2_grpc.JobServiceStub:
        # No await before assignment, so under a single event loop the first
        # caller fully initializes the singleton before any other observes it.
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(
                f"{settings.job_svc.host}:{settings.job_svc.port}"
            )
            self._stub = service_pb2_grpc.JobServiceStub(self._channel)
        return self._stub

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def create_job(self, *, tenant_id: str, input: str) -> JobRef:
        # A task carries only a free-form input string, so it is submitted as an
        # agent-execution job whose instructions are that input. max_attempts is
        # left unset so job_svc applies its own default.
        request = service_pb2.CreateJobRequest(
            type=service_pb2.JOB_TYPE_AGENT_EXECUTION,
            spec=service_pb2.JobSpec(
                agent_execution_spec=service_pb2.AgentExecutionSpec(instructions=input)
            ),
        )
        response = await self._call(self._get_stub().CreateJob, request, tenant_id)
        return _to_ref(response.job)

    async def start_job(self, *, tenant_id: str, job_id: str) -> JobRef:
        # Approving a task resumes its paused job: job_svc transitions
        # waiting_approval -> queued (guarded atomically), and the poller then
        # re-claims and re-runs it from its checkpoint. UpdateJob(QUEUED) is the
        # non-resetting requeue; from waiting_approval it is exactly the approval
        # transition (see job_svc's _ALLOWED_ENTRY).
        request = service_pb2.UpdateJobRequest(id=job_id, status=service_pb2.JOB_STATUS_QUEUED)
        response = await self._call(self._get_stub().UpdateJob, request, tenant_id)
        return _to_ref(response.job)

    async def retry_job(self, *, tenant_id: str, job_id: str) -> JobRef:
        request = service_pb2.RetryJobRequest(id=job_id)
        response = await self._call(self._get_stub().RetryJob, request, tenant_id)
        return _to_ref(response.job)

    async def _call(self, method, request, tenant_id: str):  # noqa: ANN001
        try:
            return await method(request, metadata=_md(tenant_id))
        except grpc.aio.AioRpcError as exc:
            err_cls = _ERROR_BY_CODE.get(exc.code(), AppError)
            raise err_cls(f"job_svc: {exc.details()}") from exc


# job_svc JobStatus enum -> its canonical lowercase status string.
_JOB_STATUS_STR = {
    service_pb2.JOB_STATUS_QUEUED: "queued",
    service_pb2.JOB_STATUS_RUNNING: "running",
    service_pb2.JOB_STATUS_SUCCEEDED: "succeeded",
    service_pb2.JOB_STATUS_FAILED: "failed",
    service_pb2.JOB_STATUS_DEAD: "dead",
    service_pb2.JOB_STATUS_CANCELLED: "cancelled",
    service_pb2.JOB_STATUS_WAITING_APPROVAL: "waiting_approval",
}


def _to_ref(job: service_pb2.Job) -> JobRef:
    return JobRef(id=job.id, status=_JOB_STATUS_STR.get(job.status, "unspecified"))
