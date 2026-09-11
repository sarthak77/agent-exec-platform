"""SQLAlchemy async ORM models, persisted in Postgres (see db.py for the
engine/session setup and config.py for connection settings). Row classes are
named `*Row` to stay distinct from the generated protobuf message classes of
the same domain name (`Job` in service_pb2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Fallback retry budgets. The live defaults are config-driven
# (config.JobsSettings.default_max_attempts/default_max_retries, injected into
# JobService); these constants are only the last-resort defaults for the
# columns and an un-configured JobService.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_RETRIES = 3


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(index=True)
    type: Mapped[str] = mapped_column(index=True)
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="queued", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=DEFAULT_MAX_ATTEMPTS)
    # User/manual retry budget: consumed by RetryJob (not the automatic
    # attempts/max_attempts cycle), which resets `attempts` to 0 and increments
    # `retry_count` each time a caller retries a job resting at `failed`. Once
    # both budgets are exhausted the job is dead-lettered.
    retry_count: Mapped[int] = mapped_column(default=0)
    max_retries: Mapped[int] = mapped_column(default=DEFAULT_MAX_RETRIES)
    # Runner execution checkpoint (the job's "metadata" for resume/idempotency):
    # the decomposition plan and per-step outputs. Survives failed -> queued ->
    # running retries untouched, so a re-run resumes from the last completed step
    # rather than replanning or re-executing. See runner.py for the schema.
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    # Claim lease for crash recovery under horizontally-scaled pollers. Stamped
    # when a job is claimed (queued -> running); the reaper (JobService.
    # reap_expired, driven by JobPoller) requeues any running job whose lease has
    # expired -- i.e. the pod that claimed it died mid-run -- so it is retried
    # rather than stranded in `running` forever. `locked_by` records the claiming
    # worker for observability. Both are cleared when the job returns to `queued`.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    locked_by: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
