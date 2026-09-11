"""gRPC server bootstrap."""

from __future__ import annotations

import asyncio
import logging
import signal

import grpc
from grpc_reflection.v1alpha import reflection

from aep.agent_execution.v1 import service_pb2, service_pb2_grpc
from agent_execution_service.config import settings
from agent_execution_service.db import Sessions, init_models
from agent_execution_service.job_client import JobClient
from agent_execution_service.servicer import AgentExecutionServicer

logger = logging.getLogger(__name__)


async def serve() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_models()

    jobs = JobClient()
    server = grpc.aio.server()
    service_pb2_grpc.add_AgentExecutionServiceServicer_to_server(
        AgentExecutionServicer(Sessions, jobs), server
    )

    service_names = (
        service_pb2.DESCRIPTOR.services_by_name["AgentExecutionService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

    address = f"{settings.grpc.host}:{settings.grpc.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("agent_execution_service listening on %s", address)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info("shutting down")
    await server.stop(grace=5)
    await jobs.close()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
