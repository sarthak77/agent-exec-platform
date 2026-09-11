"""SQLAlchemy async ORM models, persisted in Postgres (see db.py for the
engine/session setup and config.py for connection settings). Row classes are
named `*Row` to stay distinct from the generated protobuf message classes of
the same domain name (`Agent`, `Tool`, `Task` in service_pb2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AgentRow(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    instructions: Mapped[str]
    llm_config_name: Mapped[str]
    llm_config_temperature: Mapped[float]
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )

    tool_links: Mapped[list["AgentToolRow"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class ToolRow(Base):
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tools_tenant_id_name"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    description: Mapped[str | None]
    # If true, every call to this tool must be approved by a human before it
    # runs (see AgentToolWorkbench in orchestrator).
    mutating: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


class AgentToolRow(Base):
    """The agent<->tool grant backing `ToolConfig.ids` on Agent — mirrors the
    original `agent_tools` join table, minus the enabled/override columns
    that have no home in this basic impl's minimal ToolConfig shape.
    """

    __tablename__ = "agent_tools"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(
        ForeignKey("tools.id", ondelete="CASCADE"), primary_key=True
    )

    agent: Mapped[AgentRow] = relationship(back_populates="tool_links")


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(index=True)
    input: Mapped[str]
    # Id of the job in job_svc this task was submitted as (see services/tasks.py).
    # Every task corresponds to exactly one job.
    job_id: Mapped[str] = mapped_column(index=True)
    # A snapshot of the corresponding job's status, refreshed from the
    # authoritative Job on every mutating call (create/approve/retry) and, while
    # the task is non-terminal, on read too (see services/tasks.py). A terminal
    # task is served from this snapshot without a job_svc round-trip.
    status: Mapped[str] = mapped_column(default="pending")
    # Snapshot of the job's final output, refreshed alongside `status`. Empty
    # until the backing job has completed and produced a result.
    result: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )
