"""Tests for JobRunner: decomposition, ordered step execution, per-step
checkpointing, terminal transitions, and -- the crux -- idempotent resume from
the last successful step across a failed/requeued retry."""

from __future__ import annotations

import pytest

from job_svc.poller import JobPoller
from job_svc.runner import JobRunner, RunnerDispatcher, _parse_plan
from job_svc.services.jobs import JobService

from tests.conftest import FakeOrchestrator, claim_one

TENANT = "t1"


async def _claimed(service: JobService, *, instructions: str = "do the big thing", max_attempts=3):
    """Create a job and claim it (queued -> running) so the runner receives a
    running row, exactly as the poller would hand it over."""
    job = await service.create(
        tenant_id=TENANT,
        type="agent_execution",
        spec={"agent_execution_spec": {"instructions": instructions}},
        max_attempts=max_attempts,
    )
    return await claim_one(service)


async def _fetch(service: JobService, job_id: str):
    (row,) = await service.get(tenant_id=TENANT, ids=[job_id], statuses=[], types=[])
    return row


# --- happy path ------------------------------------------------------------


async def test_runs_all_steps_and_succeeds(service, orchestrator) -> None:
    orchestrator.plan = ["a", "b", "c"]
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service)

    await runner.run(row)

    done = await _fetch(service, row.id)
    assert done.status == "succeeded"
    assert done.progress["phase"] == "completed"
    assert done.progress["plan"] == ["a", "b", "c"]
    assert [done.progress["steps"][str(i)]["output"] for i in range(3)] == [
        "done: a",
        "done: b",
        "done: c",
    ]
    # One planning call, then one execution call per sub-prompt, in order.
    assert orchestrator.plan_calls == 1
    assert orchestrator.exec_calls == ["a", "b", "c"]


async def test_plan_is_decomposition_of_the_job_prompt(service, orchestrator) -> None:
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service, instructions="build me a house")

    await runner.run(row)

    # The job's prompt is forwarded to the planner as the user turn.
    assert orchestrator.calls[0] == ("plan", "build me a house")


# --- failure + checkpointing ----------------------------------------------


async def test_failure_mid_execution_checkpoints_completed_steps(service, orchestrator) -> None:
    orchestrator.plan = ["a", "b", "c"]
    orchestrator.fail_on = {"b"}  # second step blows up
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service)

    await runner.run(row)

    failed = await _fetch(service, row.id)
    assert failed.status == "failed"
    assert failed.attempts == 1  # consumed by claim; budget (3) not yet spent
    # Step a is checkpointed; b/c are not, so a resume re-runs from b.
    assert set(failed.progress["steps"]) == {"0"}
    assert failed.progress["steps"]["0"]["output"] == "done: a"
    assert failed.progress["plan"] == ["a", "b", "c"]


async def test_planning_failure_marks_job_failed_with_no_plan(service, orchestrator) -> None:
    orchestrator.fail_on = {"plan"}
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service)

    await runner.run(row)

    failed = await _fetch(service, row.id)
    assert failed.status == "failed"
    assert "plan" not in failed.progress  # nothing checkpointed


async def test_empty_plan_fails_job(service, orchestrator) -> None:
    orchestrator.plan = []
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service)

    await runner.run(row)

    assert (await _fetch(service, row.id)).status == "failed"


async def test_missing_instructions_fails_job(service, orchestrator) -> None:
    job = await service.create(
        tenant_id=TENANT, type="agent_execution", spec={}, max_attempts=3
    )
    row = await claim_one(service)
    runner = JobRunner(service, orchestrator)

    await runner.run(row)

    assert (await _fetch(service, row.id)).status == "failed"
    assert orchestrator.calls == []  # never reached the orchestrator


# --- resume / idempotency --------------------------------------------------


async def test_resume_after_failure_skips_completed_steps(service) -> None:
    # Attempt 1: step "b" fails after "a" succeeds.
    first = FakeOrchestrator(plan=["a", "b", "c"], fail_on={"b"})
    runner = JobRunner(service, first)
    row = await _claimed(service)
    await runner.run(row)
    assert (await _fetch(service, row.id)).status == "failed"

    # Requeue (as RetryJob would) and re-claim, preserving the checkpoint.
    await service.retry(tenant_id=TENANT, job_id=row.id)
    reclaimed = await claim_one(service)
    assert reclaimed.progress["steps"].keys() == {"0"}  # checkpoint carried over

    # Attempt 2: a healthy orchestrator finishes the job.
    second = FakeOrchestrator(plan=["a", "b", "c"])  # would be used only if replanned
    runner2 = JobRunner(service, second)
    await runner2.run(reclaimed)

    done = await _fetch(service, row.id)
    assert done.status == "succeeded"
    # The plan was reused (not recomputed) and step "a" was NOT re-executed:
    # only the previously-failed "b" and the never-run "c" ran on attempt 2.
    assert second.plan_calls == 0
    assert second.exec_calls == ["b", "c"]
    assert [done.progress["steps"][str(i)]["output"] for i in range(3)] == [
        "done: a",  # preserved from attempt 1
        "done: b",
        "done: c",
    ]


async def test_precheckpointed_plan_is_not_recomputed(service, orchestrator) -> None:
    row = await _claimed(service)  # already running
    # Seed a plan (and one completed step) as if a prior attempt had run.
    await service.save_progress(
        job_id=row.id,
        progress={
            "phase": "executing",
            "plan": ["x", "y"],
            "steps": {"0": {"prompt": "x", "output": "old", "finish_reason": "stop"}},
        },
    )
    # Re-fetch so the row handed to the runner carries the seeded checkpoint,
    # exactly as claim() would return it on a resumed attempt.
    reclaimed = await _fetch(service, row.id)

    runner = JobRunner(service, orchestrator)
    await runner.run(reclaimed)

    done = await _fetch(service, row.id)
    assert done.status == "succeeded"
    assert orchestrator.plan_calls == 0  # seeded plan reused
    assert orchestrator.exec_calls == ["y"]  # step 0 skipped, only step 1 ran
    assert done.progress["steps"]["0"]["output"] == "old"  # untouched


# --- plan parsing ----------------------------------------------------------


def test_parse_plan_json_array() -> None:
    assert _parse_plan('["one", "two", "three"]') == ["one", "two", "three"]


def test_parse_plan_json_ignores_blanks() -> None:
    assert _parse_plan('["one", "  ", "two"]') == ["one", "two"]


def test_parse_plan_line_fallback_strips_bullets_and_numbers() -> None:
    text = "1. first\n2) second\n- third\n* fourth"
    assert _parse_plan(text) == ["first", "second", "third", "fourth"]


def test_parse_plan_empty_is_empty() -> None:
    assert _parse_plan("   ") == []
    assert _parse_plan("[]") == []


# --- end-to-end through the poller -----------------------------------------


async def test_poller_claims_and_runs_job_to_completion(service, orchestrator) -> None:
    orchestrator.plan = ["a", "b"]
    job = await service.create(
        tenant_id=TENANT,
        type="agent_execution",
        spec={"agent_execution_spec": {"instructions": "go"}},
        max_attempts=3,
    )
    runner = JobRunner(service, orchestrator)
    poller = JobPoller(
        service, interval_seconds=0.01, batch_size=10, lease_seconds=60, runner=runner.run
    )

    claimed = await poller.tick()

    assert claimed == 1
    done = await _fetch(service, job.id)
    assert done.status == "succeeded"
    assert done.attempts == 1  # claimed once by the poller
    assert orchestrator.plan_calls == 1
    assert orchestrator.exec_calls == ["a", "b"]


# --- human-approval pause / resume -----------------------------------------


async def test_pauses_on_approval_gate_without_checkpointing_the_step(service) -> None:
    orchestrator = FakeOrchestrator(plan=["retrieve", "send"], approve_on={"send"})
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service)

    await runner.run(row)

    paused = await _fetch(service, row.id)
    assert paused.status == "waiting_approval"
    # The completed first step is checkpointed; the paused send step is NOT, so
    # a resume re-runs it.
    assert set(paused.progress["steps"]) == {"0"}
    assert paused.progress["pending_approval"]["step"] == 1
    assert paused.progress["pending_approval"]["prompt"] == "send"


async def test_resume_after_approval_completes_the_job(service) -> None:
    orchestrator = FakeOrchestrator(plan=["retrieve", "send"], approve_on={"send"})
    runner = JobRunner(service, orchestrator)
    row = await _claimed(service)
    await runner.run(row)
    assert (await _fetch(service, row.id)).status == "waiting_approval"

    # Approve: requeue (waiting_approval -> queued) and re-claim, exactly as
    # ApproveTask + the poller would. The send tool is now approved, so it no
    # longer pauses.
    await service.update(tenant_id=TENANT, job_id=row.id, status="queued")
    resumed = await claim_one(service)
    orchestrator.approve_on = set()

    await runner.run(resumed)

    done = await _fetch(service, row.id)
    assert done.status == "succeeded"
    assert set(done.progress["steps"]) == {"0", "1"}
    assert "pending_approval" not in done.progress
    # Across both runs "retrieve" executed exactly once (checkpointed, so the
    # resume skipped it) while "send" ran twice (paused, then re-ran on resume).
    assert orchestrator.exec_calls == ["retrieve", "send", "send"]


# --- dispatch by job type --------------------------------------------------


async def test_dispatcher_routes_to_runner_for_job_type(service) -> None:
    seen: list[str] = []

    async def agent_runner(row) -> None:
        seen.append(row.id)

    dispatcher = RunnerDispatcher(service, {"agent_execution": agent_runner})
    row = await _claimed(service)  # an agent_execution job

    await dispatcher.run(row)

    assert seen == [row.id]  # routed to the agent_execution runner


async def test_dispatcher_fails_job_with_no_runner_for_its_type(service) -> None:
    # A "mutation" job has no registered runner, so the dispatcher must fail it
    # (not leave it running until the reaper picks it up).
    job = await service.create(
        tenant_id=TENANT,
        type="mutation",
        spec={"agent_execution_spec": {"instructions": "mutate"}},
        max_attempts=3,
    )
    row = await claim_one(service)  # queued -> running, attempt 1
    dispatcher = RunnerDispatcher(service, {"agent_execution": lambda r: None})

    await dispatcher.run(row)

    assert (await _fetch(service, job.id)).status == "failed"
