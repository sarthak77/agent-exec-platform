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
of the job's status, refreshed on every mutating call from the authoritative
Job that job_svc returns. GetTask then reads that snapshot locally instead of
fanning out to job_svc on every read. The remote call is always made OUTSIDE a
DB transaction so a slow/unreachable job_svc never pins a Postgres connection,
and the snapshot write sets an absolute value (never a read-modify-write), so
concurrent refreshes are last-writer-wins against the same source of truth
rather than a lost-update race.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_execution_service.errors import NotFoundError
from agent_execution_service.job_client import JobGateway, JobRef
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
        )
        async with self._sessions.begin() as session:
            session.add(row)
        return row

    async def get(self, *, tenant_id: str, ids: list[str]) -> list[TaskRow]:
        async with self._sessions() as session:
            stmt = select(TaskRow).where(TaskRow.tenant_id == tenant_id)
            if ids:
                stmt = stmt.where(TaskRow.id.in_(ids))
            return list((await session.scalars(stmt)).all())

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
                .values(status=_task_status(ref.status), updated_at=now())
                .returning(TaskRow)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"task {task_id} not found")
            return row
