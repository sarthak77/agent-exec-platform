"""MCP server exposing the `tools` table as live MCP tools.

`list_tools` queries Postgres directly on every call (no in-process cache),
so a tool created via agent_execution_service is visible here immediately.
The catalog itself carries no execution details (just name + description); the
actual behaviour of a tool lives in code, in handlers.py. A catalog tool that
has a registered handler advertises that handler's input schema and executes
it on `call_tool`; a catalog tool with no handler reports "no execution
binding" rather than pretending to execute.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette

from mcp_svc.auth import tenant_id_from_context
from mcp_svc.config import settings
from mcp_svc.db import Sessions, engine
from mcp_svc.errors import AuthenticationError
from mcp_svc.handlers import ToolContext, get_handler
from mcp_svc.tools import get_tool_by_name, list_tools_for_tenant

logger = logging.getLogger(__name__)

_EMPTY_INPUT_SCHEMA = {"type": "object", "properties": {}}


def _tenant_id(ctx: ServerRequestContext) -> str:
    """Resolve the tenant, surfacing a missing header as a clean MCP error
    (INVALID_REQUEST) rather than an opaque internal error."""
    try:
        return tenant_id_from_context(ctx)
    except AuthenticationError as exc:
        raise MCPError(code=types.INVALID_REQUEST, message=str(exc)) from exc


async def _on_list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    tenant_id = _tenant_id(ctx)
    async with Sessions() as session:
        rows = await list_tools_for_tenant(session, tenant_id)

    tools: list[types.Tool] = []
    for row in rows:
        handler = get_handler(row.name)
        # A registered handler owns the argument schema (and a richer default
        # description); a catalog tool with no handler is still listed, with an
        # empty schema, so it's discoverable even though it can't be executed.
        input_schema = handler.input_schema if handler else _EMPTY_INPUT_SCHEMA
        description = row.description or (handler.description if handler else "")
        tools.append(
            types.Tool(name=row.name, description=description, inputSchema=input_schema)
        )
    return types.ListToolsResult(tools=tools)


def _error_result(text: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=True,
    )


async def _on_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    tenant_id = _tenant_id(ctx)
    # Gate on the catalog first: the tenant must actually own a tool with this
    # name before we'll dispatch to its (shared, code-defined) handler.
    async with Sessions() as session:
        tool = await get_tool_by_name(session, tenant_id, params.name)
    if tool is None:
        return _error_result(f"tool {params.name!r} not found in the catalog")

    handler = get_handler(params.name)
    if handler is None:
        return _error_result(
            f"tool {params.name!r} has no execution binding configured"
        )

    try:
        result = await handler.run(dict(params.arguments or {}), ToolContext(tenant_id=tenant_id))
    except Exception as exc:
        logger.exception("tool %r execution failed", params.name)
        return _error_result(f"tool {params.name!r} failed: {exc}")

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result)],
    )


def build_server() -> Server:
    return Server("mcp_svc", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


def _transport_security() -> TransportSecuritySettings:
    sec = settings.security
    if sec.allowed_hosts or sec.allowed_origins:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(sec.allowed_hosts),
            allowed_origins=list(sec.allowed_origins),
        )
    # No allowlist configured: the service is expected to sit behind a trusted
    # edge, so disable Host/Origin checks explicitly rather than by accident.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def build_app() -> Starlette:
    """Build the streamable-HTTP ASGI app.

    Transport security (Host/Origin validation) comes from `[security]`; the
    bind host/port are uvicorn's concern (see main.py), not passed here. The
    DB engine is disposed on shutdown by wrapping the transport's own lifespan.
    """
    app = build_server().streamable_http_app(transport_security=_transport_security())

    inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[Any]:
        try:
            async with inner_lifespan(app) as state:
                yield state
        finally:
            await engine.dispose()

    app.router.lifespan_context = lifespan
    return app
