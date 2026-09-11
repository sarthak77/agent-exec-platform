"""Tests for JobPoller: observe-only behaviour with no runner, claim-then-run
with a runner, batching, runner-failure isolation, and prompt shutdown."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from job_svc.models import JobRow, now
from job_svc.poller import JobPoller
from job_svc.services.jobs import JobService

from tests.conftest import claim_one

TENANT = "t1"
SPEC = {"agent_execution_spec": {"name": "demo"}}


async def _create(service: JobService, *, tenant_id=TENANT):
    return await service.create(
        tenant_id=tenant_id, type="agent_execution", spec=SPEC, max_attempts=None
    )


async def _queued_ids(service: JobService):
    return {r.id for r in await service.list_queued(limit=100)}


async def _backdate_lease(sessions, job_id: str, *, seconds: float) -> None:
    # Push a claimed job's lease into the past so the reaper treats it as
    # abandoned, standing in for a pod that died `seconds` ago mid-run.
    async with sessions.begin() as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(locked_at=now() - timedelta(seconds=seconds))
        )


# --- observe-only (no runner) ----------------------------------------------


async def test_tick_without_runner_does_not_claim(service) -> None:
    job = await _create(service)
    poller = JobPoller(service, interval_seconds=0.01, batch_size=10, lease_seconds=60, runner=None)

    claimed = await poller.tick()

    assert claimed == 0
    assert await _queued_ids(service) == {job.id}  # still queued, untouched


# --- claim-then-run --------------------------------------------------------


async def test_tick_claims_and_runs_each_queued_job(service) -> None:
    a = await _create(service)
    b = await _create(service)
    seen: list[JobRow] = []

    async def runner(row: JobRow) -> None:
        seen.append(row)

    poller = JobPoller(service, interval_seconds=0.01, batch_size=10, lease_seconds=60, runner=runner)
    claimed = await poller.tick()

    assert claimed == 2
    assert {r.id for r in seen} == {a.id, b.id}
    assert all(r.status == "running" and r.attempts == 1 for r in seen)
    assert await _queued_ids(service) == set()  # backlog drained


async def test_tick_respects_batch_size(service) -> None:
    for _ in range(5):
        await _create(service)

    async def runner(row: JobRow) -> None:
        return None

    poller = JobPoller(service, interval_seconds=0.01, batch_size=2, lease_seconds=60, runner=runner)
    assert await poller.tick() == 2
    assert len(await _queued_ids(service)) == 3  # only one batch drained


async def test_runner_failure_is_isolated(service) -> None:
    await _create(service)
    await _create(service)
    calls = 0

    async def runner(row: JobRow) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    poller = JobPoller(service, interval_seconds=0.01, batch_size=10, lease_seconds=60, runner=runner)
    # A raising runner must not abort the batch nor propagate out of tick().
    claimed = await poller.tick()

    assert claimed == 2
    assert calls == 2


# --- run loop / shutdown ---------------------------------------------------


async def test_run_loop_processes_then_stops_promptly(service) -> None:
    await _create(service)
    seen: list[str] = []

    async def runner(row: JobRow) -> None:
        seen.append(row.id)

    poller = JobPoller(service, interval_seconds=0.02, batch_size=10, lease_seconds=60, runner=runner)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))

    # Give it a couple of ticks to drain the backlog, then stop.
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(seen) == 1  # the single queued job was run exactly once


async def test_run_loop_survives_a_failing_tick(service, monkeypatch) -> None:
    poller = JobPoller(service, interval_seconds=0.02, batch_size=10, lease_seconds=60, runner=None)

    calls = 0
    real_tick = poller.tick

    async def flaky_tick() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return await real_tick()

    monkeypatch.setattr(poller, "tick", flaky_tick)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert calls >= 2  # kept polling after the first tick raised


# --- reaping abandoned runs (crash recovery) -------------------------------


async def test_tick_reaps_and_reruns_stranded_running_job(service, sessions) -> None:
    job = await _create(service)
    await claim_one(service)  # queued -> running, fresh lease
    await _backdate_lease(sessions, job.id, seconds=120)  # older than the lease
    seen: list[str] = []

    async def runner(row: JobRow) -> None:
        seen.append(row.id)

    poller = JobPoller(service, interval_seconds=0.01, batch_size=10, lease_seconds=60, runner=runner)
    # One tick both reaps the stranded job (running -> queued) and re-claims it.
    claimed = await poller.tick()

    assert claimed == 1
    assert seen == [job.id]
    reclaimed = await _at(service, job.id)
    assert reclaimed.status == "running"
    assert reclaimed.attempts == 2  # first claim + the reclaim after reaping


async def test_tick_does_not_reap_fresh_running_job(service) -> None:
    job = await _create(service)
    await claim_one(service)  # running, lease well within the window
    seen: list[str] = []

    async def runner(row: JobRow) -> None:
        seen.append(row.id)

    poller = JobPoller(service, interval_seconds=0.01, batch_size=10, lease_seconds=60, runner=runner)
    claimed = await poller.tick()

    assert claimed == 0  # nothing queued; the fresh run is left alone
    assert seen == []
    assert (await _at(service, job.id)).status == "running"


async def _at(service: JobService, job_id: str):
    (row,) = await service.get(tenant_id=TENANT, ids=[job_id], statuses=[], types=[])
    return row
