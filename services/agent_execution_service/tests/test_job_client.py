"""Tests for JobClient's protobuf conversion and gRPC-error translation. These
exercise the pure translation logic without opening a real channel to job_svc.
"""

from __future__ import annotations

import grpc
import pytest

from aep.job.v1 import service_pb2
from agent_execution_service.errors import NotFoundError, StateError, ValidationError
from agent_execution_service.job_client import JobClient, _to_ref


class FakeRpcError(grpc.aio.AioRpcError):
    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details


def test_to_ref_maps_status_string() -> None:
    job = service_pb2.Job(id="j1", status=service_pb2.JOB_STATUS_RUNNING)
    ref = _to_ref(job)
    assert ref.id == "j1"
    assert ref.status == "running"


@pytest.mark.parametrize(
    "code,expected",
    [
        (grpc.StatusCode.NOT_FOUND, NotFoundError),
        (grpc.StatusCode.FAILED_PRECONDITION, StateError),
        (grpc.StatusCode.INVALID_ARGUMENT, ValidationError),
    ],
)
async def test_call_translates_grpc_errors(code, expected) -> None:
    client = JobClient()

    async def boom(request, metadata=None):  # noqa: ANN001
        raise FakeRpcError(code, "kaboom")

    with pytest.raises(expected, match="job_svc: kaboom"):
        await client._call(boom, object(), tenant_id="t1")
