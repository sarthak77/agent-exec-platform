"""Process-wide shared gRPC channel + stub to the gateway service.

One channel is reused across every request rather than opened and closed per
`Chat` call: grpc channels multiplex concurrent RPCs, so a shared channel
avoids per-request connection setup while staying safe under the server's
single asyncio loop. Created lazily on first use (an insecure_channel must be
constructed with a running event loop) and closed once on shutdown by main.py.
"""

from __future__ import annotations

import grpc

from aep.gateway.v1 import service_pb2_grpc
from orchestrator.config import settings

_channel: grpc.aio.Channel | None = None
_stub: service_pb2_grpc.GatewayServiceStub | None = None


def get_gateway_stub() -> service_pb2_grpc.GatewayServiceStub:
    # No await before assignment, so under a single event loop the first
    # caller fully initializes the singleton before any other can observe it.
    global _channel, _stub
    if _stub is None:
        _channel = grpc.aio.insecure_channel(f"{settings.gateway.host}:{settings.gateway.port}")
        _stub = service_pb2_grpc.GatewayServiceStub(_channel)
    return _stub


async def close_gateway_channel() -> None:
    global _channel, _stub
    if _channel is not None:
        await _channel.close()
        _channel = None
        _stub = None
