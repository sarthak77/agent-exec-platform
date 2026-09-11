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

## Side-effecting vs. read-only tools, and human approval

`query_database` is read-only by construction (parser rejects anything
but `SELECT`). `http_request` can be side-effecting (`POST`/`PUT`/`PATCH`/
`DELETE`) or read-only (`GET`) depending on the arguments the model
supplies — this service has no way to tell from the catalog alone.

**Approval gating does not live in mcp_svc.** `handlers.py` defines a
shared sentinel string, `APPROVAL_REQUIRED`, with a comment that it must
stay in sync with `orchestrator/mcp_workbench.py`'s
`APPROVAL_REQUIRED_MARKER` — but neither shipped handler here actually
emits it. The real gate is in `orchestrator`'s `AgentToolWorkbench.call_tool`:
it checks `name in self._mutating and not self._approved` and, if true,
**never calls this service at all** — it returns the `APPROVAL_REQUIRED`
marker itself and records a pending approval. So the mutating/read-only
classification and the pause-for-approval decision are both made in
`orchestrator`, upstream of this service; mcp_svc's sentinel exists purely
as a shared string constant for that upstream logic (and as a hook if a
handler here ever needs to decline an action mid-execution for a reason
only it can see, e.g. amount-based thresholds). This is a real
inconsistency worth knowing about if you're extending this service: adding
a new mutating handler here does **not** automatically get approval
gating — that also has to be declared as "mutating" wherever
`orchestrator` builds `self._mutating` for the agent's tool grants.

## Multi-tenancy / isolation

Tenant identity comes from a single `x-tenant-id` header (`auth.py`),
exactly mirroring `agent_execution_service`'s and `orchestrator`'s
pattern: RBAC/authentication is assumed to happen upstream at the edge;
this service only trusts and scopes by whatever tenant id it's handed.
Isolation is enforced at two points: the catalog lookup in `_on_call_tool`
is always filtered by `(tenant_id, name)`, and `query_database`'s
tenant-scoping CTE means a validated query can only ever read the
caller's own tenant's `customers`/`invoices` rows, regardless of query
shape. There's no cross-tenant data path in either shipped handler.

## Observability

Plain `logging` module usage only: `INFO` level configured in `main.py`,
a `logger.warning` on a failed tenant-scoped listing (not applicable here
directly, that's orchestrator's workbench) and `logger.exception` on a
handler `run()` failure in `server.py`, with the exception folded into
the returned error text as well. No structured logging, metrics, or
tracing.

## Run

```sh
uv sync
uv run mcp-svc
```

Needs Postgres reachable with the `tools` (and, for `query_database`,
`customers`/`invoices`) tables that `agent_execution_service`/the
integration-test seed create. Config (HTTP host/port, Postgres,
transport security allowlists, tool-execution timeout/response-size
bounds) lives in `config.toml`; override its path with
`MCP_SVC_CONFIG_FILE`. Postgres credentials can be overridden per-field
from the environment (`MCP_SVC_POSTGRES_PASSWORD`, `MCP_SVC_POSTGRES_HOST`,
...) so real deployments keep secrets out of source control.

## Test

```sh
uv run --group dev pytest
```

`tests/test_handlers.py` covers both handlers directly against
`ToolContext`/`get_handler`: `http_request` against an `httpx.MockTransport`
(JSON/text bodies, network errors, the full internal-target blocklist),
and `query_database` against an in-memory SQLite engine seeded with two
tenants (tenant isolation, a cross-table join, and every bypass attempt in
the docstring: comma-join, schema-qualification, CTE-shadowing, mutation,
multi-statement, non-allow-listed table). No Postgres or MCP transport
needed for these tests.

## Known limitations / what production would add next

- **No dedicated email tools.** The assignment's example toolset ("create
  an email draft", "send an email") isn't implemented as first-class
  handlers — `http_request` could call a real email API, but there's no
  purpose-built `draft_email`/`send_email` handler, no draft persistence,
  and (per above) no handler-side approval marker wired to either.
- **Approval/mutating classification lives entirely upstream** in
  `orchestrator`, with only a shared string constant here — there's no
  way for this service to independently refuse a mutating call if
  `orchestrator`'s classification is wrong or missing for a given tool.
- **DNS-rebinding is only partially closed** for `http_request` (static
  IP/hostname check, no resolver pinning) — noted directly in the code.
- **No per-tool rate limiting or per-tenant quotas** on tool execution;
  `tool_execution.timeout_seconds`/`max_response_chars` bound a single
  `http_request` call but nothing bounds call *frequency*.
- **No dry-run/simulate mode** for side-effecting tools — a handler either
  runs for real or (via `orchestrator`) doesn't run at all.
- Handler registry is a static Python dict (`_HANDLERS`); adding a new
  tool means shipping code, not just inserting a catalog row — by design
  (mirrors the reference `appsec-mcp-servers` pattern cited in
  `handlers.py`), but worth knowing if the expectation was fully
  data-driven tools.
