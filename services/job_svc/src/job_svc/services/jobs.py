"""JobService — create / get / update / retry on the `jobs` resource, plus the
poller-facing claim_batch / reap_expired backlog operations.

Status transitions go through a single conditional UPDATE with the current
status folded into its WHERE clause (`... WHERE id=:id AND status IN
(:allowed)`), rather than a plain fetch-then-check-then-mutate. A job's status
is meant to be driven by concurrent workers (and now the startup poller)
claiming and reporting on it, so the guard has to be enforced atomically at the
DB layer -- otherwise two concurrent transitions could each pass a Python-side
check before either commits. The precondition on allowed source statuses
doubles as the concurrency control; no separate locking or version column
needed.

UpdateJob can drive a job into any of the six statuses, but each target still
has a sensible set of allowed source statuses (see `_ALLOWED_ENTRY` plus the
`_start`/`_fail` special cases) so nonsensical jumps (e.g. succeeded -> running)
are rejected as StateError rather than silently applied.

Attempt accounting: moving queued -> running consumes one attempt (via `_start`,
or `claim_batch` for the poller), and a running -> failed transition dead-letters the
job (status `dead`) once `attempts` reaches `max_attempts`, otherwise parks it
at `failed`. RetryJob requeues a failed job while preserving its attempt count
(so repeated failures still converge on `dead`). A plain UpdateJob(queued) is
the other requeue path and likewise keeps the attempt count.

The default retry budget for a CreateJob that omits max_attempts is injected
(config-driven; see config.JobsSettings.default_max_attempts).

Horizontal scaling: the poller runs on every pod, so its claim path must let N
pods share the backlog without contention or double-execution. `claim_batch`
uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so each pod grabs a disjoint batch
instead of all racing for the same head-of-queue rows. Claiming also stamps a
lease (`locked_at`/`locked_by`); `reap_expired` requeues any `running` job whose
lease has expired, so a job is never stranded when the pod that claimed it dies
mid-run -- it resumes from its progress checkpoint on the next claim.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from job_svc.errors import NotFoundError, StateError, ValidationError
from job_svc.models import DEFAULT_MAX_ATTEMPTS, JobRow, new_id, now

# Target status -> the statuses a job must currently be in to reach it via a
# plain status overwrite in UpdateJob. "running" is handled by _start (it also
# consumes an attempt) and "failed" by _fail (it depends on attempts vs
# max_attempts), so neither appears here. Every other target is reachable:
# "queued" is a non-resetting manual requeue (RetryJob is the budget-resetting
# variant) and "dead" is a manual dead-letter (auto-escalation out of a failed
# run reaches it too).
# "waiting_approval" is entered from a running job the runner has paused on a
# human-approval gate (a tool refused to act until approved). It is left via
# "queued" -- an approving caller requeues the job so the poller re-runs it from
# its checkpoint -- or "cancelled". The poller claims only "queued" and the
# reaper only "running", so a waiting_approval job is never auto-advanced: it
# waits until a human acts.
_ALLOWED_ENTRY = {
    "succeeded": {"running"},
    "cancelled": {"queued", "running", "waiting_approval"},
    "queued": {"failed", "dead", "cancelled", "waiting_approval"},
    "dead": {"failed", "running"},
    "waiting_approval": {"running"},
}


class JobService:
    def __init__(
        self, sessions: async_sessionmaker, *, default_max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> None:
        self._sessions = sessions
        self._default_max_attempts = default_max_attempts

    async def create(
        self, *, tenant_id: str, type: str, spec: dict, max_attempts: int | None
    ) -> JobRow:
        # max_attempts range is validated at the edge (JobValidator); here we
        # only fold in the config-driven default when the caller omitted it.
        row = JobRow(
            id=new_id(),
            tenant_id=tenant_id,
            type=type,
            spec=spec,
            max_attempts=max_attempts if max_attempts is not None else self._default_max_attempts,
        )
        async with self._sessions.begin() as session:
            session.add(row)
        return row

    async def get(
        self, *, tenant_id: str, ids: list[str], statuses: list[str], types: list[str]
    ) -> list[JobRow]:
        async with self._sessions() as session:
            stmt = select(JobRow).where(JobRow.tenant_id == tenant_id)
            if ids:
                stmt = stmt.where(JobRow.id.in_(ids))
            if statuses:
                stmt = stmt.where(JobRow.status.in_(statuses))
            if types:
                stmt = stmt.where(JobRow.type.in_(types))
            return list((await session.scalars(stmt)).all())

    async def update(self, *, tenant_id: str, job_id: str, status: str) -> JobRow:
        if status == "running":
            return await self._start(tenant_id=tenant_id, job_id=job_id)
        if status == "failed":
            return await self._fail(tenant_id=tenant_id, job_id=job_id)
        allowed_from = _ALLOWED_ENTRY.get(status)
        if allowed_from is None:
            raise ValidationError(f"unknown target status {status!r}")
        values = {"status": status, "updated_at": now()}
        if status in ("queued", "waiting_approval"):
            # A job leaving `running` (requeued, or parked awaiting approval) is
            # no longer held by a worker, so release its claim lease -- otherwise
            # a stale locked_by lingers and, for waiting_approval, the reaper
            # could confuse it (it only reaps `running`, but keep the lease clean).
            values["locked_at"] = None
            values["locked_by"] = None
        return await self._guarded_update(
            tenant_id=tenant_id,
            job_id=job_id,
            allowed_from=allowed_from,
            values=values,
        )

    async def _start(self, *, tenant_id: str, job_id: str) -> JobRow:
        # Starting a run consumes one attempt -- the "claim" analog a worker
        # would perform. _fail then dead-letters once the budget is exhausted.
        return await self._guarded_update(
            tenant_id=tenant_id,
            job_id=job_id,
            allowed_from={"queued"},
            values={
                "status": "running",
                "attempts": JobRow.attempts + 1,
                # Stamp a lease so even a manually-started run is reapable if
                # nothing drives it to a terminal status. locked_by is unset --
                # this start did not come from an identified worker.
                "locked_at": now(),
                "locked_by": None,
                "updated_at": now(),
            },
        )

    async def _fail(self, *, tenant_id: str, job_id: str) -> JobRow:
        # attempts was already incremented at _start, so escalate to dead once
        # the budget is spent, otherwise fall back to a retryable failed state.
        values = {
            "status": case((JobRow.attempts >= JobRow.max_attempts, "dead"), else_="failed"),
            "updated_at": now(),
        }
        return await self._guarded_update(
            tenant_id=tenant_id, job_id=job_id, allowed_from={"running"}, values=values
        )

    async def retry(self, *, tenant_id: str, job_id: str) -> JobRow:
        return await self._guarded_update(
            tenant_id=tenant_id,
            job_id=job_id,
            allowed_from={"failed"},
            values={
                "status": "queued",
                "attempts": JobRow.attempts,
                "locked_at": None,
                "locked_by": None,
                "updated_at": now(),
            },
        )

    async def list_queued(self, *, limit: int) -> list[JobRow]:
        # System-wide (not tenant-scoped): the poller is a platform worker, not a
        # caller acting on behalf of one tenant. Oldest-first so queued jobs are
        # picked up roughly FIFO. Read-only -- used for observe-only backlog
        # reporting; the actual claim path is claim_batch.
        async with self._sessions() as session:
            stmt = (
                select(JobRow)
                .where(JobRow.status == "queued")
                .order_by(JobRow.created_at)
                .limit(limit)
            )
            return list((await session.scalars(stmt)).all())

    async def claim_batch(self, *, limit: int, owner: str) -> list[JobRow]:
        """Atomically claim up to `limit` of the oldest queued jobs for `owner`
        (queued -> running, one attempt each, lease stamped), returning the
        claimed rows.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so that when the poller is
        scaled across pods, each pod locks and skips the rows another pod is
        already claiming and walks away with a *disjoint* batch. That avoids the
        thundering herd of every pod fetching the same head-of-queue rows and
        then losing the claim race on all but one -- throughput now scales with
        the number of pods instead of collapsing onto the same rows.

        SKIP LOCKED is a Postgres feature. Under SQLite (tests) the locking
        clause is a no-op, which is harmless: its single writer already
        serialises access, so batches cannot overlap there anyway.
        """
        async with self._sessions.begin() as session:
            candidates = (
                select(JobRow.id)
                .where(JobRow.status == "queued")
                .order_by(JobRow.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            stmt = (
                update(JobRow)
                .where(JobRow.id.in_(candidates.scalar_subquery()))
                .values(
                    status="running",
                    attempts=JobRow.attempts + 1,
                    locked_at=now(),
                    locked_by=owner,
                    updated_at=now(),
                )
                .returning(JobRow)
                .execution_options(synchronize_session=False)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def reap_expired(self, *, lease_seconds: float) -> list[JobRow]:
        """Requeue jobs stranded in `running` past their claim lease -- the pod
        that claimed them died before reporting a terminal status. Returns the
        reaped rows.

        Attempts are preserved (the attempt was already consumed at claim), and a
        job that has exhausted its budget is dead-lettered rather than retried,
        mirroring _fail's escalation. Combined with the persisted progress
        checkpoint, a reaped job resumes from its last completed step on the next
        claim rather than restarting.

        System-wide and safe to run from every pod: the UPDATE is atomic, so each
        stale job is reclaimed by exactly one reaper. Jobs with a NULL lease are
        never reaped (NULL comparisons are not true), so only genuinely leased
        runs are considered.
        """
        cutoff = now() - timedelta(seconds=lease_seconds)
        async with self._sessions.begin() as session:
            stmt = (
                update(JobRow)
                .where(JobRow.status == "running", JobRow.locked_at < cutoff)
                .values(
                    status=case(
                        (JobRow.attempts >= JobRow.max_attempts, "dead"), else_="queued"
                    ),
                    locked_at=None,
                    locked_by=None,
                    updated_at=now(),
                )
                .returning(JobRow)
                .execution_options(synchronize_session=False)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def save_progress(self, *, job_id: str, progress: dict) -> None:
        # System-wide absolute write of the runner's checkpoint. A job is claimed
        # by exactly one runner at a time (claim_batch is the atomic queued ->
        # running gate), so this is single-writer per job -- an overwrite, not a
        # read-modify-write race. Deliberately does NOT touch status/attempts, so
        # a checkpoint can be persisted between step transitions.
        async with self._sessions.begin() as session:
            stmt = (
                update(JobRow)
                .where(JobRow.id == job_id)
                .values(progress=progress, updated_at=now())
                .returning(JobRow.id)
            )
            if (await session.execute(stmt)).scalar_one_or_none() is None:
                raise NotFoundError(f"job {job_id} not found")

    async def _guarded_update(
        self, *, tenant_id: str, job_id: str, allowed_from: set[str], values: dict
    ) -> JobRow:
        async with self._sessions.begin() as session:
            stmt = (
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.tenant_id == tenant_id,
                    JobRow.status.in_(allowed_from),
                )
                .values(**values)
                .returning(JobRow)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                return row
            existing = await session.get(JobRow, job_id)
            if existing is None or existing.tenant_id != tenant_id:
                raise NotFoundError(f"job {job_id} not found")
            raise StateError(
                f"job {job_id} is {existing.status!r}; expected one of {sorted(allowed_from)}"
            )
