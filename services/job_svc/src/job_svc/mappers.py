"""ORM row <-> protobuf conversions, isolated here so the servicer stays a
thin RPC adapter and the services layer stays protobuf-agnostic.
"""

from __future__ import annotations

from datetime import datetime

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.timestamp_pb2 import Timestamp

from aep.job.v1 import service_pb2
from job_svc.models import JobRow

_STATUS_TO_PROTO = {
    "queued": service_pb2.JOB_STATUS_QUEUED,
    "running": service_pb2.JOB_STATUS_RUNNING,
    "succeeded": service_pb2.JOB_STATUS_SUCCEEDED,
    "failed": service_pb2.JOB_STATUS_FAILED,
    "dead": service_pb2.JOB_STATUS_DEAD,
    "cancelled": service_pb2.JOB_STATUS_CANCELLED,
    "waiting_approval": service_pb2.JOB_STATUS_WAITING_APPROVAL,
}
STATUS_FROM_PROTO = {v: k for k, v in _STATUS_TO_PROTO.items()}

_TYPE_TO_PROTO = {
    "agent_execution": service_pb2.JOB_TYPE_AGENT_EXECUTION,
}
TYPE_FROM_PROTO = {v: k for k, v in _TYPE_TO_PROTO.items()}

# The runner records these as lowercase strings in the JSON progress blob (see
# runner.py); surface them as the typed proto enums. Any value not listed --
# including "" for an unplanned job or a finish_reason the runner did not set --
# maps to the enum's UNSPECIFIED zero value.
_PHASE_TO_PROTO = {
    "planning": service_pb2.JOB_PHASE_PLANNING,
    "executing": service_pb2.JOB_PHASE_EXECUTING,
    "completed": service_pb2.JOB_PHASE_COMPLETED,
}

_FINISH_REASON_TO_PROTO = {
    "stop": service_pb2.FINISH_REASON_STOP,
    "requires_approval": service_pb2.FINISH_REASON_REQUIRES_APPROVAL,
}


def dt_to_ts(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def spec_to_dict(spec: service_pb2.JobSpec) -> dict:
    return MessageToDict(spec, preserving_proto_field_name=True)


def spec_from_dict(data: dict) -> service_pb2.JobSpec:
    return ParseDict(data, service_pb2.JobSpec(), ignore_unknown_fields=True)


def progress_to_proto(progress: dict | None) -> service_pb2.JobProgress:
    # The runner stores steps as a dict keyed by the string step index (see
    # runner.py); surface them as a repeated field ordered by that index.
    progress = progress or {}
    steps_by_index = progress.get("steps") or {}
    steps = [
        service_pb2.JobStep(
            index=int(key),
            prompt=step.get("prompt", ""),
            output=step.get("output", ""),
            finish_reason=_FINISH_REASON_TO_PROTO.get(
                step.get("finish_reason", ""), service_pb2.FINISH_REASON_UNSPECIFIED
            ),
            agent=step.get("agent", ""),
        )
        for key, step in sorted(steps_by_index.items(), key=lambda kv: int(kv[0]))
    ]
    return service_pb2.JobProgress(
        phase=_PHASE_TO_PROTO.get(progress.get("phase", ""), service_pb2.JOB_PHASE_UNSPECIFIED),
        plan=list(progress.get("plan") or []),
        steps=steps,
        error=progress.get("error", ""),
        result=progress.get("result", ""),
    )


def job_to_proto(row: JobRow) -> service_pb2.Job:
    return service_pb2.Job(
        id=row.id,
        type=_TYPE_TO_PROTO[row.type],
        spec=spec_from_dict(row.spec),
        status=_STATUS_TO_PROTO[row.status],
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        retry_count=row.retry_count,
        max_retries=row.max_retries,
        metadata=service_pb2.JobMetadata(
            created_at=dt_to_ts(row.created_at),
            updated_at=dt_to_ts(row.updated_at),
        ),
        progress=progress_to_proto(row.progress),
    )
