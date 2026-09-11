"""gRPC server bootstrap."""

from __future__ import annotations

import asyncio
import logging
import signal

import grpc
from grpc_reflection.v1alpha import reflection

from aep.job.v1 import service_pb2, service_pb2_grpc
from job_svc.config import settings
from job_svc.db import Sessions, engine, init_models
from job_svc.orchestrator_client import OrchestratorClient
from job_svc.poller import JobPoller
from job_svc.runner import JobRunner, RunnerDispatcher
from job_svc.servicer import JobServicer
from job_svc.services.jobs import JobService

logger = logging.getLogger(__name__)


async def serve() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_models()

    stop_event = asyncio.Event()

    server = grpc.aio.server()
    service_pb2_grpc.add_JobServiceServicer_to_server(
        JobServicer(
            Sessions,
            default_max_attempts=settings.jobs.default_max_attempts,
            default_max_retries=settings.jobs.default_max_retries,
        ),
        server,
    )

    service_names = (
        service_pb2.DESCRIPTOR.services_by_name["JobService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

    address = f"{settings.grpc.host}:{settings.grpc.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("job_svc listening on %s", address)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # Background poller draining the queued backlog: it claims each queued job
    # and hands it to a per-type runner (via the dispatcher), which decomposes
    # the prompt via the orchestrator, executes each sub-prompt, and drives the
    # job to a terminal status. See poller.py / runner.py.
    poller_task = None
    orchestrator: OrchestratorClient | None = None
    if settings.poller.enabled:
        orchestrator = OrchestratorClient()
        jobs = JobService(
            Sessions,
            default_max_attempts=settings.jobs.default_max_attempts,
            default_max_retries=settings.jobs.default_max_retries,
        )
        # Register a runner per job type; the dispatcher picks the right one off
        # each claimed job's type. A type with no runner is failed by the
        # dispatcher rather than run.
        # Heartbeat at a third of the lease so a run tolerates a couple of
        # missed renewals before the reaper would consider it abandoned.
        agent_runner = JobRunner(
            jobs, orchestrator, heartbeat_interval_seconds=settings.poller.lease_seconds / 3
        )
        dispatcher = RunnerDispatcher(jobs, {"agent_execution": agent_runner.run})
        poller = JobPoller(
            jobs,
            interval_seconds=settings.poller.interval_seconds,
            batch_size=settings.poller.batch_size,
            lease_seconds=settings.poller.lease_seconds,
            runner=dispatcher.run,
        )
        poller_task = asyncio.create_task(poller.run(stop_event))

    await stop_event.wait()
    logger.info("shutting down")
    if poller_task is not None:
        await poller_task  # observes stop_event and exits promptly
    if orchestrator is not None:
        await orchestrator.close()
    await server.stop(grace=5)
    await engine.dispose()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
