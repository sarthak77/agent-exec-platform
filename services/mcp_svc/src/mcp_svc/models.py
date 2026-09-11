"""SQLAlchemy model for the `tools` table.

mcp_svc only READS this table; agent_execution_service owns writes and the
DDL bootstrap. This maps only the columns mcp_svc actually uses — id (the
PK SQLAlchemy requires), tenant_id (every query is scoped by it), and the
name/description surfaced as MCP tools — with the same names and types the
writer's model declares. Write-side details (column defaults, the
tenant/name unique constraint, timezone-aware timestamps) live in the
writer, since nothing here creates DDL.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ToolRow(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    description: Mapped[str | None]


