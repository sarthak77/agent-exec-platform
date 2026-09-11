import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TaskStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TASK_STATUS_UNSPECIFIED: _ClassVar[TaskStatus]
    TASK_STATUS_PENDING: _ClassVar[TaskStatus]
    TASK_STATUS_RUNNING: _ClassVar[TaskStatus]
    TASK_STATUS_WAITING_APPROVAL: _ClassVar[TaskStatus]
    TASK_STATUS_COMPLETED: _ClassVar[TaskStatus]
    TASK_STATUS_FAILED: _ClassVar[TaskStatus]
TASK_STATUS_UNSPECIFIED: TaskStatus
TASK_STATUS_PENDING: TaskStatus
TASK_STATUS_RUNNING: TaskStatus
TASK_STATUS_WAITING_APPROVAL: TaskStatus
TASK_STATUS_COMPLETED: TaskStatus
TASK_STATUS_FAILED: TaskStatus

class CreateToolRequest(_message.Message):
    __slots__ = ("name", "description", "mutating")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    MUTATING_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    mutating: bool
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., mutating: _Optional[bool] = ...) -> None: ...

class CreateToolResponse(_message.Message):
    __slots__ = ("tool",)
    TOOL_FIELD_NUMBER: _ClassVar[int]
    tool: Tool
    def __init__(self, tool: _Optional[_Union[Tool, _Mapping]] = ...) -> None: ...

class GetToolRequest(_message.Message):
    __slots__ = ("filter",)
    FILTER_FIELD_NUMBER: _ClassVar[int]
    filter: GetToolRequestFilter
    def __init__(self, filter: _Optional[_Union[GetToolRequestFilter, _Mapping]] = ...) -> None: ...

class GetToolRequestFilter(_message.Message):
    __slots__ = ("ids",)
    IDS_FIELD_NUMBER: _ClassVar[int]
    ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GetToolResponse(_message.Message):
    __slots__ = ("tools",)
    TOOLS_FIELD_NUMBER: _ClassVar[int]
    tools: _containers.RepeatedCompositeFieldContainer[Tool]
    def __init__(self, tools: _Optional[_Iterable[_Union[Tool, _Mapping]]] = ...) -> None: ...

class UpdateToolRequest(_message.Message):
    __slots__ = ("id", "name", "description", "mutating")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    MUTATING_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    description: str
    mutating: bool
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., mutating: _Optional[bool] = ...) -> None: ...

class UpdateToolResponse(_message.Message):
    __slots__ = ("tool",)
    TOOL_FIELD_NUMBER: _ClassVar[int]
    tool: Tool
    def __init__(self, tool: _Optional[_Union[Tool, _Mapping]] = ...) -> None: ...

class DeleteToolRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class DeleteToolResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: _Optional[bool] = ...) -> None: ...

class CreateAgentRequest(_message.Message):
    __slots__ = ("name", "instructions", "llm_config", "tool_config")
    NAME_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    LLM_CONFIG_FIELD_NUMBER: _ClassVar[int]
    TOOL_CONFIG_FIELD_NUMBER: _ClassVar[int]
    name: str
    instructions: str
    llm_config: LLMConfig
    tool_config: ToolConfig
    def __init__(self, name: _Optional[str] = ..., instructions: _Optional[str] = ..., llm_config: _Optional[_Union[LLMConfig, _Mapping]] = ..., tool_config: _Optional[_Union[ToolConfig, _Mapping]] = ...) -> None: ...

class CreateAgentResponse(_message.Message):
    __slots__ = ("agent",)
    AGENT_FIELD_NUMBER: _ClassVar[int]
    agent: Agent
    def __init__(self, agent: _Optional[_Union[Agent, _Mapping]] = ...) -> None: ...

class GetAgentRequest(_message.Message):
    __slots__ = ("filter",)
    FILTER_FIELD_NUMBER: _ClassVar[int]
    filter: GetAgentRequestFilter
    def __init__(self, filter: _Optional[_Union[GetAgentRequestFilter, _Mapping]] = ...) -> None: ...

class GetAgentRequestFilter(_message.Message):
    __slots__ = ("ids",)
    IDS_FIELD_NUMBER: _ClassVar[int]
    ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GetAgentResponse(_message.Message):
    __slots__ = ("agents",)
    AGENTS_FIELD_NUMBER: _ClassVar[int]
    agents: _containers.RepeatedCompositeFieldContainer[Agent]
    def __init__(self, agents: _Optional[_Iterable[_Union[Agent, _Mapping]]] = ...) -> None: ...

class UpdateAgentRequest(_message.Message):
    __slots__ = ("id", "name", "instructions", "llm_config", "tool_config")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    LLM_CONFIG_FIELD_NUMBER: _ClassVar[int]
    TOOL_CONFIG_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    instructions: str
    llm_config: LLMConfig
    tool_config: ToolConfig
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., instructions: _Optional[str] = ..., llm_config: _Optional[_Union[LLMConfig, _Mapping]] = ..., tool_config: _Optional[_Union[ToolConfig, _Mapping]] = ...) -> None: ...

class UpdateAgentResponse(_message.Message):
    __slots__ = ("agent",)
    AGENT_FIELD_NUMBER: _ClassVar[int]
    agent: Agent
    def __init__(self, agent: _Optional[_Union[Agent, _Mapping]] = ...) -> None: ...

class DeleteAgentRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class DeleteAgentResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: _Optional[bool] = ...) -> None: ...

class CreateTaskRequest(_message.Message):
    __slots__ = ("input",)
    INPUT_FIELD_NUMBER: _ClassVar[int]
    input: str
    def __init__(self, input: _Optional[str] = ...) -> None: ...

class CreateTaskResponse(_message.Message):
    __slots__ = ("task",)
    TASK_FIELD_NUMBER: _ClassVar[int]
    task: Task
    def __init__(self, task: _Optional[_Union[Task, _Mapping]] = ...) -> None: ...

class GetTaskRequest(_message.Message):
    __slots__ = ("filter",)
    FILTER_FIELD_NUMBER: _ClassVar[int]
    filter: GetTaskRequestFilter
    def __init__(self, filter: _Optional[_Union[GetTaskRequestFilter, _Mapping]] = ...) -> None: ...

class GetTaskRequestFilter(_message.Message):
    __slots__ = ("ids",)
    IDS_FIELD_NUMBER: _ClassVar[int]
    ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GetTaskResponse(_message.Message):
    __slots__ = ("tasks",)
    TASKS_FIELD_NUMBER: _ClassVar[int]
    tasks: _containers.RepeatedCompositeFieldContainer[Task]
    def __init__(self, tasks: _Optional[_Iterable[_Union[Task, _Mapping]]] = ...) -> None: ...

class ApproveTaskRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class ApproveTaskResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: _Optional[bool] = ...) -> None: ...

class RetryTaskRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class RetryTaskResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: _Optional[bool] = ...) -> None: ...

class GetTaskProgressRequest(_message.Message):
    __slots__ = ("task_id",)
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    def __init__(self, task_id: _Optional[str] = ...) -> None: ...

class GetTaskProgressResponse(_message.Message):
    __slots__ = ("progress",)
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    progress: TaskProgress
    def __init__(self, progress: _Optional[_Union[TaskProgress, _Mapping]] = ...) -> None: ...

class LLMConfig(_message.Message):
    __slots__ = ("name", "temperature")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    name: str
    temperature: float
    def __init__(self, name: _Optional[str] = ..., temperature: _Optional[float] = ...) -> None: ...

class ToolConfig(_message.Message):
    __slots__ = ("ids",)
    IDS_FIELD_NUMBER: _ClassVar[int]
    ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ids: _Optional[_Iterable[str]] = ...) -> None: ...

class Agent(_message.Message):
    __slots__ = ("id", "name", "instructions", "llm_config", "tool_config", "metadata")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    INSTRUCTIONS_FIELD_NUMBER: _ClassVar[int]
    LLM_CONFIG_FIELD_NUMBER: _ClassVar[int]
    TOOL_CONFIG_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    instructions: str
    llm_config: LLMConfig
    tool_config: ToolConfig
    metadata: AgentMetadata
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., instructions: _Optional[str] = ..., llm_config: _Optional[_Union[LLMConfig, _Mapping]] = ..., tool_config: _Optional[_Union[ToolConfig, _Mapping]] = ..., metadata: _Optional[_Union[AgentMetadata, _Mapping]] = ...) -> None: ...

class AgentMetadata(_message.Message):
    __slots__ = ("version", "created_at", "updated_at")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    version: int
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, version: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class Tool(_message.Message):
    __slots__ = ("id", "name", "description", "metadata", "mutating")
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    MUTATING_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    description: str
    metadata: ToolMetadata
    mutating: bool
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., description: _Optional[str] = ..., metadata: _Optional[_Union[ToolMetadata, _Mapping]] = ..., mutating: _Optional[bool] = ...) -> None: ...

class ToolMetadata(_message.Message):
    __slots__ = ("version", "created_at", "updated_at")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    version: int
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, version: _Optional[int] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class Task(_message.Message):
    __slots__ = ("id", "input", "status", "metadata", "job_id", "result")
    ID_FIELD_NUMBER: _ClassVar[int]
    INPUT_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    id: str
    input: str
    status: TaskStatus
    metadata: TaskMetadata
    job_id: str
    result: TaskResult
    def __init__(self, id: _Optional[str] = ..., input: _Optional[str] = ..., status: _Optional[_Union[TaskStatus, str]] = ..., metadata: _Optional[_Union[TaskMetadata, _Mapping]] = ..., job_id: _Optional[str] = ..., result: _Optional[_Union[TaskResult, _Mapping]] = ...) -> None: ...

class TaskResult(_message.Message):
    __slots__ = ("output",)
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    output: str
    def __init__(self, output: _Optional[str] = ...) -> None: ...

class TaskProgress(_message.Message):
    __slots__ = ("task_id", "status", "percent_complete", "steps_completed", "steps_total", "requires_approval", "summary", "steps", "output", "error", "updated_at")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    PERCENT_COMPLETE_FIELD_NUMBER: _ClassVar[int]
    STEPS_COMPLETED_FIELD_NUMBER: _ClassVar[int]
    STEPS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_APPROVAL_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    status: TaskStatus
    percent_complete: float
    steps_completed: int
    steps_total: int
    requires_approval: bool
    summary: str
    steps: _containers.RepeatedCompositeFieldContainer[TaskProgressStep]
    output: str
    error: str
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, task_id: _Optional[str] = ..., status: _Optional[_Union[TaskStatus, str]] = ..., percent_complete: _Optional[float] = ..., steps_completed: _Optional[int] = ..., steps_total: _Optional[int] = ..., requires_approval: _Optional[bool] = ..., summary: _Optional[str] = ..., steps: _Optional[_Iterable[_Union[TaskProgressStep, _Mapping]]] = ..., output: _Optional[str] = ..., error: _Optional[str] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class TaskProgressStep(_message.Message):
    __slots__ = ("index", "description", "output", "agent", "requires_approval")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    REQUIRES_APPROVAL_FIELD_NUMBER: _ClassVar[int]
    index: int
    description: str
    output: str
    agent: str
    requires_approval: bool
    def __init__(self, index: _Optional[int] = ..., description: _Optional[str] = ..., output: _Optional[str] = ..., agent: _Optional[str] = ..., requires_approval: _Optional[bool] = ...) -> None: ...

class TaskMetadata(_message.Message):
    __slots__ = ("created_at", "updated_at")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
