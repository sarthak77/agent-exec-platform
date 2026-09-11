"""Execution bindings for catalog tools.

The `tools` table is a per-tenant *catalog* — it says which tools a tenant is
allowed to see (name + description), but carries no execution details. The
actual behaviour of a tool lives here, in code, exactly like the reference
appsec-mcp-servers keeps each tool's implementation (and its input schema) in
a `tool_impl` module rather than in a database row.

A `Handler` binds a catalog name to (a) the JSON-Schema for its arguments,
surfaced on `tools/list`, and (b) an async `run` that performs the work and
returns a text result. `server.py` gates execution on the catalog first (the
tenant must own a row with that name), then dispatches to the handler here; a
catalog tool with no registered handler is reported as "no execution binding"
rather than executed.

Handlers receive a `ToolContext` alongside the parsed arguments so a handler
that needs the caller's identity (e.g. the tenant, for a per-tenant approval
gate) has it without every handler having to plumb it through.

Two handlers ship here today:

* `http_request` — a built-in generic handler so the platform has a working
  end-to-end tool out of the box (agent -> gateway tool call -> mcp_svc
  executes -> result fed back) without every deployment having to add code
  first.
* `query_database` — read-only SQL access to this service's own Postgres
  connection (the `customers`/`invoices` demo tables). Guarded to a single
  SELECT statement over an allow-listed set of tables, so an agent can
  explore that data without being able to mutate it, chain statements, or
  read unrelated tables (`tools`, `agents`, ...) that happen to live in the
  same database.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from mcp_svc.config import settings
from mcp_svc.db import Sessions

# Sentinel a tool result carries when the action was withheld pending a human
# approval. The orchestrator watches for this string in a tool result (see
# orchestrator/mcp_workbench.py's APPROVAL_REQUIRED_MARKER) to pause the job;
# keep the two in sync.
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Ambient per-call context handed to every handler. `tenant_id` is the
    caller resolved from the request (see server.py)."""

    tenant_id: str


@dataclass(frozen=True, slots=True)
class Handler:
    """A code-defined execution binding for a catalog tool.

    `input_schema` is a JSON-Schema object (the same shape MCP's `inputSchema`
    expects). `run` receives the already-parsed arguments dict plus the call's
    `ToolContext` and returns the tool result as text (JSON-encoded for
    structured results).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any], ToolContext], Awaitable[str]]


# --------------------------------------------------------------------------- #
# http_request — generic outbound HTTP                                        #
# --------------------------------------------------------------------------- #

_HTTP_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {
            "type": "string",
            "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
            "description": "HTTP method to use.",
        },
        "url": {"type": "string", "description": "Absolute URL to request."},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Optional request headers.",
        },
        "query": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Optional query-string parameters.",
        },
        "body": {
            "type": "object",
            "description": "Optional JSON request body.",
        },
    },
    "required": ["method", "url"],
    "additionalProperties": False,
}


async def _run_http_request(arguments: dict[str, Any], ctx: ToolContext) -> str:
    """Perform an outbound HTTP request and return a JSON-encoded summary of
    the response (status, headers, body). Network/HTTP errors are returned as
    a structured error payload rather than raised, so the model gets a usable
    tool result to reason about instead of an opaque failure."""
    method = str(arguments.get("method", "")).upper()
    url = str(arguments.get("url", ""))
    if not method or not url:
        raise ValueError("`method` and `url` are required")

    headers = arguments.get("headers") or {}
    params = arguments.get("query") or {}
    body = arguments.get("body")

    timeout = settings.tool_execution.timeout_seconds
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=body if body is not None else None,
            )
    except httpx.HTTPError as exc:
        return json.dumps({"error": str(exc)})

    # Prefer JSON if the response parses as JSON; otherwise fall back to text
    # (truncated so a huge body can't blow up the model's context).
    text = response.text
    max_chars = settings.tool_execution.max_response_chars
    truncated = len(text) > max_chars
    try:
        parsed: Any = response.json()
        payload: dict[str, Any] = {"json": parsed}
    except ValueError:
        payload = {"text": text[:max_chars], "truncated": truncated}

    return json.dumps(
        {
            "status_code": response.status_code,
            **payload,
        }
    )


# --------------------------------------------------------------------------- #
# query_database — read-only SQL against the customers/invoices tables       #
# --------------------------------------------------------------------------- #

_QUERY_DATABASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "A single read-only SQL SELECT statement over the `customers` "
                "and/or `invoices` tables."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_QUERY_ALLOWED_TABLES = {"customers", "invoices"}
_QUERY_TABLE_REF_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
_QUERY_MAX_ROWS = 100


async def _run_query_database(arguments: dict[str, Any], ctx: ToolContext) -> str:
    """Run a single read-only SQL query and return the matching rows as JSON.

    Guardrails (not full tenant-row-level security — a demo-scale substitute
    for it): only one statement, only SELECT, and only over the allow-listed
    tables, so a hallucinating or adversarial agent can't mutate data, chain a
    second statement onto the query, or read tables outside the ones this
    tool is meant to expose. Results are capped so a broad query can't blow
    up the model's context.
    """
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ValueError("`query` is required")
    if ";" in query.rstrip(";"):
        return json.dumps({"error": "only a single SQL statement is allowed"})
    if not re.match(r"^\s*select\b", query, re.IGNORECASE):
        return json.dumps({"error": "only SELECT statements are allowed"})

    tables = {m.group(1).lower() for m in _QUERY_TABLE_REF_RE.finditer(query)}
    if not tables or not tables <= _QUERY_ALLOWED_TABLES:
        return json.dumps(
            {"error": f"query may only reference: {', '.join(sorted(_QUERY_ALLOWED_TABLES))}"}
        )

    try:
        async with Sessions() as session:
            result = await session.execute(text(query))
            rows = result.mappings().fetchmany(_QUERY_MAX_ROWS + 1)
    except SQLAlchemyError as exc:
        return json.dumps({"error": str(exc)})

    truncated = len(rows) > _QUERY_MAX_ROWS
    rows = rows[:_QUERY_MAX_ROWS]
    return json.dumps(
        {"rows": [dict(row) for row in rows], "count": len(rows), "truncated": truncated},
        default=str,
    )


_HANDLERS: dict[str, Handler] = {
    "http_request": Handler(
        name="http_request",
        description=(
            "Make an outbound HTTP request to a URL and return the response "
            "status and body."
        ),
        input_schema=_HTTP_INPUT_SCHEMA,
        run=_run_http_request,
    ),
    "query_database": Handler(
        name="query_database",
        description=(
            "Run a read-only SQL SELECT query against the customers/invoices "
            "tables and return the matching rows."
        ),
        input_schema=_QUERY_DATABASE_SCHEMA,
        run=_run_query_database,
    ),
}


def get_handler(name: str) -> Handler | None:
    """Return the execution binding for `name`, or None if none is registered."""
    return _HANDLERS.get(name)
