"""gRPC server bootstrap."""

from __future__ import annotations

import asyncio
import logging
import signal

import grpc
from grpc_reflection.v1alpha import reflection

from aep.orchestrator.v1 import service_pb2, service_pb2_grpc
from orchestrator.config import settings
from orchestrator.gateway_client import close_gateway_channel
from orchestrator.servicer import OrchestratorServicer

logger = logging.getLogger(__name__)


async def serve() -> None:
    logging.basicConfig(level=logging.INFO)

    server = grpc.aio.server()
    service_pb2_grpc.add_OrchestratorServiceServicer_to_server(OrchestratorServicer(), server)

    service_names = (
        service_pb2.DESCRIPTOR.services_by_name["OrchestratorService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

    address = f"{settings.grpc.host}:{settings.grpc.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("orchestrator listening on %s", address)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info("shutting down")
    await server.stop(grace=5)
    await close_gateway_channel()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
