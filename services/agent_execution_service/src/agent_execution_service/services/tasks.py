"""TaskService — submit (create) / get / approve / retry on the `tasks`
resource. A task is a thin edge-side handle onto a job in job_svc: every task
corresponds to exactly one job, and the task's status mirrors that job's.

Delegation and the source of truth
-----------------------------------
State transitions are NOT owned here. job_svc owns them and already enforces
them atomically (a single conditional UPDATE with the allowed source statuses
folded into the WHERE clause), so this service never does a fetch-then-check-
then-mutate on job state — it forwards to job_svc and lets that guard decide.
That keeps the transition concurrency-safe even with many callers:

  - approve  -> job_svc UpdateJob(RUNNING): only one concurrent approve of a
    queued job wins (queued -> running); the loser gets FAILED_PRECONDITION,
    surfaced here as StateError.
  - retry    -> job_svc RetryJob: only valid from failed/dead, likewise guarded.

What this service does own is a local `tasks` row per submission and a snapshot
of the job's status and final result, refreshed from the authoritative Job that
job_svc returns on every mutating call and, for a task that has not yet reached
a terminal state, on read as well: GetTask fetches the backing job so a job that
ran to completion on its own in job_svc is reflected, while a terminal task is
served straight from the local snapshot with no remote call. The remote call is
always made OUTSIDE a DB transaction so a slow/unreachable job_svc never pins a
Postgres connection, and the snapshot write sets an absolute value (never a
read-modify-write), so concurrent refreshes are last-writer-wins against the
same source of truth rather than a lost-update race.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_execution_service.errors import AppError, NotFoundError
from agent_execution_service.job_client import JobGateway, JobRef, JobStepRef
from agent_execution_service.models import TaskRow, new_id, now

# job_svc status string -> the task status string stored in TaskRow.status
# (mapped to the proto TaskStatus in mappers.py). A job the runner has paused on
# a human-approval gate reports `waiting_approval`, surfaced verbatim so GetTask
# shows WAITING_APPROVAL until the task is approved.
_TASK_STATUS_FROM_JOB = {
    "queued": "pending",
    "running": "running",
    "waiting_approval": "waiting_approval",
    "succeeded": "completed",
    "failed": "failed",
    "dead": "failed",
    "cancelled": "failed",
}


def _task_status(job_status: str) -> str:
    return _TASK_STATUS_FROM_JOB.get(job_status, "pending")


# A task in one of these states is finished: its backing job can no longer change
# status or produce a new result, so GetTask serves it straight from the local
# snapshot without refreshing from job_svc.
_TERMINAL_STATUSES = frozenset({"completed", "failed"})


@dataclass(frozen=True, slots=True)
class TaskProgressView:
    """A task-centric snapshot of how far a task has advanced, distilled from
    its backing job's execution checkpoint. Job-internal mechanics (the planning
    phase, attempt/retry counters) are already resolved away: what remains is
    the task's own lifecycle status, a normalized percentage, the ordered step
    history and the caller-actionable approval flag / one-line summary.
    """

    task_id: str
    status: str
    percent_complete: float
    steps_completed: int
    steps_total: int
    requires_approval: bool
    summary: str
    steps: tuple[JobStepRef, ...]
    output: str
    error: str
    updated_at: datetime


def _percent_complete(status: str, completed: int, total: int) -> float:
    # A completed task is 100% by definition even if the plan count is unknown;
    # otherwise it is the fraction of planned steps finished (0 while the job is
    # still pending/planning, i.e. before any plan exists).
    if status == "completed":
        return 100.0
    if total <= 0:
        return 0.0
    return round(min(completed / total, 1.0) * 100.0, 1)


def _summary(status: str, completed: int, total: int, error: str) -> str:
    if status == "running":
        return f"Running: {completed} of {total} steps done" if total else "Running: planning"
    if status == "waiting_approval":
        return "Waiting for approval"
    if status == "completed":
        return "Completed"
    if status == "failed":
        return f"Failed: {error}" if error else "Failed"
    return "Pending"


class TaskService:
    def __init__(self, sessions: async_sessionmaker, jobs: JobGateway) -> None:
        self._sessions = sessions
        self._jobs = jobs

    async def create(self, *, tenant_id: str, input: str) -> TaskRow:
        # Submit the job first: if job_svc rejects it, no orphan task row is
        # left behind. Input validation happens in the servicer.
        ref = await self._jobs.create_job(tenant_id=tenant_id, input=input)
        row = TaskRow(
            id=new_id(),
            tenant_id=tenant_id,
            input=input,
            job_id=ref.id,
            status=_task_status(ref.status),
            result=ref.result,
        )
        async with self._sessions.begin() as session:
            session.add(row)
        return row

    async def get(self, *, tenant_id: str, ids: list[str]) -> list[TaskRow]:
        rows = await self._load(tenant_id=tenant_id, ids=ids)
        # A task's status/result drift between mutating calls because its job
        # advances on its own in job_svc (the poller runs it to completion). So
        # refresh any non-terminal task from the authoritative Job before
        # returning; a terminal task can no longer change and is served straight
        # from the local snapshot with no remote call.
        stale = [row for row in rows if row.status not in _TERMINAL_STATUSES]
        if stale and await self._refresh(tenant_id=tenant_id, rows=stale):
            rows = await self._load(tenant_id=tenant_id, ids=ids)
        return rows

    async def get_progress(self, *, tenant_id: str, task_id: str) -> TaskProgressView:
        # Progress is a live view onto the backing job's execution checkpoint,
        # not a value cached on the task row, so it is always read straight from
        # the authoritative Job -- there is no terminal short-circuit like get's
        # snapshot. The task is loaded first (tenant-scoped) so an unknown or
        # foreign task is a NotFoundError before any job_svc call, and the
        # remote call is then made OUTSIDE the DB session so a slow job_svc never
        # pins a connection. Any job_svc error is allowed to surface (unlike
        # _refresh, which swallows it): the remote progress IS the response, so
        # failing to fetch it must fail the call rather than return a stale view.
        async with self._sessions() as session:
            row = await session.get(TaskRow, task_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"task {task_id} not found")
            task_id, job_id, updated_at = row.id, row.job_id, row.updated_at
        ref = await self._jobs.get_job_progress(tenant_id=tenant_id, job_id=job_id)
        status = _task_status(ref.status)
        completed = len(ref.steps)
        # Never report fewer planned steps than have already completed, so a
        # partially-reported plan can't yield a >100% or nonsensical fraction.
        total = max(ref.steps_total, completed)
        error = ref.error if status == "failed" else ""
        return TaskProgressView(
            task_id=task_id,
            status=status,
            percent_complete=_percent_complete(status, completed, total),
            steps_completed=completed,
            steps_total=total,
            requires_approval=status == "waiting_approval",
            summary=_summary(status, completed, total, error),
            steps=ref.steps,
            output=ref.result,
            error=error,
            updated_at=updated_at,
        )

    async def _load(self, *, tenant_id: str, ids: list[str]) -> list[TaskRow]:
        async with self._sessions() as session:
            stmt = select(TaskRow).where(TaskRow.tenant_id == tenant_id)
            if ids:
                stmt = stmt.where(TaskRow.id.in_(ids))
            return list((await session.scalars(stmt)).all())

    async def _refresh(self, *, tenant_id: str, rows: list[TaskRow]) -> bool:
        # Pull fresh Job snapshots concurrently and OUTSIDE any DB transaction (a
        # slow/unreachable job_svc must never pin a Postgres connection), then
        # persist only the tasks that actually changed. Each write is an
        # absolute, tenant-scoped set (never a read-modify-write), so concurrent
        # refreshes are last-writer-wins against the one source of truth. A
        # per-task fetch failure is non-fatal: the stale snapshot is kept and
        # retried on a later read, so a job_svc blip never fails the read.
        async def fetch(row: TaskRow) -> tuple[str, str, str] | None:
            try:
                ref = await self._jobs.get_job(tenant_id=tenant_id, job_id=row.job_id)
            except AppError:
                return None
            status = _task_status(ref.status)
            if status == row.status and ref.result == row.result:
                return None
            return row.id, status, ref.result

        changes = [c for c in await asyncio.gather(*(fetch(r) for r in rows)) if c is not None]
        if not changes:
            return False
        async with self._sessions.begin() as session:
            for task_id, status, result in changes:
                await session.execute(
                    update(TaskRow)
                    .where(TaskRow.id == task_id, TaskRow.tenant_id == tenant_id)
                    .values(status=status, result=result, updated_at=now())
                )
        return True

    async def approve(self, *, tenant_id: str, task_id: str) -> TaskRow:
        job_id = await self._job_id_for(tenant_id=tenant_id, task_id=task_id)
        ref = await self._jobs.start_job(tenant_id=tenant_id, job_id=job_id)
        return await self._save_status(tenant_id=tenant_id, task_id=task_id, ref=ref)

    async def retry(self, *, tenant_id: str, task_id: str) -> TaskRow:
        job_id = await self._job_id_for(tenant_id=tenant_id, task_id=task_id)
        ref = await self._jobs.retry_job(tenant_id=tenant_id, job_id=job_id)
        return await self._save_status(tenant_id=tenant_id, task_id=task_id, ref=ref)

    async def _job_id_for(self, *, tenant_id: str, task_id: str) -> str:
        async with self._sessions() as session:
            row = await session.get(TaskRow, task_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"task {task_id} not found")
            return row.job_id

    async def _save_status(self, *, tenant_id: str, task_id: str, ref: JobRef) -> TaskRow:
        # Absolute write of the job_svc-authoritative status; scoped by tenant so
        # it can never touch another tenant's row.
        async with self._sessions.begin() as session:
            stmt = (
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.tenant_id == tenant_id)
                .values(status=_task_status(ref.status), result=ref.result, updated_at=now())
                .returning(TaskRow)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"task {task_id} not found")
            return row
