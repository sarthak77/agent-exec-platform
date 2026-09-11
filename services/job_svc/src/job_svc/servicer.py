"""gRPC servicer: thin adapter between JobService's generated stubs and the
services/* business logic. Extracts the caller's tenant id from metadata,
converts ORM rows <-> protobuf via mappers, and maps typed errors to gRPC
status codes.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import grpc
from sqlalchemy.ext.asyncio import async_sessionmaker

from aep.job.v1 import service_pb2, service_pb2_grpc
from job_svc.auth import tenant_id_from_metadata
from job_svc.config import settings
from job_svc.errors import (
    AppError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)
from job_svc.mappers import STATUS_FROM_PROTO, TYPE_FROM_PROTO, job_to_proto, spec_to_dict
from job_svc.services.jobs import JobService
from job_svc.validators import JobValidator

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: dict[type[Exception], grpc.StatusCode] = {
    NotFoundError: grpc.StatusCode.NOT_FOUND,
    ValidationError: grpc.StatusCode.INVALID_ARGUMENT,
    AuthenticationError: grpc.StatusCode.UNAUTHENTICATED,
    # ALREADY_EXISTS, not ABORTED: ConflictError means "this would duplicate an
    # existing resource," the same conflict agent_execution_service's servicer
    # maps to ALREADY_EXISTS -- keep both services on one convention for the
    # same typed error.
    ConflictError: grpc.StatusCode.ALREADY_EXISTS,
    StateError: grpc.StatusCode.FAILED_PRECONDITION,
}

P = ParamSpec("P")
T = TypeVar("T")


def _handle_errors(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
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


class JobServicer(service_pb2_grpc.JobServiceServicer):
    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        default_max_attempts: int | None = None,
        default_max_retries: int | None = None,
    ) -> None:
        if default_max_attempts is None:
            default_max_attempts = settings.jobs.default_max_attempts
        if default_max_retries is None:
            default_max_retries = settings.jobs.default_max_retries
        self._jobs = JobService(
            sessions,
            default_max_attempts=default_max_attempts,
            default_max_retries=default_max_retries,
        )

    @_handle_errors
    async def CreateJob(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        job_type = JobValidator.validate_type(request.type)
        JobValidator.validate_spec(job_type, request.spec)
        max_attempts = request.max_attempts if request.HasField("max_attempts") else None
        JobValidator.validate_max_attempts(max_attempts)
        max_retries = request.max_retries if request.HasField("max_retries") else None
        JobValidator.validate_max_retries(max_retries)
        row = await self._jobs.create(
            tenant_id=tenant_id,
            type=job_type,
            spec=spec_to_dict(request.spec),
            max_attempts=max_attempts,
            max_retries=max_retries,
        )
        return service_pb2.CreateJobResponse(job=job_to_proto(row))

    @_handle_errors
    async def GetJob(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        JobValidator.validate_filter_ids(list(request.filter.ids))
        statuses = JobValidator.validate_filter_enums(
            request.filter.statuses, STATUS_FROM_PROTO, "status"
        )
        types = JobValidator.validate_filter_enums(request.filter.types, TYPE_FROM_PROTO, "type")
        rows = await self._jobs.get(
            tenant_id=tenant_id, ids=list(request.filter.ids), statuses=statuses, types=types
        )
        return service_pb2.GetJobResponse(jobs=[job_to_proto(row) for row in rows])

    @_handle_errors
    async def UpdateJob(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        JobValidator.validate_job_id(request.id)
        status = JobValidator.validate_status(request.status)
        row = await self._jobs.update(tenant_id=tenant_id, job_id=request.id, status=status)
        return service_pb2.UpdateJobResponse(job=job_to_proto(row))

    @_handle_errors
    async def RetryJob(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        JobValidator.validate_job_id(request.id)
        row = await self._jobs.retry(tenant_id=tenant_id, job_id=request.id)
        return service_pb2.RetryJobResponse(job=job_to_proto(row))
