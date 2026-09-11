"""Tests for the servicer's task RPCs: input validation, tenant extraction, and
the mapping from typed errors to gRPC status codes. Uses a fake ServicerContext
so no real gRPC server is needed."""

from __future__ import annotations

import grpc
import pytest
import pytest_asyncio

from aep.agent_execution.v1 import service_pb2
from agent_execution_service.job_client import JobStepRef
from agent_execution_service.models import AgentRow
from agent_execution_service.servicer import AgentExecutionServicer

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
def servicer(sessions, jobs) -> AgentExecutionServicer:
    return AgentExecutionServicer(sessions, jobs)


@pytest_asyncio.fixture
async def seeded_agent(sessions) -> None:
    """Insert one agent for TENANT so CreateTask's "the tenant must have at
    least one agent" precondition is satisfied. Shares the servicer's database
    (the same `sessions` fixture instance)."""
    async with sessions.begin() as session:
        session.add(
            AgentRow(
                tenant_id=TENANT,
                name="seed-agent",
                instructions="do things",
                llm_config_name="openai/gpt-oss-20b",
                llm_config_temperature=0.0,
            )
        )


async def test_create_task_happy_path(servicer, seeded_agent) -> None:
    resp = await servicer.CreateTask(
        service_pb2.CreateTaskRequest(input="hello"), FakeContext()
    )
    assert resp.task.id
    assert resp.task.job_id
    assert resp.task.status == service_pb2.TASK_STATUS_PENDING


async def test_create_task_without_agents_failed_precondition(servicer) -> None:
    # No agent seeded for TENANT, so the tenant has nothing to run a task
    # against: submission is rejected up front, before any job is created.
    with pytest.raises(Aborted) as exc:
        await servicer.CreateTask(
            service_pb2.CreateTaskRequest(input="hello"), FakeContext()
        )
    assert exc.value.code == grpc.StatusCode.FAILED_PRECONDITION


async def test_create_task_empty_input_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateTask(service_pb2.CreateTaskRequest(input="   "), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_missing_tenant_metadata_unauthenticated(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateTask(
            service_pb2.CreateTaskRequest(input="hi"), FakeContext(tenant_id=None)
        )
    assert exc.value.code == grpc.StatusCode.UNAUTHENTICATED


async def test_get_task_blank_filter_id_invalid_argument(servicer) -> None:
    req = service_pb2.GetTaskRequest(filter=service_pb2.GetTaskRequestFilter(ids=[""]))
    with pytest.raises(Aborted) as exc:
        await servicer.GetTask(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_get_task_returns_created(servicer, seeded_agent) -> None:
    created = await servicer.CreateTask(
        service_pb2.CreateTaskRequest(input="hi"), FakeContext()
    )
    resp = await servicer.GetTask(service_pb2.GetTaskRequest(), FakeContext())
    assert [t.id for t in resp.tasks] == [created.task.id]


async def test_approve_blank_id_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.ApproveTask(service_pb2.ApproveTaskRequest(task_id=" "), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_approve_unknown_task_not_found(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.ApproveTask(
            service_pb2.ApproveTaskRequest(task_id="missing"), FakeContext()
        )
    assert exc.value.code == grpc.StatusCode.NOT_FOUND


async def test_approve_twice_failed_precondition(servicer, jobs, seeded_agent) -> None:
    created = await servicer.CreateTask(
        service_pb2.CreateTaskRequest(input="go"), FakeContext()
    )
    tid = created.task.id
    jobs.set_status(created.task.job_id, "waiting_approval")  # runner paused on the gate
    await servicer.ApproveTask(service_pb2.ApproveTaskRequest(task_id=tid), FakeContext())
    # The job is now queued (waiting_approval -> queued); a second approve has no
    # paused job to resume, so it fails the precondition.
    with pytest.raises(Aborted) as exc:
        await servicer.ApproveTask(service_pb2.ApproveTaskRequest(task_id=tid), FakeContext())
    assert exc.value.code == grpc.StatusCode.FAILED_PRECONDITION


async def test_create_agent_blank_name_invalid_argument(servicer) -> None:
    req = service_pb2.CreateAgentRequest(name=" ", instructions="do stuff")
    with pytest.raises(Aborted) as exc:
        await servicer.CreateAgent(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_agent_blank_instructions_invalid_argument(servicer) -> None:
    req = service_pb2.CreateAgentRequest(name="agent", instructions=" ")
    with pytest.raises(Aborted) as exc:
        await servicer.CreateAgent(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_agent_empty_tool_ids_invalid_argument(servicer) -> None:
    req = service_pb2.CreateAgentRequest(name="agent", instructions="do stuff")
    with pytest.raises(Aborted) as exc:
        await servicer.CreateAgent(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_update_agent_empty_tool_ids_invalid_argument(servicer) -> None:
    req = service_pb2.UpdateAgentRequest(id="a1", name="agent", instructions="do stuff")
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateAgent(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_get_agent_blank_filter_id_invalid_argument(servicer) -> None:
    req = service_pb2.GetAgentRequest(filter=service_pb2.GetAgentRequestFilter(ids=[""]))
    with pytest.raises(Aborted) as exc:
        await servicer.GetAgent(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_update_agent_blank_id_invalid_argument(servicer) -> None:
    req = service_pb2.UpdateAgentRequest(id=" ", name="agent", instructions="do stuff")
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateAgent(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_delete_agent_blank_id_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.DeleteAgent(service_pb2.DeleteAgentRequest(id=" "), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_create_tool_blank_name_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.CreateTool(service_pb2.CreateToolRequest(name=" "), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_get_tool_blank_filter_id_invalid_argument(servicer) -> None:
    req = service_pb2.GetToolRequest(filter=service_pb2.GetToolRequestFilter(ids=[""]))
    with pytest.raises(Aborted) as exc:
        await servicer.GetTool(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_update_tool_blank_id_invalid_argument(servicer) -> None:
    req = service_pb2.UpdateToolRequest(id=" ", name="tool")
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateTool(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_update_tool_blank_name_invalid_argument(servicer) -> None:
    req = service_pb2.UpdateToolRequest(id="t1", name=" ")
    with pytest.raises(Aborted) as exc:
        await servicer.UpdateTool(req, FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_delete_tool_blank_id_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.DeleteTool(service_pb2.DeleteToolRequest(id=" "), FakeContext())
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_approve_transitions_status(servicer, jobs, seeded_agent) -> None:
    created = await servicer.CreateTask(
        service_pb2.CreateTaskRequest(input="go"), FakeContext()
    )
    jobs.set_status(created.task.job_id, "waiting_approval")  # runner paused on the gate
    resp = await servicer.ApproveTask(
        service_pb2.ApproveTaskRequest(task_id=created.task.id), FakeContext()
    )
    assert resp.success is True
    # Approve requeues the paused job (waiting_approval -> queued), so the task
    # snapshot reads back as pending for the poller to re-run.
    got = await servicer.GetTask(service_pb2.GetTaskRequest(), FakeContext())
    assert got.tasks[0].status == service_pb2.TASK_STATUS_PENDING


async def test_get_task_progress_happy_path(servicer, jobs, seeded_agent) -> None:
    created = await servicer.CreateTask(
        service_pb2.CreateTaskRequest(input="go"), FakeContext()
    )
    tid = created.task.id
    jobs.set_status(created.task.job_id, "running")
    jobs.set_progress(
        created.task.job_id,
        steps_total=2,
        steps=(
            JobStepRef(
                index=0, description="d", output="o", agent="A", requires_approval=False
            ),
        ),
    )

    resp = await servicer.GetTaskProgress(
        service_pb2.GetTaskProgressRequest(task_id=tid), FakeContext()
    )
    assert resp.progress.task_id == tid
    assert resp.progress.status == service_pb2.TASK_STATUS_RUNNING
    assert resp.progress.steps_total == 2
    assert resp.progress.steps_completed == 1
    assert [s.output for s in resp.progress.steps] == ["o"]


async def test_get_task_progress_blank_id_invalid_argument(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.GetTaskProgress(
            service_pb2.GetTaskProgressRequest(task_id=" "), FakeContext()
        )
    assert exc.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_get_task_progress_unknown_task_not_found(servicer) -> None:
    with pytest.raises(Aborted) as exc:
        await servicer.GetTaskProgress(
            service_pb2.GetTaskProgressRequest(task_id="missing"), FakeContext()
        )
    assert exc.value.code == grpc.StatusCode.NOT_FOUND
