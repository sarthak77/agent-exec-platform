"""Tests for TaskService — the task<->job delegation, status snapshotting,
tenant scoping, and concurrency behaviour."""

from __future__ import annotations

import asyncio

import pytest

from agent_execution_service.errors import NotFoundError, StateError
from agent_execution_service.services.tasks import TaskService

TENANT = "t1"
OTHER = "t2"


async def test_create_submits_a_job_and_links_it(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    row = await svc.create(tenant_id=TENANT, input="summarize this")

    assert row.job_id  # a job id was assigned by job_svc
    assert row.status == "pending"  # queued job -> pending task
    assert jobs.created == ["summarize this"]  # input forwarded as the job


async def test_get_returns_only_own_tenant_tasks(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    mine = await svc.create(tenant_id=TENANT, input="a")
    await svc.create(tenant_id=OTHER, input="b")

    rows = await svc.get(tenant_id=TENANT, ids=[])
    assert [r.id for r in rows] == [mine.id]


async def test_get_by_ids_filters(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    a = await svc.create(tenant_id=TENANT, input="a")
    await svc.create(tenant_id=TENANT, input="b")

    rows = await svc.get(tenant_id=TENANT, ids=[a.id])
    assert [r.id for r in rows] == [a.id]


async def test_approve_resumes_a_waiting_job_and_refreshes_snapshot(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "waiting_approval")  # runner paused on approval gate

    updated = await svc.approve(tenant_id=TENANT, task_id=task.id)
    # waiting_approval -> queued: the job is requeued for the poller to re-run,
    # so the task snapshot reads back as pending.
    assert updated.status == "pending"

    # snapshot is persisted, visible to a subsequent read
    (fetched,) = await svc.get(tenant_id=TENANT, ids=[task.id])
    assert fetched.status == "pending"


async def test_approve_twice_fails_precondition(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "waiting_approval")
    await svc.approve(tenant_id=TENANT, task_id=task.id)

    with pytest.raises(StateError):
        await svc.approve(tenant_id=TENANT, task_id=task.id)


async def test_retry_requeues_a_failed_job(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "failed")  # a worker failed the job

    updated = await svc.retry(tenant_id=TENANT, task_id=task.id)
    assert updated.status == "pending"  # requeued -> pending


async def test_retry_on_non_failed_job_fails_precondition(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")  # still queued

    with pytest.raises(StateError):
        await svc.retry(tenant_id=TENANT, task_id=task.id)


async def test_approve_unknown_task_not_found(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    with pytest.raises(NotFoundError):
        await svc.approve(tenant_id=TENANT, task_id="nope")


async def test_approve_other_tenant_task_not_found(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=OTHER, input="go")
    with pytest.raises(NotFoundError):
        await svc.approve(tenant_id=TENANT, task_id=task.id)


async def test_concurrent_creates_persist_all_distinct_tasks(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    results = await asyncio.gather(
        *(svc.create(tenant_id=TENANT, input=f"task-{i}") for i in range(20))
    )
    ids = {r.id for r in results}
    job_ids = {r.job_id for r in results}
    assert len(ids) == 20 and len(job_ids) == 20  # no lost writes, no collisions

    rows = await svc.get(tenant_id=TENANT, ids=[])
    assert len(rows) == 20


async def test_concurrent_approvals_only_one_wins(sessions) -> None:
    from tests.conftest import FakeJobGateway

    # delay forces the racing approvals to interleave on the event loop.
    jobs = FakeJobGateway(delay=0.01)
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "waiting_approval")

    outcomes = await asyncio.gather(
        *(svc.approve(tenant_id=TENANT, task_id=task.id) for _ in range(10)),
        return_exceptions=True,
    )
    wins = [o for o in outcomes if not isinstance(o, Exception)]
    losses = [o for o in outcomes if isinstance(o, StateError)]
    assert len(wins) == 1
    assert len(losses) == 9

    (fetched,) = await svc.get(tenant_id=TENANT, ids=[task.id])
    assert fetched.status == "pending"  # waiting_approval -> queued -> pending
