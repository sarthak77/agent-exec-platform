"""Read-only queries resolving a tenant's agents plus their granted tool
names, for group-chat construction (see groupchat.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from orchestrator.models import AgentRow, ToolRow


@dataclass(frozen=True, slots=True)
class AgentSpec:
    id: str
    name: str
    instructions: str
    # Carried for completeness; the gateway is single-model, so model
    # selection by name is not honored yet (only temperature is passed through).
    llm_config_name: str
    llm_config_temperature: float
    tool_names: list[str]


async def list_agents_for_tenant(session: AsyncSession, tenant_id: str) -> list[AgentSpec]:
    stmt = (
        select(AgentRow)
        .where(AgentRow.tenant_id == tenant_id)
        .options(selectinload(AgentRow.tool_links))
        .order_by(AgentRow.name)
    )
    rows = list((await session.scalars(stmt)).all())

    tool_ids = {link.tool_id for row in rows for link in row.tool_links}
    tool_names: dict[str, str] = {}
    if tool_ids:
        # tenant_id is redundant given the ids come from this tenant's own
        # agents, but scoping the query is cheap defense-in-depth.
        tools_stmt = select(ToolRow).where(
            ToolRow.tenant_id == tenant_id, ToolRow.id.in_(tool_ids)
        )
        tool_names = {t.id: t.name for t in (await session.scalars(tools_stmt)).all()}

    return [
        AgentSpec(
            id=row.id,
            name=row.name,
            instructions=row.instructions,
            llm_config_name=row.llm_config_name,
            llm_config_temperature=row.llm_config_temperature,
            tool_names=[tool_names[link.tool_id] for link in row.tool_links if link.tool_id in tool_names],
        )
        for row in rows
    ]
