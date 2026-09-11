"""Read-only mirror of agent_execution_service's `agents`/`agent_tools`/
`tools` tables (see agent_execution_service/models.py — authoritative
schema owner). orchestrator never writes these tables or runs DDL.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    instructions: Mapped[str]
    llm_config_name: Mapped[str]
    llm_config_temperature: Mapped[float]
    version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    tool_links: Mapped[list["AgentToolRow"]] = relationship(back_populates="agent")


class ToolRow(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    description: Mapped[str | None]
    mutating: Mapped[bool]
    version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentToolRow(Base):
    __tablename__ = "agent_tools"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"), primary_key=True)

    agent: Mapped[AgentRow] = relationship(back_populates="tool_links")
