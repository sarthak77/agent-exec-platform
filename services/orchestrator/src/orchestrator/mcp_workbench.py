"""Per-agent MCP tool workbench.

Each AutoGen agent in the group chat (see groupchat.py) attaches one of these
so it is wired to mcp_svc's tools — scoped to the tool names granted to that
agent (see agents_repo.py). `list_tools` exposes only the granted tools;
`call_tool` refuses anything outside that grant locally and otherwise forwards
to mcp_svc.

Built directly on the mcp 2.x streamable-HTTP client (the same client used by
the old catalog connector): autogen_ext's `McpWorkbench` is unusable under this
service's ``mcp>=2,<3`` pin because it imports the mcp 1.x
``mcp.shared.context.RequestContext``.

This closes the full tool loop: the gateway now supports function calling (see
model_client.py, which forwards these schemas and surfaces the model's tool
calls), and mcp_svc executes tools that have a registered handler (see
mcp_svc/handlers.py). A `call_tool` for a catalog tool with no execution
binding still comes back as mcp_svc's "no execution binding configured"
response. The per-agent instruction restriction added in groupchat.py
constrains which tools an agent may call, in addition to the local `_allowed`
scoping here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from autogen_core import CancellationToken
from autogen_core.tools import (
    ParametersSchema,
    TextResultContent,
    ToolResult,
    ToolSchema,
    Workbench,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Sentinel a tool result carries when the tool refused to act until a human
# approves (e.g. mcp_svc's send_email; see mcp_svc/handlers.py). Detected here
# at the workbench boundary -- off the tool's own result text, not the model's
# paraphrase of it -- and recorded into the run's approval sink so run.py can
# surface a "requires_approval" finish_reason to job_svc. Must match the marker
# mcp_svc emits.
APPROVAL_REQUIRED_MARKER = "APPROVAL_REQUIRED"


@asynccontextmanager
async def _mcp_session(tenant_id: str) -> AsyncIterator[ClientSession]:
    """Open a short-lived, initialized mcp_svc session for a tenant."""
    http_client = create_mcp_http_client(headers={"x-tenant-id": tenant_id})
    async with streamable_http_client(settings.mcp.url, http_client=http_client) as (
        read,
        write,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _render_content(content: Sequence[Any]) -> str:
    """Flatten an mcp tool result's content blocks into plain text."""
    parts: list[str] = []
    for item in content or []:
        if isinstance(item, TextContent):
            parts.append(item.text)
        else:
            parts.append(str(item))
    return "\n".join(parts)


class AgentToolWorkbench(Workbench):
    """A read-through workbench exposing a tenant's mcp_svc tools, scoped to a
    single agent's granted tool names.

    Stateless: each list/call opens a fresh mcp_svc session. Failures are
    best-effort — a listing failure yields an empty tool set rather than
    blocking the chat. start/stop/reset and state are no-ops.
    """

    def __init__(
        self,
        tenant_id: str,
        allowed_tool_names: Sequence[str],
        approval_sink: list[str] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._allowed = set(allowed_tool_names)
        # Shared across every agent's workbench in one run (see groupchat.py):
        # a tool result signalling a pending approval is appended here so the
        # run can report it regardless of which agent made the call.
        self._approval_sink = approval_sink

    async def list_tools(self) -> list[ToolSchema]:
        if not self._allowed:
            return []
        try:
            async with _mcp_session(self._tenant_id) as session:
                result = await session.list_tools()
        except Exception:
            logger.warning(
                "mcp_svc tool listing failed for tenant=%s", self._tenant_id, exc_info=True
            )
            return []

        schemas: list[ToolSchema] = []
        for tool in result.tools:
            if tool.name not in self._allowed:
                continue
            input_schema = tool.input_schema or {}
            schemas.append(
                ToolSchema(
                    name=tool.name,
                    description=tool.description or "",
                    parameters=ParametersSchema(
                        type=input_schema.get("type", "object"),
                        properties=input_schema.get("properties", {}),
                        required=input_schema.get("required", []),
                    ),
                )
            )
        return schemas

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        cancellation_token: CancellationToken | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        if name not in self._allowed:
            return ToolResult(
                name=name,
                result=[
                    TextResultContent(
                        content=f"Tool {name!r} is not permitted for this agent."
                    )
                ],
                is_error=True,
            )
        try:
            async with _mcp_session(self._tenant_id) as session:
                result = await session.call_tool(name, dict(arguments or {}))
        except Exception as exc:
            logger.warning(
                "mcp_svc call_tool failed tenant=%s tool=%s",
                self._tenant_id,
                name,
                exc_info=True,
            )
            return ToolResult(
                name=name,
                result=[TextResultContent(content=f"Tool call failed: {exc}")],
                is_error=True,
            )
        content = _render_content(getattr(result, "content", []))
        if self._approval_sink is not None and APPROVAL_REQUIRED_MARKER in content:
            # The tool declined to act pending human approval. Record it so the
            # run surfaces a pause; the content still flows back to the model so
            # it can tell the user the action is awaiting approval.
            self._approval_sink.append(content)
        return ToolResult(
            name=name,
            result=[TextResultContent(content=content)],
            is_error=bool(getattr(result, "is_error", False)),
        )

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def reset(self) -> None:
        return None

    async def save_state(self) -> Mapping[str, Any]:
        return {}

    async def load_state(self, state: Mapping[str, Any]) -> None:
        return None
