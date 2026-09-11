"""JobRunner — the per-job execution logic the poller dispatches to.

Flow for one (already-claimed, i.e. running) job:

  1. Decompose: ask the orchestrator to break the job's prompt into an ordered
     list of simple, independently-executable sub-prompts (the "plan").
  2. Execute: run each sub-prompt as its own orchestrator call, in order,
     recording its output as a step.
  3. Finalize: mark the job succeeded once every step is done.

Progress, idempotency and resume
---------------------------------
Every checkpoint is persisted to the job's `progress` column (see models.py),
which survives a failed -> queued -> running retry untouched. So a re-run of a
job that previously failed part-way resumes rather than restarting:

  - The plan is computed once and checkpointed. A resumed run reuses the stored
    plan and never re-decomposes -- otherwise the LLM could return a *different*
    breakdown and the already-completed steps would no longer align.
  - Each step's output is checkpointed the moment it succeeds. A resumed run
    skips any step already present in `progress["steps"]`, so a completed step
    is never executed twice. This is the "execute from the last successful step"
    guarantee: a step runs at most once across all attempts.

On any failure the job is moved to `failed` (which job_svc auto-escalates to
`dead` once the retry budget, consumed at claim time, is exhausted). The runner
never lets an exception escape to the poller loop -- a failed job is a normal
outcome, not a poller fault.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable

from job_svc.models import JobRow
from job_svc.orchestrator_client import ChatTurn, OrchestratorGateway
from job_svc.services.jobs import JobService

logger = logging.getLogger(__name__)

# A per-type runner: given a freshly-claimed (running) job, execute it and own
# its terminal transition. Matches the poller's `Runner` callable.
TypeRunner = Callable[[JobRow], Awaitable[None]]

# The planner turn: instruct the orchestrator to return an ordered list of
# simple, independently-executable sub-prompts. We ask for a JSON array for
# reliable parsing but tolerate a plain (numbered/bulleted) list as a fallback.
_PLANNER_INSTRUCTION = (
    "You are a task planner. Break the user's request into a list of simple "
    "prompts, each one self-contained and independently executable by an agent, "
    "ordered so they can be run in sequence. Return ONLY a JSON array of "
    "strings (each string one prompt), in the order they should be executed, "
    "with no surrounding text."
)

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*")

# finish_reason the orchestrator returns when a tool paused mid-turn on a
# human-approval gate (see orchestrator/run.py). The runner treats this not as
# a failure but as a pause: it parks the job at `waiting_approval` and does NOT
# checkpoint the step, so once approved the step re-runs and can complete. Must
# match orchestrator/run.py's APPROVAL_FINISH_REASON.
APPROVAL_FINISH_REASON = "requires_approval"


class RunnerError(Exception):
    """A job could not be run to completion (bad spec, empty plan, ...). Caught
    by the runner itself and turned into a failed job."""


class ApprovalRequired(Exception):
    """Raised mid-execution when a step paused on a human-approval gate. Caught
    by `run` and turned into a `waiting_approval` job (not a failure)."""


class JobRunner:
    def __init__(self, jobs: JobService, orchestrator: OrchestratorGateway) -> None:
        self._jobs = jobs
        self._orchestrator = orchestrator

    async def run(self, row: JobRow) -> None:
        """Entry point matching the poller's Runner callable. `row` is a freshly
        claimed job (already running). Owns the job's terminal transition."""
        try:
            await self._execute(row)
        except ApprovalRequired:
            logger.info("job %s paused awaiting approval", row.id)
            await self._mark_waiting_approval(row)
        except Exception:
            logger.exception("job %s failed; marking failed", row.id)
            await self._mark_failed(row)

    async def _execute(self, row: JobRow) -> None:
        prompt = self._prompt_of(row)
        # Copy so we never mutate the ORM row's attribute in place; the column is
        # rewritten wholesale by save_progress.
        progress: dict = dict(row.progress or {})

        plan = await self._ensure_plan(row, prompt, progress)
        await self._execute_steps(row, plan, progress)
        await self._finalize(row, progress)

    async def _ensure_plan(self, row: JobRow, prompt: str, progress: dict) -> list[str]:
        existing = progress.get("plan")
        if existing:
            # Resume: reuse the checkpointed plan, never re-decompose.
            return existing

        reply = await self._orchestrator.chat(
            tenant_id=row.tenant_id,
            messages=[
                ChatTurn(role="system", content=_PLANNER_INSTRUCTION),
                ChatTurn(role="user", content=prompt),
            ],
        )
        plan = _parse_plan(reply.content)
        if not plan:
            raise RunnerError(f"planner returned no sub-prompts for job {row.id}")

        progress["plan"] = plan
        progress["phase"] = "executing"
        progress.setdefault("steps", {})
        await self._jobs.save_progress(job_id=row.id, progress=progress)
        return plan

    async def _execute_steps(self, row: JobRow, plan: list[str], progress: dict) -> None:
        steps: dict = progress.setdefault("steps", {})
        for index, sub_prompt in enumerate(plan):
            key = str(index)
            if key in steps:
                continue  # already completed on a prior attempt -> skip (idempotent)

            reply = await self._orchestrator.chat(
                tenant_id=row.tenant_id,
                messages=[ChatTurn(role="user", content=sub_prompt)],
            )
            if reply.finish_reason == APPROVAL_FINISH_REASON:
                # A tool paused this step on a human-approval gate. Record the
                # pending step for observability but deliberately do NOT add it
                # to `steps`: it hasn't completed, so once approved it must
                # re-run (the resume path skips only completed steps). Signal a
                # pause -- not a failure -- to `run`.
                progress["pending_approval"] = {
                    "step": index,
                    "prompt": sub_prompt,
                    "detail": reply.content,
                }
                await self._jobs.save_progress(job_id=row.id, progress=progress)
                raise ApprovalRequired
            # The step completed (e.g. re-run after an approval), so clear any
            # stale pending-approval marker from a prior paused attempt.
            progress.pop("pending_approval", None)
            steps[key] = {
                "prompt": sub_prompt,
                "output": reply.content,
                "finish_reason": reply.finish_reason,
            }
            # Checkpoint immediately: a crash after this point must not re-run the
            # step on the next attempt.
            await self._jobs.save_progress(job_id=row.id, progress=progress)

    async def _finalize(self, row: JobRow, progress: dict) -> None:
        progress["phase"] = "completed"
        await self._jobs.save_progress(job_id=row.id, progress=progress)
        await self._jobs.update(tenant_id=row.tenant_id, job_id=row.id, status="succeeded")

    async def _mark_waiting_approval(self, row: JobRow) -> None:
        # running -> waiting_approval: the job pauses (poller/reaper ignore this
        # status) until an approving caller requeues it (see job_svc state
        # machine). The attempt consumed at claim is NOT refunded, so approval
        # cycles draw down the retry budget like any other run.
        await self._jobs.update(
            tenant_id=row.tenant_id, job_id=row.id, status="waiting_approval"
        )

    async def _mark_failed(self, row: JobRow) -> None:
        # running -> failed (job_svc escalates to dead when the budget, consumed
        # at claim, is exhausted). If the job is somehow no longer running, let
        # the resulting StateError surface to the poller's safety net.
        await self._jobs.update(tenant_id=row.tenant_id, job_id=row.id, status="failed")

    @staticmethod
    def _prompt_of(row: JobRow) -> str:
        spec = row.spec or {}
        prompt = (spec.get("agent_execution_spec") or {}).get("instructions")
        if not prompt or not prompt.strip():
            raise RunnerError(f"job {row.id} spec has no agent_execution_spec.instructions")
        return prompt


class RunnerDispatcher:
    """Routes each claimed job to the runner registered for its type.

    The poller hands every claimed job to a single `Runner` callable; this
    dispatcher is that callable. It looks the job's `type` up in a
    type -> runner registry and delegates, so distinct job types (agent
    execution, mutation, ...) can be executed by distinct runners while the
    poller itself stays type-agnostic. `type` is the domain string stored on
    the row (see mappers.TYPE_FROM_PROTO), so the registry is keyed by those
    same values.

    A job whose type has no registered runner cannot be executed, so it is
    marked failed (and dead-lettered once its retry budget is spent) rather than
    left running until its lease is reaped -- a fast, deterministic outcome for
    a job type nothing is wired to handle.
    """

    def __init__(self, jobs: JobService, runners: dict[str, TypeRunner]) -> None:
        self._jobs = jobs
        self._runners = dict(runners)

    async def run(self, row: JobRow) -> None:
        runner = self._runners.get(row.type)
        if runner is None:
            logger.error(
                "no runner registered for job %s of type %r; marking failed",
                row.id,
                row.type,
            )
            await self._jobs.update(tenant_id=row.tenant_id, job_id=row.id, status="failed")
            return
        await runner(row)


def _parse_plan(content: str) -> list[str]:
    """Parse the planner's reply into an ordered list of sub-prompts.

    Prefers a JSON array of strings; falls back to treating each non-empty line
    as a prompt (stripping leading bullets / numbering) so a group chat that
    wraps its answer in prose still yields a usable plan.
    """
    text = content.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]

    lines = [_BULLET_RE.sub("", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]
