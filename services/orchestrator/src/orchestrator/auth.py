"""Tenant-identity extraction from gRPC metadata.

RBAC/permission checking is assumed to happen upstream of this service (an
already-authenticated edge authorizes the caller before the request ever
reaches here), so this module does not decide who is allowed to call what.
It only extracts the tenant id every query is scoped by, mirroring
agent_execution_service/auth.py's convention.
"""

from __future__ import annotations

import grpc

from orchestrator.errors import AuthenticationError


def tenant_id_from_metadata(context: grpc.aio.ServicerContext) -> str:
    md = dict(context.invocation_metadata() or ())
    tenant_id = md.get("x-tenant-id")
    if not tenant_id:
        raise AuthenticationError("missing x-tenant-id metadata")
    return tenant_id
