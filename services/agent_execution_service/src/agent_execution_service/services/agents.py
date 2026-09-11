"""AgentService — create/get/update/delete on the `agents` resource,
backed by Postgres.

DeleteAgent is now a hard delete: the proto dropped the AgentStatus /
archived concept, and Task no longer references agent_id at all, so
nothing is left to orphan by removing the row outright (agent_tools links
cascade via the FK).
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_execution_service.errors import NotFoundError
from agent_execution_service.models import AgentRow, AgentToolRow, ToolRow, new_id, now


class AgentService:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        instructions: str,
        llm_config_name: str,
        llm_config_temperature: float,
        tool_ids: list[str],
    ) -> tuple[AgentRow, list[str]]:
        row = AgentRow(
            id=new_id(),
            tenant_id=tenant_id,
            name=name,
            instructions=instructions,
            llm_config_name=llm_config_name,
            llm_config_temperature=llm_config_temperature,
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
            linked = await _link_tools(session, tenant_id, row.id, tool_ids)
        return row, linked

    async def get(self, *, tenant_id: str, ids: list[str]) -> list[tuple[AgentRow, list[str]]]:
        async with self._sessions() as session:
            stmt = select(AgentRow).where(AgentRow.tenant_id == tenant_id)
            if ids:
                stmt = stmt.where(AgentRow.id.in_(ids))
            rows = list((await session.scalars(stmt)).all())
            if not rows:
                return []

            links = await session.execute(
                select(AgentToolRow.agent_id, AgentToolRow.tool_id).where(
                    AgentToolRow.agent_id.in_([row.id for row in rows])
                )
            )
            tool_ids_by_agent: dict[str, list[str]] = defaultdict(list)
            for agent_id, tool_id in links:
                tool_ids_by_agent[agent_id].append(tool_id)
            return [(row, tool_ids_by_agent.get(row.id, [])) for row in rows]

    async def has_any(self, *, tenant_id: str) -> bool:
        """Whether the tenant has at least one agent configured. A task has
        nothing to run against until an agent exists, so task submission is
        gated on this. Uses a LIMIT 1 existence probe rather than loading rows.
        """
        async with self._sessions() as session:
            first = await session.scalar(
                select(AgentRow.id).where(AgentRow.tenant_id == tenant_id).limit(1)
            )
        return first is not None

    async def update(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        name: str,
        instructions: str,
        llm_config_name: str,
        llm_config_temperature: float,
        tool_ids: list[str],
    ) -> tuple[AgentRow, list[str]]:
        async with self._sessions.begin() as session:
            row = await session.get(AgentRow, agent_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"agent {agent_id} not found")
            row.name = name
            row.instructions = instructions
            row.llm_config_name = llm_config_name
            row.llm_config_temperature = llm_config_temperature
            row.version += 1
            row.updated_at = now()
            await session.execute(delete(AgentToolRow).where(AgentToolRow.agent_id == agent_id))
            linked = await _link_tools(session, tenant_id, agent_id, tool_ids)
        return row, linked

    async def delete(self, *, tenant_id: str, agent_id: str) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(AgentRow, agent_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"agent {agent_id} not found")
            await session.delete(row)


async def _link_tools(session, tenant_id: str, agent_id: str, tool_ids: list[str]) -> list[str]:
    """Silently drops any id that isn't a real tool in this tenant, matching
    the Get-by-Filter convention of omitting unknown ids rather than erroring.
    """
    if not tool_ids:
        return []
    valid_ids = list(
        (
            await session.scalars(
                select(ToolRow.id).where(ToolRow.tenant_id == tenant_id, ToolRow.id.in_(tool_ids))
            )
        ).all()
    )
    for tool_id in valid_ids:
        session.add(AgentToolRow(agent_id=agent_id, tool_id=tool_id))
    return valid_ids
