"""Read-only queries against the `tools` table."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_svc.models import ToolRow


async def list_tools_for_tenant(session: AsyncSession, tenant_id: str) -> list[ToolRow]:
    stmt = select(ToolRow).where(ToolRow.tenant_id == tenant_id).order_by(ToolRow.name)
    return list((await session.scalars(stmt)).all())


async def get_tool_by_name(session: AsyncSession, tenant_id: str, name: str) -> ToolRow | None:
    """Look up one tool by (tenant, name); None if the tenant has no such tool.

    The writer enforces a unique (tenant_id, name) constraint, so at most one
    row matches.
    """
    stmt = select(ToolRow).where(ToolRow.tenant_id == tenant_id, ToolRow.name == name)
    return (await session.scalars(stmt)).first()
