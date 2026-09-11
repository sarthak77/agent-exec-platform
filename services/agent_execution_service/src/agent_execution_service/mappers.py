"""ORM row <-> protobuf conversions, isolated here so the servicer stays a
thin RPC adapter.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from google.protobuf.timestamp_pb2 import Timestamp

from aep.agent_execution.v1 import service_pb2
from agent_execution_service.models import AgentRow, TaskRow, ToolRow

if TYPE_CHECKING:
    # Imported for typing only, so this module stays a pure proto converter and
    # takes no runtime dependency on the services layer that builds the view.
    from agent_execution_service.services.tasks import TaskProgressView

_TASK_STATUS_TO_PROTO = {
    "pending": service_pb2.TASK_STATUS_PENDING,
    "running": service_pb2.TASK_STATUS_RUNNING,
    "waiting_approval": service_pb2.TASK_STATUS_WAITING_APPROVAL,
    "completed": service_pb2.TASK_STATUS_COMPLETED,
    "failed": service_pb2.TASK_STATUS_FAILED,
}


def dt_to_ts(dt: datetime) -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


def agent_to_proto(row: AgentRow, tool_ids: list[str]) -> service_pb2.Agent:
    return service_pb2.Agent(
        id=row.id,
        name=row.name,
        instructions=row.instructions,
        llm_config=service_pb2.LLMConfig(
            name=row.llm_config_name,
            temperature=row.llm_config_temperature,
        ),
        tool_config=service_pb2.ToolConfig(ids=tool_ids),
        metadata=service_pb2.AgentMetadata(
            version=row.version,
            created_at=dt_to_ts(row.created_at),
            updated_at=dt_to_ts(row.updated_at),
        ),
    )


def tool_to_proto(row: ToolRow) -> service_pb2.Tool:
    kwargs = dict(
        id=row.id,
        name=row.name,
        mutating=row.mutating,
        metadata=service_pb2.ToolMetadata(
            version=row.version,
            created_at=dt_to_ts(row.created_at),
            updated_at=dt_to_ts(row.updated_at),
        ),
    )
    if row.description is not None:
        kwargs["description"] = row.description
    return service_pb2.Tool(**kwargs)


def task_to_proto(row: TaskRow) -> service_pb2.Task:
    kwargs = dict(
        id=row.id,
        input=row.input,
        job_id=row.job_id,
        status=_TASK_STATUS_TO_PROTO.get(row.status, service_pb2.TASK_STATUS_UNSPECIFIED),
        metadata=service_pb2.TaskMetadata(
            created_at=dt_to_ts(row.created_at),
            updated_at=dt_to_ts(row.updated_at),
        ),
    )
    # Leave `result` unset until the backing job has produced an output, so a
    # client can tell "not done yet" from an empty answer.
    if row.result:
        kwargs["result"] = service_pb2.TaskResult(output=row.result)
    return service_pb2.Task(**kwargs)


def task_progress_to_proto(view: TaskProgressView) -> service_pb2.TaskProgress:
    return service_pb2.TaskProgress(
        task_id=view.task_id,
        status=_TASK_STATUS_TO_PROTO.get(view.status, service_pb2.TASK_STATUS_UNSPECIFIED),
        percent_complete=view.percent_complete,
        steps_completed=view.steps_completed,
        steps_total=view.steps_total,
        requires_approval=view.requires_approval,
        summary=view.summary,
        steps=[
            service_pb2.TaskProgressStep(
                index=step.index,
                description=step.description,
                output=step.output,
                agent=step.agent,
                requires_approval=step.requires_approval,
            )
            for step in view.steps
        ],
        output=view.output,
        error=view.error,
        updated_at=dt_to_ts(view.updated_at),
    )
