"""Shared test fixtures.

The suite runs against an in-memory SQLite database (via aiosqlite) so it needs
no live Postgres. JobService owns its state transitions against that DB
directly, so there is no remote dependency to fake -- unlike the edge service,
job_svc is the source of truth.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from job_svc.errors import DependencyError
from job_svc.models import Base, JobRow
from job_svc.orchestrator_client import ChatReply, ChatTurn, OrchestratorGateway
from job_svc.services.jobs import JobService

# Small, explicit budgets so tests can exhaust them in one or two attempts.
TEST_DEFAULT_MAX_ATTEMPTS = 3
TEST_DEFAULT_MAX_RETRIES = 3


async def claim_one(service: JobService, *, owner: str = "test-worker") -> JobRow:
    """Claim the single oldest queued job (queued -> running, one attempt, lease
    stamped) via the poller's real claim path, and return it.

    A test convenience standing in for the removed single-job `claim`: every
    caller has exactly one queued job at claim time, so `claim_batch(limit=1)`
    returns precisely that row."""
    (row,) = await service.claim_batch(limit=1, owner=owner)
    return row


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


@pytest.fixture
def service(sessions) -> JobService:
    return JobService(
        sessions,
        default_max_attempts=TEST_DEFAULT_MAX_ATTEMPTS,
        default_max_retries=TEST_DEFAULT_MAX_RETRIES,
    )


class FakeOrchestrator(OrchestratorGateway):
    """In-process stand-in for the orchestrator's Chat.

    A planning call is recognized by a "system" role turn (the planner
    instruction); it returns the configured `plan` as a JSON array. An execution
    call returns a deterministic ``"done: <sub-prompt>"`` output. `fail_on` is a
    set of sub-prompt strings (or the sentinel ``"plan"``) that raise a
    DependencyError, so failure/resume paths can be exercised.
    """

    def __init__(
        self,
        *,
        plan: list[str] | None = None,
        fail_on: set[str] | None = None,
        approve_on: set[str] | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.plan = ["step one", "step two"] if plan is None else plan
        self.fail_on = fail_on or set()
        # Sub-prompts whose execution returns a pending-approval finish_reason
        # (as the orchestrator would when a tool paused on a human-approval
        # gate), so the runner's waiting_approval path can be exercised.
        self.approve_on = approve_on or set()
        # Simulates a slow orchestrator call (e.g. a long LLM turn) so tests
        # can exercise behavior that only matters while a step is in flight,
        # like the runner's lease-renewal heartbeat.
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, str]] = []  # (kind, user content)

    async def chat(self, *, tenant_id: str, messages: list[ChatTurn]) -> ChatReply:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        is_plan = any(t.role == "system" for t in messages)
        user = next((t.content for t in messages if t.role == "user"), "")
        self.calls.append(("plan" if is_plan else "exec", user))
        if is_plan:
            if "plan" in self.fail_on:
                raise DependencyError("orchestrator: planning failed")
            return ChatReply(content=json.dumps(self.plan), finish_reason="stop")
        if user in self.fail_on:
            raise DependencyError(f"orchestrator: exec failed for {user!r}")
        if user in self.approve_on:
            return ChatReply(
                content="APPROVAL_REQUIRED: awaiting human approval",
                finish_reason="requires_approval",
            )
        return ChatReply(content=f"done: {user}", finish_reason="stop")

    @property
    def plan_calls(self) -> int:
        return sum(1 for kind, _ in self.calls if kind == "plan")

    @property
    def exec_calls(self) -> list[str]:
        return [content for kind, content in self.calls if kind == "exec"]


@pytest.fixture
def orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()
