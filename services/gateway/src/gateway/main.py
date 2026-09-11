"""gRPC server bootstrap."""

from __future__ import annotations

import asyncio
import logging
import signal

import grpc
from grpc_reflection.v1alpha import reflection

from aep.gateway.v1 import service_pb2, service_pb2_grpc
from gateway.config import get_settings
from gateway.provider import OpenAIProvider
from gateway.provider_zen import ZenProvider
from gateway.servicer import GatewayServicer

logger = logging.getLogger(__name__)


async def serve() -> None:
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    # The opencode test provider speaks the Zen Responses API directly; every
    # other provider is OpenAI-wire-compatible and goes through the SDK.
    if settings.model.provider == "opencode":
        provider = ZenProvider(api_key=settings.api_key, model=settings.model)
    else:
        provider = OpenAIProvider(api_key=settings.api_key, model=settings.model)
    servicer = GatewayServicer(provider, guardrails=settings.guardrails)

    server = grpc.aio.server()
    service_pb2_grpc.add_GatewayServiceServicer_to_server(servicer, server)

    service_names = (
        service_pb2.DESCRIPTOR.services_by_name["GatewayService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

    address = f"{settings.grpc.host}:{settings.grpc.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("gateway listening on %s", address)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    logger.info("shutting down")
    await server.stop(grace=5)


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
