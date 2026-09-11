"""Tests for task <-> proto mapping."""

from __future__ import annotations

import pytest

from aep.agent_execution.v1 import service_pb2
from agent_execution_service.job_client import JobStepRef
from agent_execution_service.mappers import task_progress_to_proto, task_to_proto
from agent_execution_service.models import TaskRow, now
from agent_execution_service.services.tasks import TaskProgressView


def _row(status: str) -> TaskRow:
    return TaskRow(
        id="task-1",
        tenant_id="t1",
        input="hello",
        job_id="job-1",
        status=status,
        created_at=now(),
        updated_at=now(),
    )


def test_task_to_proto_carries_job_id_and_input() -> None:
    proto = task_to_proto(_row("pending"))
    assert proto.id == "task-1"
    assert proto.job_id == "job-1"
    assert proto.input == "hello"


def test_status_mapping() -> None:
    assert task_to_proto(_row("pending")).status == service_pb2.TASK_STATUS_PENDING
    assert task_to_proto(_row("running")).status == service_pb2.TASK_STATUS_RUNNING
    assert task_to_proto(_row("completed")).status == service_pb2.TASK_STATUS_COMPLETED
    assert task_to_proto(_row("failed")).status == service_pb2.TASK_STATUS_FAILED


def test_unknown_status_maps_to_unspecified() -> None:
    assert task_to_proto(_row("bogus")).status == service_pb2.TASK_STATUS_UNSPECIFIED


def test_task_to_proto_surfaces_result_when_present() -> None:
    row = _row("completed")
    row.result = "the final answer"
    proto = task_to_proto(row)
    assert proto.HasField("result")
    assert proto.result.output == "the final answer"


def test_task_to_proto_leaves_result_unset_when_empty() -> None:
    # A task with no output yet must not carry a result, so a client can tell
    # "not done" from an empty answer.
    proto = task_to_proto(_row("pending"))
    assert not proto.HasField("result")


def test_task_progress_to_proto_maps_fields_and_steps() -> None:
    view = TaskProgressView(
        task_id="task-1",
        status="running",
        percent_complete=50.0,
        steps_completed=1,
        steps_total=2,
        requires_approval=False,
        summary="Running: 1 of 2 steps done",
        steps=(
            JobStepRef(
                index=0, description="d0", output="o0", agent="A", requires_approval=False
            ),
        ),
        output="",
        error="",
        updated_at=now(),
    )
    proto = task_progress_to_proto(view)
    assert proto.task_id == "task-1"
    assert proto.status == service_pb2.TASK_STATUS_RUNNING
    assert proto.percent_complete == pytest.approx(50.0)
    assert proto.steps_completed == 1
    assert proto.steps_total == 2
    assert proto.summary == "Running: 1 of 2 steps done"
    assert len(proto.steps) == 1
    assert proto.steps[0].index == 0
    assert proto.steps[0].description == "d0"
    assert proto.steps[0].output == "o0"
    assert proto.steps[0].agent == "A"


def test_task_progress_to_proto_maps_status_and_approval() -> None:
    view = TaskProgressView(
        task_id="t",
        status="waiting_approval",
        percent_complete=0.0,
        steps_completed=0,
        steps_total=1,
        requires_approval=True,
        summary="Waiting for approval",
        steps=(),
        output="",
        error="",
        updated_at=now(),
    )
    proto = task_progress_to_proto(view)
    assert proto.status == service_pb2.TASK_STATUS_WAITING_APPROVAL
    assert proto.requires_approval is True
