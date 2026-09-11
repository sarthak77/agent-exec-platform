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
    # Subset of tool_names whose tool is flagged `mutating` in the catalog --
    # every call to one of these must be approved by a human before it runs
    # (see AgentToolWorkbench).
    mutating_tool_names: frozenset[str]


async def list_agents_for_tenant(session: AsyncSession, tenant_id: str) -> list[AgentSpec]:
    stmt = (
        select(AgentRow)
        .where(AgentRow.tenant_id == tenant_id)
        .options(selectinload(AgentRow.tool_links))
        .order_by(AgentRow.name)
    )
    rows = list((await session.scalars(stmt)).all())

    tool_ids = {link.tool_id for row in rows for link in row.tool_links}
    tools_by_id: dict[str, ToolRow] = {}
    if tool_ids:
        # tenant_id is redundant given the ids come from this tenant's own
        # agents, but scoping the query is cheap defense-in-depth.
        tools_stmt = select(ToolRow).where(
            ToolRow.tenant_id == tenant_id, ToolRow.id.in_(tool_ids)
        )
        tools_by_id = {t.id: t for t in (await session.scalars(tools_stmt)).all()}

    specs = []
    for row in rows:
        tools = [tools_by_id[link.tool_id] for link in row.tool_links if link.tool_id in tools_by_id]
        specs.append(
            AgentSpec(
                id=row.id,
                name=row.name,
                instructions=row.instructions,
                llm_config_name=row.llm_config_name,
                llm_config_temperature=row.llm_config_temperature,
                tool_names=[t.name for t in tools],
                mutating_tool_names=frozenset(t.name for t in tools if t.mutating),
            )
        )
    return specs
