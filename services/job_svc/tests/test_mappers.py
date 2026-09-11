"""Tests for ORM row <-> protobuf mapping."""

from __future__ import annotations

import pytest

from aep.job.v1 import service_pb2
from job_svc.mappers import job_to_proto, progress_to_proto, spec_from_dict, spec_to_dict
from job_svc.models import JobRow, now


def _row(status: str = "queued", type: str = "agent_execution", **kw) -> JobRow:
    defaults = dict(
        id="job-1",
        tenant_id="t1",
        type=type,
        spec={"agent_execution_spec": {"name": "demo"}},
        status=status,
        attempts=1,
        max_attempts=3,
        created_at=now(),
        updated_at=now(),
    )
    defaults.update(kw)
    return JobRow(**defaults)


def test_job_to_proto_carries_scalar_fields() -> None:
    proto = job_to_proto(_row(attempts=2, max_attempts=5))
    assert proto.id == "job-1"
    assert proto.attempts == 2
    assert proto.max_attempts == 5
    assert proto.type == service_pb2.JOB_TYPE_AGENT_EXECUTION


@pytest.mark.parametrize(
    ("domain", "proto"),
    [
        ("queued", service_pb2.JOB_STATUS_QUEUED),
        ("running", service_pb2.JOB_STATUS_RUNNING),
        ("succeeded", service_pb2.JOB_STATUS_SUCCEEDED),
        ("failed", service_pb2.JOB_STATUS_FAILED),
        ("dead", service_pb2.JOB_STATUS_DEAD),
        ("cancelled", service_pb2.JOB_STATUS_CANCELLED),
    ],
)
def test_status_mapping(domain: str, proto: int) -> None:
    assert job_to_proto(_row(status=domain)).status == proto


def test_job_to_proto_carries_spec_and_timestamps() -> None:
    proto = job_to_proto(_row())
    assert proto.spec.agent_execution_spec.name == "demo"
    assert proto.metadata.created_at.seconds > 0
    assert proto.metadata.updated_at.seconds > 0


def test_spec_round_trips() -> None:
    data = {"agent_execution_spec": {"name": "n", "instructions": "do it"}}
    proto = spec_from_dict(data)
    assert spec_to_dict(proto) == data


def test_spec_to_dict_empty() -> None:
    assert spec_to_dict(service_pb2.JobSpec()) == {}


# --- progress mapping ------------------------------------------------------


def test_progress_to_proto_empty() -> None:
    proto = progress_to_proto(None)
    assert proto.phase == service_pb2.JOB_PHASE_UNSPECIFIED
    assert list(proto.plan) == []
    assert list(proto.steps) == []


def test_progress_to_proto_orders_steps_by_index() -> None:
    progress = {
        "phase": "executing",
        "plan": ["a", "b", "c"],
        # deliberately out of order and sparse to prove index-based ordering
        "steps": {
            "1": {"prompt": "b", "output": "ob", "finish_reason": "stop"},
            "0": {"prompt": "a", "output": "oa", "finish_reason": "stop"},
        },
    }
    proto = progress_to_proto(progress)
    assert proto.phase == service_pb2.JOB_PHASE_EXECUTING
    assert list(proto.plan) == ["a", "b", "c"]
    assert [(s.index, s.prompt, s.output) for s in proto.steps] == [
        (0, "a", "oa"),
        (1, "b", "ob"),
    ]
    assert all(s.finish_reason == service_pb2.FINISH_REASON_STOP for s in proto.steps)


def test_progress_to_proto_maps_finish_reasons() -> None:
    progress = {
        "phase": "executing",
        "plan": ["a", "b"],
        "steps": {
            "0": {"prompt": "a", "output": "oa", "finish_reason": "requires_approval"},
            "1": {"prompt": "b", "output": "ob", "finish_reason": "unknown"},
        },
    }
    proto = progress_to_proto(progress)
    assert proto.steps[0].finish_reason == service_pb2.FINISH_REASON_REQUIRES_APPROVAL
    # An unrecognized reason falls back to the enum's zero value.
    assert proto.steps[1].finish_reason == service_pb2.FINISH_REASON_UNSPECIFIED


def test_job_to_proto_carries_progress() -> None:
    row = _row()
    row.progress = {"phase": "completed", "plan": ["a"], "steps": {"0": {"output": "done"}}}
    proto = job_to_proto(row)
    assert proto.progress.phase == service_pb2.JOB_PHASE_COMPLETED
    assert list(proto.progress.plan) == ["a"]
    assert proto.progress.steps[0].output == "done"
