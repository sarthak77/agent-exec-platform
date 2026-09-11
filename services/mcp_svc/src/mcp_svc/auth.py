"""Tenant-identity extraction from the MCP request context.

Mirrors agent_execution_service/auth.py: RBAC is assumed to happen upstream,
so this only extracts the tenant id every query is scoped by, standing in
for an edge that forwards a verified `x-tenant-id` header.
"""

from __future__ import annotations

from mcp.server.context import ServerRequestContext

from mcp_svc.errors import AuthenticationError


def tenant_id_from_context(ctx: ServerRequestContext) -> str:
    headers = getattr(ctx.request, "headers", None) or {}
    tenant_id = headers.get("x-tenant-id")
    if not tenant_id:
        raise AuthenticationError("missing x-tenant-id header")
    return tenant_id
