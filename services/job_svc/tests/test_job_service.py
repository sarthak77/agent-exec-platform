"""Tests for JobService: creation defaults, tenant-scoped reads, the full
status-transition matrix (including attempt accounting and dead-letter
escalation), retry semantics, and the poller-facing list_queued/claim_batch path."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from job_svc.errors import NotFoundError, StateError, ValidationError
from job_svc.models import JobRow, now
from job_svc.services.jobs import JobService

from tests.conftest import claim_one

TENANT = "t1"
OTHER = "t2"
SPEC = {"agent_execution_spec": {"name": "demo"}}


async def _create(service: JobService, *, tenant_id=TENANT, max_attempts=None, max_retries=None):
    return await service.create(
        tenant_id=tenant_id,
        type="agent_execution",
        spec=SPEC,
        max_attempts=max_attempts,
        max_retries=max_retries,
    )


async def _at(service: JobService, tenant_id: str, job_id: str):
    (row,) = await service.get(tenant_id=tenant_id, ids=[job_id], statuses=[], types=[])
    return row


async def _queued_ids(service: JobService):
    return {r.id for r in await service.list_queued(limit=100)}


# --- create ----------------------------------------------------------------


async def test_create_applies_injected_default_max_attempts(sessions) -> None:
    service = JobService(sessions, default_max_attempts=7)
    row = await _create(service)
    assert row.max_attempts == 7
    assert row.status == "queued"
    assert row.attempts == 0


async def test_create_respects_explicit_max_attempts(service) -> None:
    row = await _create(service, max_attempts=1)
    assert row.max_attempts == 1


async def test_create_applies_injected_default_max_retries(sessions) -> None:
    service = JobService(sessions, default_max_retries=5)
    row = await _create(service)
    assert row.max_retries == 5


async def test_create_respects_explicit_max_retries(service) -> None:
    row = await _create(service, max_retries=1)
    assert row.max_retries == 1


async def test_create_persists_row(service) -> None:
    row = await _create(service)
    fetched = await _at(service, TENANT, row.id)
    assert fetched.id == row.id
    assert fetched.spec == SPEC


# --- get / filtering / tenant scoping --------------------------------------


async def test_get_returns_only_own_tenant(service) -> None:
    mine = await _create(service, tenant_id=TENANT)
    await _create(service, tenant_id=OTHER)
    rows = await service.get(tenant_id=TENANT, ids=[], statuses=[], types=[])
    assert [r.id for r in rows] == [mine.id]


async def test_get_filters_by_ids(service) -> None:
    a = await _create(service)
    await _create(service)
    rows = await service.get(tenant_id=TENANT, ids=[a.id], statuses=[], types=[])
    assert [r.id for r in rows] == [a.id]


async def test_get_filters_by_status(service) -> None:
    queued = await _create(service)
    running = await _create(service)
    await service.update(tenant_id=TENANT, job_id=running.id, status="running")
    rows = await service.get(tenant_id=TENANT, ids=[], statuses=["queued"], types=[])
    assert [r.id for r in rows] == [queued.id]


async def test_get_filters_by_type(service) -> None:
    a = await _create(service)
    rows = await service.get(tenant_id=TENANT, ids=[], statuses=[], types=["agent_execution"])
    assert [r.id for r in rows] == [a.id]
    assert await service.get(tenant_id=TENANT, ids=[], statuses=[], types=["other_type"]) == []


# --- update: running (claim/start accounting) ------------------------------


async def test_update_running_from_queued_consumes_attempt(service) -> None:
    row = await _create(service)
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    assert updated.status == "running"
    assert updated.attempts == 1


async def test_update_running_from_non_queued_fails_precondition(service) -> None:
    row = await _create(service)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="running")


# --- update: succeeded -----------------------------------------------------


async def test_update_succeeded_from_running(service) -> None:
    row = await _create(service)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="succeeded")
    assert updated.status == "succeeded"


async def test_update_succeeded_from_queued_fails_precondition(service) -> None:
    row = await _create(service)
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="succeeded")


# --- update: cancelled -----------------------------------------------------


async def test_update_cancelled_from_queued(service) -> None:
    row = await _create(service)
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="cancelled")
    assert updated.status == "cancelled"


async def test_update_cancelled_from_running(service) -> None:
    row = await _create(service)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="cancelled")
    assert updated.status == "cancelled"


async def test_update_cancelled_from_succeeded_fails_precondition(service) -> None:
    row = await _create(service)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    await service.update(tenant_id=TENANT, job_id=row.id, status="succeeded")
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="cancelled")


# --- update: failed / two-tier retry escalation -----------------------------


async def test_update_failed_auto_retries_while_attempts_budget_remains(service) -> None:
    row = await _create(service, max_attempts=3)  # attempt 1 leaves auto budget
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    # No caller ever notices -- the job is silently requeued, not parked.
    assert updated.status == "queued"
    assert updated.attempts == 1


async def test_update_failed_parks_once_attempts_exhausted_with_retries_remaining(
    service,
) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    # Auto budget spent, but the manual budget still has room -- rest here for
    # a human to notice and call RetryJob.
    assert updated.status == "failed"
    assert updated.attempts == 1
    assert updated.retry_count == 0


async def test_update_failed_dead_letters_when_both_budgets_exhausted(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=0)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    assert updated.status == "dead"


async def test_update_failed_from_queued_fails_precondition(service) -> None:
    row = await _create(service)
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="failed")


# --- update: queued (non-resetting manual requeue) -------------------------


async def test_update_queued_from_failed_preserves_attempts(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)  # -> failed, budget remains
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="queued")
    assert updated.status == "queued"
    assert updated.attempts == 1  # unlike retry(), the count is preserved


async def test_update_queued_from_dead_preserves_attempts(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=0)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")  # -> dead
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="queued")
    assert updated.status == "queued"
    assert updated.attempts == 1


async def test_update_queued_from_running_fails_precondition(service) -> None:
    row = await _create(service)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="queued")


# --- update: dead (manual dead-letter) -------------------------------------


async def test_update_dead_from_running(service) -> None:
    row = await _create(service)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="dead")
    assert updated.status == "dead"


async def test_update_dead_from_failed(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)  # -> failed, budget remains
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    updated = await service.update(tenant_id=TENANT, job_id=row.id, status="dead")
    assert updated.status == "dead"


async def test_update_dead_from_queued_fails_precondition(service) -> None:
    row = await _create(service)
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="dead")


# --- update: error paths ---------------------------------------------------


async def test_update_unknown_status_rejected(service) -> None:
    row = await _create(service)
    with pytest.raises(ValidationError, match="unknown target status"):
        await service.update(tenant_id=TENANT, job_id=row.id, status="bogus")


async def test_update_unknown_job_not_found(service) -> None:
    with pytest.raises(NotFoundError):
        await service.update(tenant_id=TENANT, job_id="missing", status="running")


async def test_update_other_tenant_job_not_found(service) -> None:
    row = await _create(service, tenant_id=OTHER)
    with pytest.raises(NotFoundError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="running")


# --- retry -----------------------------------------------------------------


async def test_retry_resets_attempts_and_consumes_retry_count(service) -> None:
    # Auto budget exhausted (parking at failed), manual budget has room.
    row = await _create(service, max_attempts=1, max_retries=3)
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    updated = await service.retry(tenant_id=TENANT, job_id=row.id)
    assert updated.status == "queued"
    # A fresh automatic cycle is granted in exchange for consuming one unit of
    # the manual budget.
    assert updated.attempts == 0
    assert updated.retry_count == 1


async def test_retry_repeated_until_manual_budget_exhausted_reaches_dead(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=1)
    for _ in range(2):
        await claim_one(service)
        await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
        fetched = await _at(service, TENANT, row.id)
        if fetched.status == "failed":
            await service.retry(tenant_id=TENANT, job_id=row.id)

    assert (await _at(service, TENANT, row.id)).status == "dead"


async def test_retry_queued_fails_precondition(service) -> None:
    row = await _create(service)
    with pytest.raises(StateError):
        await service.retry(tenant_id=TENANT, job_id=row.id)


async def test_retry_unknown_job_not_found(service) -> None:
    with pytest.raises(NotFoundError):
        await service.retry(tenant_id=TENANT, job_id="missing")


# --- list_queued (poller-facing, system-wide) ------------------------------


async def test_list_queued_is_system_wide_and_oldest_first(service) -> None:
    a = await _create(service, tenant_id=TENANT)
    b = await _create(service, tenant_id=OTHER)
    # Move one out of queued so it is excluded.
    running = await _create(service, tenant_id=TENANT)
    await service.update(tenant_id=TENANT, job_id=running.id, status="running")

    rows = await service.list_queued(limit=10)
    assert [r.id for r in rows] == [a.id, b.id]  # both tenants, creation order


async def test_list_queued_respects_limit(service) -> None:
    for _ in range(5):
        await _create(service)
    rows = await service.list_queued(limit=2)
    assert len(rows) == 2


# --- save_progress (runner checkpoint) -------------------------------------


async def test_save_progress_persists_and_survives_status_change(service) -> None:
    row = await _create(service)
    progress = {"phase": "executing", "plan": ["a"], "steps": {}}
    await service.save_progress(job_id=row.id, progress=progress)

    fetched = await _at(service, TENANT, row.id)
    assert fetched.progress == progress
    assert fetched.status == "queued"  # untouched
    assert fetched.attempts == 0  # untouched


async def test_save_progress_checkpoint_survives_failed_and_retry(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)  # -> failed, budget remains
    await service.update(tenant_id=TENANT, job_id=row.id, status="running")
    await service.save_progress(job_id=row.id, progress={"plan": ["a", "b"], "steps": {"0": {}}})
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    await service.retry(tenant_id=TENANT, job_id=row.id)

    fetched = await _at(service, TENANT, row.id)
    assert fetched.status == "queued"
    assert fetched.progress == {"plan": ["a", "b"], "steps": {"0": {}}}  # checkpoint intact


async def test_save_progress_unknown_job_not_found(service) -> None:
    with pytest.raises(NotFoundError):
        await service.save_progress(job_id="missing", progress={})


# --- claim_batch (SKIP LOCKED bulk claim) ----------------------------------


async def test_claim_batch_claims_oldest_up_to_limit_and_stamps_lease(service) -> None:
    a = await _create(service)
    b = await _create(service)
    c = await _create(service)

    claimed = await service.claim_batch(limit=2, owner="worker-1")

    assert {r.id for r in claimed} == {a.id, b.id}  # oldest two, c left behind
    assert all(r.status == "running" and r.attempts == 1 for r in claimed)
    assert all(r.locked_by == "worker-1" and r.locked_at is not None for r in claimed)
    assert await _queued_ids(service) == {c.id}


async def test_claim_batch_only_claims_queued(service) -> None:
    queued = await _create(service)
    running = await _create(service)
    await service.update(tenant_id=TENANT, job_id=running.id, status="running")

    claimed = await service.claim_batch(limit=10, owner="w")

    assert {r.id for r in claimed} == {queued.id}  # the already-running one is skipped


async def test_claim_batch_empty_backlog_returns_empty(service) -> None:
    assert await service.claim_batch(limit=10, owner="w") == []


# --- reap_expired (crash recovery) -----------------------------------------


async def test_reap_expired_requeues_stale_running_and_clears_lease(service, sessions) -> None:
    row = await _create(service, max_attempts=3)
    await claim_one(service, owner="dead-pod")  # running, attempt 1
    await _expire_lease(sessions, row.id)

    reaped = await service.reap_expired(lease_seconds=60)

    assert [r.id for r in reaped] == [row.id]
    fetched = await _at(service, TENANT, row.id)
    assert fetched.status == "queued"
    assert fetched.attempts == 1  # attempt preserved (already consumed at claim)
    assert fetched.locked_at is None and fetched.locked_by is None


async def test_reap_expired_parks_at_failed_when_attempts_exhausted_but_retries_remain(
    service, sessions
) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)
    await claim_one(service)  # attempt 1 == max
    await _expire_lease(sessions, row.id)

    reaped = await service.reap_expired(lease_seconds=60)

    assert [r.status for r in reaped] == ["failed"]  # manual budget remains -> rest, not dead
    assert (await _at(service, TENANT, row.id)).status == "failed"


async def test_reap_expired_dead_letters_when_both_budgets_exhausted(service, sessions) -> None:
    row = await _create(service, max_attempts=1, max_retries=0)
    await claim_one(service)  # attempt 1 == max
    await _expire_lease(sessions, row.id)

    reaped = await service.reap_expired(lease_seconds=60)

    assert [r.status for r in reaped] == ["dead"]  # no budget left at all -> dead-lettered
    assert (await _at(service, TENANT, row.id)).status == "dead"


async def test_reap_expired_leaves_fresh_running_alone(service) -> None:
    row = await _create(service)
    await claim_one(service)  # fresh lease, well within window

    assert await service.reap_expired(lease_seconds=60) == []
    assert (await _at(service, TENANT, row.id)).status == "running"


async def test_reap_expired_ignores_running_without_a_lease(service, sessions) -> None:
    # A running row with a NULL lease (NULL comparisons are never true) must not
    # be reaped -- reaping only ever targets genuinely leased runs.
    row = await _create(service)
    await claim_one(service)
    async with sessions.begin() as session:
        await session.execute(
            update(JobRow).where(JobRow.id == row.id).values(locked_at=None)
        )

    assert await service.reap_expired(lease_seconds=0) == []
    assert (await _at(service, TENANT, row.id)).status == "running"


# --- renew_lease (runner heartbeat) -----------------------------------------


async def test_renew_lease_prevents_reaping_a_long_running_job(service, sessions) -> None:
    row = await _create(service)
    await claim_one(service, owner="w")
    await _expire_lease(sessions, row.id)  # simulate a lease that's about to go stale

    renewed = await service.renew_lease(job_id=row.id)
    assert renewed is True

    # The reaper would have requeued this job at the old (expired) lease, but
    # renew_lease just refreshed it, so it's still considered alive.
    assert await service.reap_expired(lease_seconds=60) == []
    assert (await _at(service, TENANT, row.id)).status == "running"


async def test_renew_lease_no_op_for_non_running_job(service) -> None:
    row = await _create(service)  # still queued, never claimed

    assert await service.renew_lease(job_id=row.id) is False
    assert (await _at(service, TENANT, row.id)).status == "queued"


async def test_renew_lease_no_op_for_unknown_job(service) -> None:
    assert await service.renew_lease(job_id="does-not-exist") is False


# --- lease is released when a job returns to the queue ---------------------


async def test_retry_clears_lease(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)  # -> failed, budget remains
    await claim_one(service, owner="w")
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    await service.retry(tenant_id=TENANT, job_id=row.id)

    fetched = await _at(service, TENANT, row.id)
    assert fetched.locked_at is None and fetched.locked_by is None


async def test_update_to_queued_clears_lease(service) -> None:
    row = await _create(service, max_attempts=1, max_retries=3)  # -> failed, budget remains
    await claim_one(service, owner="w")
    await service.update(tenant_id=TENANT, job_id=row.id, status="failed")
    await service.update(tenant_id=TENANT, job_id=row.id, status="queued")

    fetched = await _at(service, TENANT, row.id)
    assert fetched.locked_at is None and fetched.locked_by is None


async def _expire_lease(sessions, job_id: str, *, seconds: float = 120) -> None:
    async with sessions.begin() as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(locked_at=now() - timedelta(seconds=seconds))
        )


# --- waiting_approval (human-approval pause) -------------------------------


async def test_running_to_waiting_approval_clears_lease(service) -> None:
    row = await _create(service)
    await claim_one(service, owner="w1")  # queued -> running, lease stamped

    paused = await service.update(tenant_id=TENANT, job_id=row.id, status="waiting_approval")
    assert paused.status == "waiting_approval"
    # The job is no longer held by a worker, so the claim lease is released.
    assert paused.locked_at is None
    assert paused.locked_by is None
    # The attempt consumed at claim is not refunded.
    assert paused.attempts == 1


async def test_waiting_approval_to_queued_is_the_approve_transition(service) -> None:
    row = await _create(service)
    await claim_one(service, owner="w1")
    await service.update(tenant_id=TENANT, job_id=row.id, status="waiting_approval")

    requeued = await service.update(tenant_id=TENANT, job_id=row.id, status="queued")
    assert requeued.status == "queued"
    # Requeuing preserves the attempt count (non-resetting requeue).
    assert requeued.attempts == 1
    assert row.id in await _queued_ids(service)


async def test_cannot_enter_waiting_approval_from_queued(service) -> None:
    row = await _create(service)  # still queued, never ran
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="waiting_approval")


async def test_cannot_succeed_directly_from_waiting_approval(service) -> None:
    row = await _create(service)
    await claim_one(service, owner="w1")
    await service.update(tenant_id=TENANT, job_id=row.id, status="waiting_approval")
    # succeeded is only reachable from running; a paused job must be resumed
    # (-> queued -> running) before it can complete.
    with pytest.raises(StateError):
        await service.update(tenant_id=TENANT, job_id=row.id, status="succeeded")
