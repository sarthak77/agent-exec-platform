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
  first. Rejects requests to obviously-internal targets (cloud metadata IPs,
  loopback, link-local, private ranges) before dispatching them.
* `query_database` — read-only SQL access to this service's own Postgres
  connection (the `customers`/`invoices` demo tables). The query is parsed
  (not regex-matched) to enforce a single plain SELECT over only the
  allow-listed tables — so an agent can't mutate data, chain statements, or
  read unrelated tables (`tools`, `agents`, ...) that happen to live in the
  same database — and is then run beneath a tenant-scoping CTE so it can
  only ever see the caller's own tenant's rows.
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import sqlglot
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

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


_HTTP_ALLOWED_SCHEMES = {"http", "https"}
# Hostnames that never resolve to a legitimate external target for this tool,
# checked in addition to the IP-literal blocklist below.
_HTTP_BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal"}


def _assert_url_is_public(url: str) -> None:
    """Reject obviously-internal targets (cloud metadata endpoints, loopback,
    link-local, and other private ranges) before we let the model make an
    outbound request to an arbitrary, fully agent-controlled URL.

    This is a static check on the literal host only (no DNS resolution), so
    it can't stop DNS-rebinding (a public hostname that resolves to a private
    IP at request time) — closing that fully would need a resolver-pinning
    transport or network-level egress control. It does stop the common case
    of an agent being tricked into hitting an IP-literal internal target
    (e.g. `http://169.254.169.254/...`) or `localhost`.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in _HTTP_ALLOWED_SCHEMES:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise ValueError("URL must include a host")
    if host.lower() in _HTTP_BLOCKED_HOSTNAMES or host.lower().endswith(".localhost"):
        raise ValueError(f"requests to {host!r} are not allowed")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a non-literal hostname; nothing further to check statically
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(f"requests to {host!r} are not allowed")


async def _run_http_request(arguments: dict[str, Any], ctx: ToolContext) -> str:
    """Perform an outbound HTTP request and return a JSON-encoded summary of
    the response (status, headers, body). Network/HTTP errors are returned as
    a structured error payload rather than raised, so the model gets a usable
    tool result to reason about instead of an opaque failure."""
    method = str(arguments.get("method", "")).upper()
    url = str(arguments.get("url", ""))
    if not method or not url:
        raise ValueError("`method` and `url` are required")
    _assert_url_is_public(url)

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
_QUERY_MAX_ROWS = 100

# Table names the caller's (validated) query is rewritten to use, each bound
# to a tenant-filtered CTE prepended ahead of it — so no shape of SELECT the
# caller writes (join, subquery, aggregate, ...) can see another tenant's
# rows. Distinct names (rather than reusing `customers`/`invoices` and
# relying on WITH-clause name shadowing) keep this portable: some engines
# reject a non-recursive CTE whose body references a table of the same name.
_SCOPED_TABLE_NAME = {"customers": "__tenant_customers", "invoices": "__tenant_invoices"}
_TENANT_SCOPE_CTE = (
    "WITH __tenant_customers AS (SELECT * FROM customers WHERE tenant_id = :tenant_id), "
    "__tenant_invoices AS (SELECT * FROM invoices WHERE tenant_id = :tenant_id) "
)


def _validate_query_database_sql(query: str) -> exp.Select:
    """Parse `query`, enforce that it's a single plain SELECT (no statement
    chaining, no CTEs of its own, no schema-qualified table refs — e.g.
    `public.customers` — which would bypass the allow-list below since it
    names a table outside this connection's default search path resolution)
    over only allow-listed tables, then rewrite its table references to the
    tenant-scoped names in `_SCOPED_TABLE_NAME`. Raises ValueError with a
    caller-facing message on any violation.
    """
    try:
        statements = [s for s in sqlglot.parse(query, read="postgres") if s is not None]
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError(f"could not parse SQL: {exc}") from exc

    if len(statements) != 1:
        raise ValueError("only a single SQL statement is allowed")
    stmt = statements[0]

    if not isinstance(stmt, exp.Select):
        raise ValueError("only SELECT statements are allowed")
    if list(stmt.find_all(exp.With)):
        raise ValueError("queries may not define their own WITH/CTE clauses")

    tables = list(stmt.find_all(exp.Table))
    names: set[str] = set()
    for table in tables:
        if table.db:
            raise ValueError("schema-qualified table references are not allowed")
        names.add(table.name.lower())

    if not names or not names <= _QUERY_ALLOWED_TABLES:
        raise ValueError(
            f"query may only reference: {', '.join(sorted(_QUERY_ALLOWED_TABLES))}"
        )

    for table in tables:
        table.set("this", exp.to_identifier(_SCOPED_TABLE_NAME[table.name.lower()]))
    return stmt


async def _run_query_database(arguments: dict[str, Any], ctx: ToolContext) -> str:
    """Run a single read-only SQL query, scoped to the caller's tenant, and
    return the matching rows as JSON.

    Guardrails: the query is parsed (not regex-matched) to enforce a single
    plain SELECT over only the allow-listed tables, and is then run beneath a
    tenant-scoping CTE (see `_TENANT_SCOPE_CTE`) so the caller's tenant can
    never see another tenant's `customers`/`invoices` rows regardless of how
    the SELECT is shaped. Results are capped so a broad query can't blow up
    the model's context.
    """
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ValueError("`query` is required")

    try:
        stmt = _validate_query_database_sql(query)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    scoped_sql = _TENANT_SCOPE_CTE + stmt.sql(dialect="postgres")

    try:
        async with Sessions() as session:
            result = await session.execute(text(scoped_sql), {"tenant_id": ctx.tenant_id})
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
