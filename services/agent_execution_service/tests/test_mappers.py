"""Tests for task <-> proto mapping."""

from __future__ import annotations

from aep.agent_execution.v1 import service_pb2
from agent_execution_service.mappers import task_to_proto
from agent_execution_service.models import TaskRow, now


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
