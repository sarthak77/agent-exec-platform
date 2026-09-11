"""Background poller that drains the `queued` backlog.

Runs as an asyncio task alongside the gRPC server (launched in main.serve),
sharing its event loop and DB session factory. On each tick it reaps abandoned
runs, atomically claims a batch of the oldest queued jobs (queued -> running,
consuming an attempt each via JobService.claim_batch), and hands each claimed row
to a `runner` callback -- the per-job execution logic (see runner.py).

Horizontal scaling: the poller runs on every pod. Claiming goes through
`claim_batch` (SELECT ... FOR UPDATE SKIP LOCKED), so each pod takes a disjoint
batch rather than all pods contending for the same head-of-queue rows. Each
poller also carries a unique `owner` id stamped onto the jobs it claims, and
reaps leases that have expired -- so if the pod holding a job dies, another
pod's reaper requeues it (resuming from the job's progress checkpoint) instead of
leaving it stranded in `running`.

Until a runner is wired in, the poller stays observe-only: it lists the queued
backlog and logs it but does NOT claim or reap anything, so jobs are not moved
to `running` (and attempts consumed) with nothing to actually run them. Once a
runner is provided, reap-then-claim-then-run kicks in.

The loop is driven off a stop Event so shutdown is prompt: it waits out the
interval on `stop_event.wait()` rather than a bare sleep, and exits as soon as
the event is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from uuid import uuid4

from job_svc.models import JobRow
from job_svc.services.jobs import JobService

logger = logging.getLogger(__name__)

# A runner takes a freshly-claimed (already running) job and executes it. It
# owns reporting the terminal status back via JobService (succeeded/failed/etc).
Runner = Callable[[JobRow], Awaitable[None]]


def _default_owner() -> str:
    # Stable-ish, human-readable, and unique per process so a claim's owner is
    # traceable to a pod while still distinguishing restarts on the same host.
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


class JobPoller:
    def __init__(
        self,
        jobs: JobService,
        *,
        interval_seconds: float,
        batch_size: int,
        lease_seconds: float,
        runner: Runner | None = None,
        owner: str | None = None,
    ) -> None:
        self._jobs = jobs
        self._interval = interval_seconds
        self._batch = batch_size
        self._lease_seconds = lease_seconds
        self._runner = runner
        self._owner = owner or _default_owner()

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "job poller started (owner=%s interval=%ss batch=%d lease=%ss runner=%s)",
            self._owner,
            self._interval,
            self._batch,
            self._lease_seconds,
            "set" if self._runner else "none",
        )
        while not stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                # One bad tick must not kill the loop; log and keep polling.
                logger.exception("job poller tick failed")
            await self._wait_interval(stop_event)
        logger.info("job poller stopped")

    async def tick(self) -> int:
        """Process one batch of queued jobs. Returns the number of jobs claimed
        and dispatched (0 when observe-only or the backlog is empty)."""
        if self._runner is None:
            queued = await self._jobs.list_queued(limit=self._batch)
            if queued:
                logger.info("job poller: %d queued job(s) waiting; no runner wired", len(queued))
            return 0

        # Recover jobs stranded in `running` by a pod that died mid-run before
        # claiming any new work -- a cheap, indexed UPDATE that is safe to run
        # from every pod (atomic, so each stale job is reclaimed exactly once).
        reaped = await self._jobs.reap_expired(lease_seconds=self._lease_seconds)
        if reaped:
            logger.info("job poller: reaped %d expired running job(s)", len(reaped))

        # Claim a disjoint batch (FOR UPDATE SKIP LOCKED) so multiple poller pods
        # do not contend for the same head-of-queue rows.
        claimed = await self._jobs.claim_batch(limit=self._batch, owner=self._owner)
        for row in claimed:
            await self._dispatch(row)
        return len(claimed)

    async def _dispatch(self, row: JobRow) -> None:
        assert self._runner is not None  # guarded by caller
        try:
            await self._runner(row)
        except Exception:
            # A runner failure is a job outcome, not a poller fault -- surfacing
            # it as a failed/dead transition is the runner's responsibility, so
            # here we just log and move on to the rest of the batch.
            logger.exception("job runner failed for job %s", row.id)

    async def _wait_interval(self, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
        except asyncio.TimeoutError:
            pass
