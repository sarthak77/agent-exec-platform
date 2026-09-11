import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class JobStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JOB_STATUS_UNSPECIFIED: _ClassVar[JobStatus]
    JOB_STATUS_QUEUED: _ClassVar[JobStatus]
    JOB_STATUS_RUNNING: _ClassVar[JobStatus]
    JOB_STATUS_SUCCEEDED: _ClassVar[JobStatus]
    JOB_STATUS_FAILED: _ClassVar[JobStatus]
    JOB_STATUS_DEAD: _ClassVar[JobStatus]
    JOB_STATUS_CANCELLED: _ClassVar[JobStatus]
    JOB_STATUS_WAITING_APPROVAL: _ClassVar[JobStatus]

class JobPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JOB_PHASE_UNSPECIFIED: _ClassVar[JobPhase]
    JOB_PHASE_PLANNING: _ClassVar[JobPhase]
    JOB_PHASE_EXECUTING: _ClassVar[JobPhase]
    JOB_PHASE_COMPLETED: _ClassVar[JobPhase]

class FinishReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FINISH_REASON_UNSPECIFIED: _ClassVar[FinishReason]
    FINISH_REASON_STOP: _ClassVar[FinishReason]
    FINISH_REASON_REQUIRES_APPROVAL: _ClassVar[FinishReason]

class JobType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    JOB_TYPE_UNSPECIFIED: _ClassVar[JobType]
    JOB_TYPE_AGENT_EXECUTION: _ClassVar[JobType]
JOB_STATUS_UNSPECIFIED: JobStatus
JOB_STATUS_QUEUED: JobStatus
JOB_STATUS_RUNNING: JobStatus
JOB_STATUS_SUCCEEDED: JobStatus
JOB_STATUS_FAILED: JobStatus
JOB_STATUS_DEAD: JobStatus
JOB_STATUS_CANCELLED: JobStatus
JOB_STATUS_WAITING_APPROVAL: JobStatus
JOB_PHASE_UNSPECIFIED: JobPhase
JOB_PHASE_PLANNING: JobPhase
JOB_PHASE_EXECUTING: JobPhase
JOB_PHASE_COMPLETED: JobPhase
FINISH_REASON_UNSPECIFIED: FinishReason
FINISH_REASON_STOP: FinishReason
FINISH_REASON_REQUIRES_APPROVAL: FinishReason
JOB_TYPE_UNSPECIFIED: JobType
JOB_TYPE_AGENT_EXECUTION: JobType

class CreateJobRequest(_message.Message):
    __slots__ = ("type", "spec", "max_attempts", "max_retries")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    MAX_RETRIES_FIELD_NUMBER: _ClassVar[int]
    type: JobType
    spec: JobSpec
    max_attempts: int
    max_retries: int
    def __init__(self, type: _Optional[_Union[JobType, str]] = ..., spec: _Optional[_Union[JobSpec, _Mapping]] = ..., max_attempts: _Optional[int] = ..., max_retries: _Optional[int] = ...) -> None: ...

class CreateJobResponse(_message.Message):
    __slots__ = ("job",)
    JOB_FIELD_NUMBER: _ClassVar[int]
    job: Job
    def __init__(self, job: _Optional[_Union[Job, _Mapping]] = ...) -> None: ...

class GetJobRequest(_message.Message):
    __slots__ = ("filter",)
    FILTER_FIELD_NUMBER: _ClassVar[int]
    filter: GetJobRequestFilter
    def __init__(self, filter: _Optional[_Union[GetJobRequestFilter, _Mapping]] = ...) -> None: ...

class GetJobRequestFilter(_message.Message):
    __slots__ = ("ids", "statuses", "types")
    IDS_FIELD_NUMBER: _ClassVar[int]
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    TYPES_FIELD_NUMBER: _ClassVar[int]
    ids: _containers.RepeatedScalarFieldContainer[str]
    statuses: _containers.RepeatedScalarFieldContainer[JobStatus]
    types: _containers.RepeatedScalarFieldContainer[JobType]
    def __init__(self, ids: _Optional[_Iterable[str]] = ..., statuses: _Optional[_Iterable[_Union[JobStatus, str]]] = ..., types: _Optional[_Iterable[_Union[JobType, str]]] = ...) -> None: ...

class GetJobResponse(_message.Message):
    __slots__ = ("jobs",)
    JOBS_FIELD_NUMBER: _ClassVar[int]
    jobs: _containers.RepeatedCompositeFieldContainer[Job]
    def __init__(self, jobs: _Optional[_Iterable[_Union[Job, _Mapping]]] = ...) -> None: ...

class UpdateJobRequest(_message.Message):
    __slots__ = ("id", "status")
    ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    id: str
    status: JobStatus
    def __init__(self, id: _Optional[str] = ..., status: _Optional[_Union[JobStatus, str]] = ...) -> None: ...

class UpdateJobResponse(_message.Message):
    __slots__ = ("job",)
    JOB_FIELD_NUMBER: _ClassVar[int]
    job: Job
    def __init__(self, job: _Optional[_Union[Job, _Mapping]] = ...) -> None: ...

class RetryJobRequest(_message.Message):
    __slots__ = ("id",)
    ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    def __init__(self, id: _Optional[str] = ...) -> None: ...

class RetryJobResponse(_message.Message):
    __slots__ = ("job",)
    JOB_FIELD_NUMBER: _ClassVar[int]
    job: Job
    def __init__(self, job: _Optional[_Union[Job, _Mapping]] = ...) -> None: ...

class Job(_message.Message):
    __slots__ = ("id", "type", "spec", "status", "attempts", "max_attempts", "metadata", "progress", "retry_count", "max_retries")
    ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    MAX_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    RETRY_COUNT_FIELD_NUMBER: _ClassVar[int]
    MAX_RETRIES_FIELD_NUMBER: _ClassVar[int]
    id: str
    type: JobType
    spec: JobSpec
    status: JobStatus
    attempts: int
    max_attempts: int
    metadata: JobMetadata
    progress: JobProgress
    retry_count: int
    max_retries: int
    def __init__(self, id: _Optional[str] = ..., type: _Optional[_Union[JobType, str]] = ..., spec: _Optional[_Union[JobSpec, _Mapping]] = ..., status: _Optional[_Union[JobStatus, str]] = ..., attempts: _Optional[int] = ..., max_attempts: _Optional[int] = ..., metadata: _Optional[_Union[JobMetadata, _Mapping]] = ..., progress: _Optional[_Union[JobProgress, _Mapping]] = ..., retry_count: _Optional[int] = ..., max_retries: _Optional[int] = ...) -> None: ...

class JobProgress(_message.Message):
    __slots__ = ("phase", "plan", "steps", "error", "result")
    PHASE_FIELD_NUMBER: _ClassVar[int]
    PLAN_FIELD_NUMBER: _ClassVar[int]
    STEPS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    phase: JobPhase
    plan: _containers.RepeatedScalarFieldContainer[str]
    steps: _containers.RepeatedCompositeFieldContainer[JobStep]
    error: str
    result: str
    def __init__(self, phase: _Optional[_Union[JobPhase, str]] = ..., plan: _Optional[_Iterable[str]] = ..., steps: _Optional[_Iterable[_Union[JobStep, _Mapping]]] = ..., error: _Optional[str] = ..., result: _Optional[str] = ...) -> None: ...

class JobStep(_message.Message):
    __slots__ = ("index", "prompt", "output", "finish_reason", "agent")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    index: int
    prompt: str
    output: str
    finish_reason: FinishReason
    agent: str
    def __init__(self, index: _Optional[int] = ..., prompt: _Optional[str] = ..., output: _Optional[str] = ..., finish_reason: _Optional[_Union[FinishReason, str]] = ..., agent: _Optional[str] = ...) -> None: ...

class JobSpec(_message.Message):
    __slots__ = ("agent_execution_spec",)
    AGENT_EXECUTION_SPEC_FIELD_NUMBER: _ClassVar[int]
    agent_execution_spec: AgentExecutionSpec
    def __init__(self, agent_execution_spec: _Optional[_Union[AgentExecutionSpec, _Mapping]] = ...) -> None: ...

class AgentExecutionSpec(_message.Message):
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

class JobMetadata(_message.Message):
    __slots__ = ("created_at", "updated_at")
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    def __init__(self, created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...
