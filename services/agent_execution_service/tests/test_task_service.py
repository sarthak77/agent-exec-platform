"""Tests for TaskService — the task<->job delegation, status snapshotting,
tenant scoping, and concurrency behaviour."""

from __future__ import annotations

import asyncio

import pytest

from agent_execution_service.errors import NotFoundError, StateError
from agent_execution_service.job_client import JobStepRef
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


async def test_get_refreshes_status_and_result_from_job(sessions, jobs) -> None:
    # A freshly created task is pending; its job then runs to completion in
    # job_svc on its own. GetTask must refresh from the authoritative Job and
    # surface both the terminal status and the final result -- not the stale
    # snapshot taken at create time.
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "succeeded")
    jobs.set_result(task.job_id, "the answer is 42")

    (fetched,) = await svc.get(tenant_id=TENANT, ids=[task.id])
    assert fetched.status == "completed"
    assert fetched.result == "the answer is 42"


async def test_get_does_not_refresh_terminal_tasks(sessions, jobs) -> None:
    # Once a task is terminal its result is frozen: a later out-of-band job_svc
    # change must not be pulled in on a subsequent read.
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "succeeded")
    jobs.set_result(task.job_id, "first")
    await svc.get(tenant_id=TENANT, ids=[task.id])  # snapshots completed/"first"

    jobs.set_result(task.job_id, "second")
    (fetched,) = await svc.get(tenant_id=TENANT, ids=[task.id])
    assert fetched.status == "completed"
    assert fetched.result == "first"


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


# -- get_progress: the task-centric progress view ------------------------------


def _step(
    index: int,
    *,
    description: str = "do a thing",
    output: str = "ok",
    agent: str = "Assistant",
    requires_approval: bool = False,
) -> JobStepRef:
    return JobStepRef(
        index=index,
        description=description,
        output=output,
        agent=agent,
        requires_approval=requires_approval,
    )


async def test_get_progress_reports_step_history_and_counts(sessions, jobs) -> None:
    # A running task exposes the complete ordered step history plus normalized
    # counts/percentage derived from the plan size and steps done.
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "running")
    jobs.set_progress(
        task.job_id,
        steps_total=3,
        steps=(_step(0, output="first"), _step(1, output="second")),
    )

    view = await svc.get_progress(tenant_id=TENANT, task_id=task.id)
    assert view.task_id == task.id
    assert view.status == "running"
    assert view.steps_completed == 2
    assert view.steps_total == 3
    assert view.percent_complete == pytest.approx(66.7)
    assert view.summary == "Running: 2 of 3 steps done"
    assert [s.output for s in view.steps] == ["first", "second"]
    assert view.requires_approval is False


async def test_get_progress_completed_is_full_with_output(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "succeeded")
    jobs.set_result(task.job_id, "the answer is 42")
    jobs.set_progress(task.job_id, steps_total=2, steps=(_step(0), _step(1)))

    view = await svc.get_progress(tenant_id=TENANT, task_id=task.id)
    assert view.status == "completed"
    assert view.percent_complete == 100.0
    assert view.output == "the answer is 42"
    assert view.summary == "Completed"


async def test_get_progress_failed_surfaces_error(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "failed")
    jobs.set_progress(task.job_id, steps_total=2, steps=(_step(0),), error="boom")

    view = await svc.get_progress(tenant_id=TENANT, task_id=task.id)
    assert view.status == "failed"
    assert view.error == "boom"
    assert view.summary == "Failed: boom"


async def test_get_progress_waiting_approval_flags_task_and_step(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")
    jobs.set_status(task.job_id, "waiting_approval")
    jobs.set_progress(task.job_id, steps_total=2, steps=(_step(0, requires_approval=True),))

    view = await svc.get_progress(tenant_id=TENANT, task_id=task.id)
    assert view.requires_approval is True
    assert view.steps[0].requires_approval is True
    assert view.summary == "Waiting for approval"


async def test_get_progress_pending_has_no_steps(sessions, jobs) -> None:
    # A freshly created (queued) task has no plan yet: no steps, 0%.
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=TENANT, input="go")

    view = await svc.get_progress(tenant_id=TENANT, task_id=task.id)
    assert view.status == "pending"
    assert view.steps == ()
    assert view.steps_total == 0
    assert view.percent_complete == 0.0
    assert view.summary == "Pending"


async def test_get_progress_unknown_task_not_found(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    with pytest.raises(NotFoundError):
        await svc.get_progress(tenant_id=TENANT, task_id="nope")


async def test_get_progress_other_tenant_task_not_found(sessions, jobs) -> None:
    svc = TaskService(sessions, jobs)
    task = await svc.create(tenant_id=OTHER, input="go")
    with pytest.raises(NotFoundError):
        await svc.get_progress(tenant_id=TENANT, task_id=task.id)
