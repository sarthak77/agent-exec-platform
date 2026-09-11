# mcp_svc

Tool-execution boundary for the platform. Exposes a tenant's tool catalog and
runs the actual tool calls an agent's execution makes — this is where a
model's decision to call a tool ("look up an invoice", "make an HTTP
request") turns into a real side effect. `orchestrator` is the only
consumer today: it drives the model conversation via `gateway`, and every
`tools/list` / `tools/call` an agent issues goes over MCP to this service.
`agent_execution_service` owns the tool *catalog* (the `tools` table —
which tool names exist and which agents may see them); this service only
reads that table and supplies the executable behaviour behind the names it
finds there. `job_svc` never talks to this service directly — it drives
`orchestrator`, which is the actual MCP client.

## Protocol

This is the one service in the platform that isn't gRPC. It's a real MCP
(Model Context Protocol) server, served over **streamable HTTP** (the
`mcp` SDK's `Server(...).streamable_http_app()`), mounted as a Starlette
ASGI app and run under `uvicorn` (`main.py`, `server.py`). That matches
standard MCP server practice — stdio is for local, single-client tools;
streamable HTTP is the transport MCP defines for a server multiple remote
clients (here, `orchestrator` instances) can reach over the network.
Transport-level Host/Origin (DNS-rebinding) protection is configurable via
`[security]` in `config.toml` and is left **off** by default, on the
assumption the service sits behind a trusted internal edge — see
`_transport_security()` in `server.py`.

The server registers exactly two MCP operations: `on_list_tools` and
`on_call_tool` (`build_server()` in `server.py`).

## Tool catalog vs. tool execution

The `tools` table is a per-tenant *catalog* — just `(id, tenant_id, name,
description)` — owned and written by `agent_execution_service`; this
service only reads it (`tools.py`, `models.py` — `mcp_svc` has no DDL
bootstrap of its own). It carries no execution details. The actual
behaviour of a tool lives in code, in `handlers.py`, as a `Handler(name,
description, input_schema, run)` binding.

On `tools/list`, `_on_list_tools` queries the catalog live (no in-process
cache, so a tool created via `agent_execution_service` is visible
immediately) and, for each row, looks up a code handler by name:

- catalog row **with** a registered handler → listed with that handler's
  real JSON-Schema `input_schema` and description.
- catalog row **without** one → still listed (so it's discoverable), but
  with an empty schema (`{"type": "object", "properties": {}}`).

On `tools/call` (`_on_call_tool`), the catalog is checked *first* — the
calling tenant must own a row with that name — and only then is the
call dispatched to the handler. A catalog entry with no handler returns a
`isError=True` result ("no execution binding configured") rather than
silently doing nothing.

## Tools implemented

Two handlers ship today (`handlers.py`), registered in `_HANDLERS`:

**`http_request`** — generic outbound HTTP call.
- Schema: `method` (`GET`/`POST`/`PUT`/`PATCH`/`DELETE`, required), `url`
  (required), optional `headers`, `query`, JSON `body`.
- Rejects requests to obviously-internal targets before dispatching:
  loopback, link-local, private/reserved/multicast IP ranges,
  `localhost`/`*.localhost`, and known cloud-metadata hostnames
  (`_assert_url_is_public`). This is a static check on the literal
  host/IP only — no DNS resolution — so it stops IP-literal SSRF but not a
  public hostname that resolves to a private IP at request time
  (DNS rebinding); closing that needs a resolver-pinning transport or
  network-level egress control, and is called out as a known gap in the
  docstring.
- Response body is returned as JSON if parseable, else as text truncated
  to `tool_execution.max_response_chars`; network/HTTP errors are caught
  and returned as a `{"error": ...}` payload rather than raised, so the
  model always gets a usable result to reason about.
- This is the platform's generic, works-out-of-the-box tool — it's what
  demonstrates the agent → gateway → tool-call → mcp_svc → result loop
  without every deployment needing to write a handler first. It is *not*
  a purpose-built "send email" tool; there is no dedicated email handler
  in this service (see Limitations).

**`query_database`** — read-only SQL over this service's own Postgres
connection, against the `customers`/`invoices` demo tables (the same
tables `tests/sql/sample_data.sql` at the platform root seeds — 10
customers, 20 invoices with `paid`/`overdue`/`pending` statuses). This is
the tool that stands in for the assignment's "retrieve invoices" /
"retrieve customer information" tools.
- Schema: single `query` string (a SQL `SELECT`), required.
- The query is **parsed** with `sqlglot` (not regex-matched) and rejected
  unless it is exactly one plain `SELECT`, with no `WITH`/CTE of its own,
  no schema-qualified table refs (e.g. `public.customers`, which would
  dodge the allow-list), and referencing only `customers`/`invoices`
  (`_validate_query_database_sql`). This blocks statement chaining,
  mutation, and reads of unrelated tables that live in the same database
  (`tools`, `agents`, ...).
- The validated statement is then rewritten to reference
  `__tenant_customers`/`__tenant_invoices` and run beneath a prepended CTE
  that pre-filters both tables to `tenant_id = :tenant_id`
  (`_TENANT_SCOPE_CTE`) — so no shape of `SELECT` (join, subquery,
  aggregate) can see another tenant's rows, regardless of what the caller
  writes.
- Results are capped at 100 rows (`_QUERY_MAX_ROWS`), with a `truncated`
  flag if more matched.
- Both parse failures and SQL execution errors are returned as
  `{"error": ...}` text rather than raised — same "always give the model
  something to reason about" pattern as `http_request`.

There is no "create an email draft" or "send an email" tool implemented
here — see Limitations.

## Tool permissions — enforced where?

**Not here.** This service will execute any handler-backed tool for any
tenant that owns a matching catalog row; it has no concept of "this
agent" at all. Two layers of permissioning sit upstream instead:

1. `agent_execution_service` decides which tool *rows* exist for a tenant
   and which agents are granted which tools (`agent_tools`), via its own
   API — this is "the mechanism for controlling which tools are available
   to a particular agent" the assignment asks for.
2. `orchestrator`'s `mcp_workbench.AgentToolWorkbench` loads a specific
   agent's granted tool names and filters `tools/list` down to
   `self._allowed` before the model ever sees them, and re-checks
   `name not in self._allowed` on every `tools/call`, refusing (with
   `is_error=True`, no request sent) if the agent isn't granted that tool.

So `mcp_svc` enforces **tenant** isolation (via the catalog check in
`_on_call_tool`) but not **per-agent** tool permissions — an agent-scoped
call that reaches this service has already passed the per-agent check in
`orchestrator`.

## Request lifecycle (`tools/call`)

1. `_tenant_id(ctx)` pulls `x-tenant-id` off the MCP request context
   (`auth.py`); missing header → `MCPError(INVALID_REQUEST)`.
2. `get_tool_by_name(session, tenant_id, name)` — catalog lookup scoped to
   `(tenant_id, name)`; a unique constraint on the writer's side guarantees
   at most one row. No row → `isError=True`, "not found in the catalog".
3. `get_handler(name)` — code lookup. No handler → `isError=True`, "no
   execution binding configured".
4. `handler.run(arguments, ToolContext(tenant_id=tenant_id))` — the
   handler does its own argument validation and work. `ToolContext` is how
   a handler gets the caller's tenant without every handler threading it
   through by hand (used by `query_database` for the scoping CTE).
5. Any exception the handler raises is caught, logged
   (`logger.exception`), and turned into an `isError=True` text result
   ("tool %r failed: %s") — a buggy or misused handler can't crash the
   MCP session or leak a bare traceback to the caller.
6. Success: `CallToolResult(content=[TextContent(text=result)])`. Every
   handler returns its result pre-serialized as a JSON string, so the
   content shape is uniform regardless of which tool ran.

## Error handling

`errors.py` defines exactly one typed error, `AuthenticationError` (missing
tenant identity), which `server.py` maps to `MCPError(INVALID_REQUEST)` —
a clean, protocol-level error rather than an opaque 500. Everything else
(bad arguments, unknown tool, handler exceptions, SQL/network failures) is
handled at the call site as described above and returned as an
`isError=True` `CallToolResult` or a `{"error": ...}` JSON payload inside a
normal result — deliberately *not* raised up through the MCP transport, so
"the tool failed" is something the calling model can see and react to
(e.g. tell the user, try different arguments) instead of a request that
just errors out.
