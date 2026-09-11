"""Tests for JobClient's protobuf conversion and gRPC-error translation. These
exercise the pure translation logic without opening a real channel to job_svc.
"""

from __future__ import annotations

import grpc
import pytest

from aep.job.v1 import service_pb2
from agent_execution_service.errors import NotFoundError, StateError, ValidationError
from agent_execution_service.job_client import JobClient, _to_progress_ref, _to_ref


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
    assert ref.result == ""  # no result oneof set yet


def test_to_ref_surfaces_agent_execution_result() -> None:
    job = service_pb2.Job(
        id="j1",
        status=service_pb2.JOB_STATUS_SUCCEEDED,
        result=service_pb2.JobResult(
            agent_execution_result=service_pb2.AgentExecutionResult(output="done")
        ),
    )
    ref = _to_ref(job)
    assert ref.status == "succeeded"
    assert ref.result == "done"


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


def test_to_progress_ref_extracts_ordered_step_history() -> None:
    # steps arrive out of order and must be surfaced sorted by plan index; the
    # finish reason maps to the per-step requires_approval flag.
    job = service_pb2.Job(
        id="j1",
        status=service_pb2.JOB_STATUS_RUNNING,
        progress=service_pb2.JobProgress(
            plan=["a", "b", "c"],
            steps=[
                service_pb2.JobStep(
                    index=1,
                    prompt="p1",
                    output="o1",
                    agent="A1",
                    finish_reason=service_pb2.FINISH_REASON_REQUIRES_APPROVAL,
                ),
                service_pb2.JobStep(
                    index=0,
                    prompt="p0",
                    output="o0",
                    agent="A0",
                    finish_reason=service_pb2.FINISH_REASON_STOP,
                ),
            ],
            error="uh oh",
        ),
    )
    ref = _to_progress_ref(job)
    assert ref.status == "running"
    assert ref.steps_total == 3
    assert ref.error == "uh oh"
    assert [s.index for s in ref.steps] == [0, 1]
    assert ref.steps[0].description == "p0"
    assert ref.steps[0].requires_approval is False
    assert ref.steps[1].requires_approval is True


def test_to_progress_ref_surfaces_final_result_and_empty_progress() -> None:
    job = service_pb2.Job(
        id="j1",
        status=service_pb2.JOB_STATUS_SUCCEEDED,
        result=service_pb2.JobResult(
            agent_execution_result=service_pb2.AgentExecutionResult(output="done")
        ),
    )
    ref = _to_progress_ref(job)
    assert ref.result == "done"
    assert ref.steps == ()  # unset progress reads back empty
    assert ref.steps_total == 0
