"""Shared test fixtures.

The suite runs against an in-memory SQLite database (via aiosqlite) so it needs
no live Postgres, and against a stateful in-process fake for job_svc so it needs
no live job_svc either. The fake reproduces job_svc's transition guards
(queued -> running, failed/dead -> queued) atomically, which is exactly the
contract TaskService relies on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agent_execution_service.errors import NotFoundError, StateError
from agent_execution_service.job_client import JobGateway, JobRef
from agent_execution_service.models import Base


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker]:
    # StaticPool keeps a single underlying connection alive, so the in-memory
    # DB persists across sessions for the lifetime of the test.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeJobGateway(JobGateway):
    """In-process stand-in for job_svc. Enforces the same transition guards, so
    TaskService's approve/retry error behaviour can be exercised without a real
    job service. `delay` inserts an await between read and write of a transition
    so concurrency tests can interleave callers on the single event loop.
    """

    def __init__(self, *, delay: float = 0.0) -> None:
        self._status: dict[str, str] = {}
        self._tenant: dict[str, str] = {}
        self.delay = delay
        self.created: list[str] = []

    async def create_job(self, *, tenant_id: str, input: str) -> JobRef:
        job_id = str(uuid.uuid4())
        self._status[job_id] = "queued"
        self._tenant[job_id] = tenant_id
        self.created.append(input)
        return JobRef(id=job_id, status="queued")

    async def start_job(self, *, tenant_id: str, job_id: str) -> JobRef:
        # Approve resumes a paused run: job_svc transitions waiting_approval ->
        # queued (the non-resetting requeue), and the poller re-runs it.
        return await self._transition(job_id, {"waiting_approval"}, "queued")

    async def retry_job(self, *, tenant_id: str, job_id: str) -> JobRef:
        return await self._transition(job_id, {"failed", "dead"}, "queued")

    async def _transition(self, job_id: str, allowed: set[str], target: str) -> JobRef:
        if job_id not in self._status:
            raise NotFoundError(f"job_svc: job {job_id} not found")
        current = self._status[job_id]
        if self.delay:
            await asyncio.sleep(self.delay)
        # Re-read after the await: the guard must reflect the latest state, the
        # way job_svc's conditional UPDATE does, so only one racing caller wins.
        current = self._status[job_id]
        if current not in allowed:
            raise StateError(f"job_svc: job {job_id} is {current!r}; expected {sorted(allowed)}")
        self._status[job_id] = target
        return JobRef(id=job_id, status=target)

    # Test helper: simulate a worker driving a job to a terminal state.
    def set_status(self, job_id: str, status: str) -> None:
        self._status[job_id] = status


@pytest.fixture
def jobs() -> FakeJobGateway:
    return FakeJobGateway()
