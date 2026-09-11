"""Tests for JobServicer's RPCs: input validation, tenant extraction, and the
mapping from typed errors to gRPC status codes. Uses a fake ServicerContext so
no real gRPC server is needed."""

from __future__ import annotations

import grpc
import pytest

from aep.job.v1 import service_pb2
from job_svc.servicer import JobServicer

TENANT = "t1"


class Aborted(Exception):
    """Stands in for the exception grpc raises out of context.abort()."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        self.code = code
        self.details = details


class FakeContext:
    def __init__(self, tenant_id: str | None = TENANT) -> None:
        self._md = [("x-tenant-id", tenant_id)] if tenant_id is not None else []

    def invocation_metadata(self):
        return self._md

    async def abort(self, code, details):  # noqa: ANN001
        raise Aborted(code, details)


@pytest.fixture
def servicer(sessions) -> JobServicer:
    return JobServicer(sessions, default_max_attempts=3, default_max_retries=3)


def _create_req(
    *,
    type=service_pb2.JOB_TYPE_AGENT_EXECUTION,
    max_attempts=None,
    max_retries=None,
    spec=None,
):
    if spec is None:
        spec = service_pb2.JobSpec(
            agent_execution_spec=service_pb2.AgentExecutionSpec(instructions="do the thing")
        )
    kw = {"type": type, "spec": spec}
    if max_attempts is not None:
        kw["max_attempts"] = max_attempts
    if max_retries is not None:
        kw["max_retries"] = max_retries
    return service_pb2.CreateJobRequest(**kw)


# --- CreateJob -------------------------------------------------------------


async def test_create_job_happy_path(servicer) -> None:
    resp = await servicer.CreateJob(_create_req(), FakeContext())
    assert resp.job.id
    assert resp.job.status == service_pb2.JOB_STATUS_QUEUED
    assert resp.job.max_attempts == 3  # config-driven default applied


async def test_create_job_explicit_max_attempts(servicer) -> None:
    resp = await servicer.CreateJob(_create_req(max_attempts=5), FakeContext())
    assert resp.job.max_attempts == 5


async def test_create_job_default_max_retries(servicer) -> None:
    resp = await servicer.CreateJob(_create_req(), FakeContext())
    assert resp.job.max_retries == 3  # config-driven default applied


async def test_create_job_explicit_max_retries(servicer) -> None:
    resp = await servicer.CreateJob(_create_req(max_retries=7), FakeContext())
    assert resp.job.max_retries == 7


async def test_create_job_negative_max_retries_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(_create_req(max_retries=-1), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_job_unspecified_type_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(_create_req(type=service_pb2.JOB_TYPE_UNSPECIFIED), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_job_zero_max_attempts_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(_create_req(max_attempts=0), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_job_mutation_type_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(_create_req(type=service_pb2.JOB_TYPE_MUTATION), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_job_missing_spec_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(
            _create_req(spec=service_pb2.JobSpec()), FakeContext()
        )
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_job_blank_instructions_invalid_argument(servicer) -> None:
    spec = service_pb2.JobSpec(
        agent_execution_spec=service_pb2.AgentExecutionSpec(instructions="   ")
    )
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(_create_req(spec=spec), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_missing_tenant_metadata_unauthenticated(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateJob(_create_req(), FakeContext(tenant_id=None))
    assert exc.value.code == grpc.StatusCode.UNAUTHENTICATED


# --- GetJob ----------------------------------------------------------------


async def test_get_job_returns_created(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    resp = await servicer.GetJob(service_pb2.GetJobRequest(), FakeContext())
    assert [j.id for j in resp.jobs] == [created.job.id]


async def test_get_job_blank_filter_id_invalid_argument(servicer) -> None:
    req = service_pb2.GetJobRequest(filter=service_pb2.GetJobRequestFilter(ids=[""]))
    with pytest.raises(Aborted) as exc:
        await servicer.GetJob(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_get_job_surfaces_progress(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    # A runner checkpoint written to job_svc's store...
    await servicer._jobs.save_progress(
        job_id=created.job.id,
        progress={
            "phase": "executing",
            "plan": ["a", "b"],
            "steps": {"0": {"prompt": "a", "output": "done: a", "finish_reason": "stop"}},
        },
    )
    # ...is visible to a caller reading the job back.
    resp = await servicer.GetJob(service_pb2.GetJobRequest(), FakeContext())
    progress = resp.jobs[0].progress
    assert progress.phase == service_pb2.JOB_PHASE_EXECUTING
    assert list(progress.plan) == ["a", "b"]
    assert progress.steps[0].index == 0
    assert progress.steps[0].output == "done: a"
    assert progress.steps[0].finish_reason == service_pb2.FINISH_REASON_STOP


async def test_get_job_status_filter(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    req = service_pb2.GetJobRequest(
        filter=service_pb2.GetJobRequestFilter(statuses=[service_pb2.JOB_STATUS_QUEUED])
    )
    resp = await servicer.GetJob(req, FakeContext())
    assert [j.id for j in resp.jobs] == [created.job.id]


# --- UpdateJob -------------------------------------------------------------


async def test_update_job_transitions_status(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    resp = await servicer.UpdateJob(
        service_pb2.UpdateJobRequest(id=created.job.id, status=service_pb2.JOB_STATUS_RUNNING),
        FakeContext(),
    )
    assert resp.job.status == service_pb2.JOB_STATUS_RUNNING
    assert resp.job.attempts == 1


async def test_update_job_unspecified_status_invalid_argument(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateJob(
            service_pb2.UpdateJobRequest(id=created.job.id), FakeContext()
        )
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_update_job_blank_id_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateJob(
            service_pb2.UpdateJobRequest(id=" ", status=service_pb2.JOB_STATUS_RUNNING),
            FakeContext(),
        )
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_update_job_unknown_not_found(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateJob(
            service_pb2.UpdateJobRequest(id="missing", status=service_pb2.JOB_STATUS_RUNNING),
            FakeContext(),
        )
    assert exc.value.code == grpc.StatusCode.NOT_FOUND


async def test_update_job_bad_precondition_failed_precondition(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    # succeeded is only reachable from running; the job is still queued.
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateJob(
            service_pb2.UpdateJobRequest(
                id=created.job.id, status=service_pb2.JOB_STATUS_SUCCEEDED
            ),
            FakeContext(),
        )
    assert exc.value.code == grpc.StatusCode.FAILED_PRECONDITION


# --- RetryJob --------------------------------------------------------------


async def test_retry_job_blank_id_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.RetryJob(service_pb2.RetryJobRequest(id=""), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_retry_job_on_queued_failed_precondition(servicer) -> None:
    created = await servicer.CreateJob(_create_req(), FakeContext())
    with pytest.raises(Aborted) as exc:
        await servicer.RetryJob(service_pb2.RetryJobRequest(id=created.job.id), FakeContext())
    assert exc.value.code == grpc.StatusCode.FAILED_PRECONDITION
