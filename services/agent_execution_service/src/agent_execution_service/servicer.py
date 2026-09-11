"""gRPC servicer: thin adapter between AgentExecutionServiceServicer's
generated methods and the services/* business logic. Extracts the caller's
tenant id from metadata, converts ORM rows <-> protobuf via mappers, and
maps typed errors to gRPC status codes.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import grpc
from sqlalchemy.ext.asyncio import async_sessionmaker

from aep.agent_execution.v1 import service_pb2, service_pb2_grpc
from agent_execution_service.auth import tenant_id_from_metadata
from agent_execution_service.errors import (
    AppError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)
from agent_execution_service.job_client import JobGateway
from agent_execution_service.mappers import (
    agent_to_proto,
    task_progress_to_proto,
    task_to_proto,
    tool_to_proto,
)
from agent_execution_service.services.agents import AgentService
from agent_execution_service.services.tasks import TaskService
from agent_execution_service.services.tools import ToolService
from agent_execution_service.validators import (
    validate_agent_id,
    validate_agent_tool_ids,
    validate_filter_ids,
    validate_instructions,
    validate_name,
    validate_task_id,
    validate_task_input,
    validate_tool_id,
)

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: dict[type[Exception], grpc.StatusCode] = {
    NotFoundError: grpc.StatusCode.NOT_FOUND,
    ValidationError: grpc.StatusCode.INVALID_ARGUMENT,
    AuthenticationError: grpc.StatusCode.UNAUTHENTICATED,
    ConflictError: grpc.StatusCode.ALREADY_EXISTS,
    StateError: grpc.StatusCode.FAILED_PRECONDITION,
}

P = ParamSpec("P")
T = TypeVar("T")


def _handle_errors(
    fn: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    @functools.wraps(fn)
    async def wrapper(self, request, context: grpc.aio.ServicerContext):  # noqa: ANN001
        try:
            return await fn(self, request, context)
        except AppError as exc:
            code = _STATUS_BY_ERROR.get(type(exc), grpc.StatusCode.INTERNAL)
            await context.abort(code, str(exc))
        except Exception:
            logger.exception("unhandled error in %s", fn.__name__)
            await context.abort(grpc.StatusCode.INTERNAL, "internal error")

    return wrapper


class AgentExecutionServicer(service_pb2_grpc.AgentExecutionServiceServicer):
    def __init__(self, sessions: async_sessionmaker, jobs: JobGateway) -> None:
        self._agents = AgentService(sessions)
        self._tools = ToolService(sessions)
        self._tasks = TaskService(sessions, jobs)

    @_handle_errors
    async def CreateAgent(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_name(request.name)
        validate_instructions(request.instructions)
        tool_ids = list(request.tool_config.ids)
        validate_agent_tool_ids(tool_ids)
        row, tool_ids = await self._agents.create(
            tenant_id=tenant_id,
            name=request.name,
            instructions=request.instructions,
            llm_config_name=request.llm_config.name,
            llm_config_temperature=request.llm_config.temperature,
            tool_ids=tool_ids,
        )
        return service_pb2.CreateAgentResponse(agent=agent_to_proto(row, tool_ids))

    @_handle_errors
    async def GetAgent(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        ids = list(request.filter.ids)
        validate_filter_ids(ids)
        results = await self._agents.get(tenant_id=tenant_id, ids=ids)
        return service_pb2.GetAgentResponse(
            agents=[agent_to_proto(row, tool_ids) for row, tool_ids in results]
        )

    @_handle_errors
    async def UpdateAgent(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_agent_id(request.id)
        validate_name(request.name)
        validate_instructions(request.instructions)
        tool_ids = list(request.tool_config.ids)
        validate_agent_tool_ids(tool_ids)
        row, tool_ids = await self._agents.update(
            tenant_id=tenant_id,
            agent_id=request.id,
            name=request.name,
            instructions=request.instructions,
            llm_config_name=request.llm_config.name,
            llm_config_temperature=request.llm_config.temperature,
            tool_ids=tool_ids,
        )
        return service_pb2.UpdateAgentResponse(agent=agent_to_proto(row, tool_ids))

    @_handle_errors
    async def DeleteAgent(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_agent_id(request.id)
        await self._agents.delete(tenant_id=tenant_id, agent_id=request.id)
        return service_pb2.DeleteAgentResponse(success=True)

    @_handle_errors
    async def CreateTool(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_name(request.name)
        row = await self._tools.create(
            tenant_id=tenant_id,
            name=request.name,
            description=request.description if request.HasField("description") else None,
            mutating=request.mutating,
        )
        return service_pb2.CreateToolResponse(tool=tool_to_proto(row))

    @_handle_errors
    async def GetTool(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        ids = list(request.filter.ids)
        validate_filter_ids(ids)
        rows = await self._tools.get(tenant_id=tenant_id, ids=ids)
        return service_pb2.GetToolResponse(tools=[tool_to_proto(row) for row in rows])

    @_handle_errors
    async def UpdateTool(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_tool_id(request.id)
        validate_name(request.name)
        row = await self._tools.update(
            tenant_id=tenant_id,
            tool_id=request.id,
            name=request.name,
            description=request.description,
            mutating=request.mutating,
        )
        return service_pb2.UpdateToolResponse(tool=tool_to_proto(row))

    @_handle_errors
    async def DeleteTool(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_tool_id(request.id)
        await self._tools.delete(tenant_id=tenant_id, tool_id=request.id)
        return service_pb2.DeleteToolResponse(success=True)

    @_handle_errors
    async def CreateTask(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_task_input(request.input)
        # A task has nothing to run against until the tenant has configured at
        # least one agent, so reject submission outright (FAILED_PRECONDITION)
        # rather than creating a job in job_svc that could never be executed.
        if not await self._agents.has_any(tenant_id=tenant_id):
            raise StateError("cannot submit a task: no agents are configured for this tenant")
        row = await self._tasks.create(tenant_id=tenant_id, input=request.input)
        return service_pb2.CreateTaskResponse(task=task_to_proto(row))

    @_handle_errors
    async def GetTask(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        ids = list(request.filter.ids)
        validate_filter_ids(ids)
        rows = await self._tasks.get(tenant_id=tenant_id, ids=ids)
        return service_pb2.GetTaskResponse(tasks=[task_to_proto(row) for row in rows])

    @_handle_errors
    async def ApproveTask(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_task_id(request.task_id)
        await self._tasks.approve(tenant_id=tenant_id, task_id=request.task_id)
        return service_pb2.ApproveTaskResponse(success=True)

    @_handle_errors
    async def RetryTask(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_task_id(request.task_id)
        await self._tasks.retry(tenant_id=tenant_id, task_id=request.task_id)
        return service_pb2.RetryTaskResponse(success=True)

    @_handle_errors
    async def GetTaskProgress(self, request, context):
        tenant_id = tenant_id_from_metadata(context)
        validate_task_id(request.task_id)
        view = await self._tasks.get_progress(tenant_id=tenant_id, task_id=request.task_id)
        return service_pb2.GetTaskProgressResponse(progress=task_progress_to_proto(view))
