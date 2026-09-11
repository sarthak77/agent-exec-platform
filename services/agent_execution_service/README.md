# agent_execution_service (AES)

Edge API for the AI Agent Execution Platform: gRPC CRUD for `agents` and
`tools`, plus task submission/read/approve/retry. AES owns agent
configuration (instructions, `LLMConfig`, granted tool ids) and the tool
catalog (name, description, `mutating` flag), and is the entry point a
caller submits a task to. It does not execute anything itself — `CreateTask`
forwards the task to `job_svc` as a job and returns immediately; `job_svc`'s
poller later drives that job through `orchestrator` (which runs the LLM
group chat via `gateway`, backed by tools exposed by `mcp_svc`). AES's own
job is to stay a thin, fast, tenant-scoped record system in front of that
pipeline: agents/tools/tasks in Postgres, one `tasks` row per submission
mirroring the state of exactly one job in job_svc.

## Data model (`models.py`, Postgres via SQLAlchemy async ORM)

| Table | Key columns | Notes |
| --- | --- | --- |
| `agents` | `id`, `tenant_id`, `name`, `instructions`, `llm_config_name`, `llm_config_temperature`, `version`, `created_at`, `updated_at` | `version` is bumped on every `UpdateAgent`. No status/archived flag — deletes are hard deletes. |
| `tools` | `id`, `tenant_id`, `name`, `description`, `mutating`, `version`, `created_at`, `updated_at` | `UNIQUE(tenant_id, name)`. `mutating=true` marks a tool whose calls require human approval (enforced in `orchestrator`/`mcp_svc`, not here). |
| `agent_tools` | `agent_id`, `tool_id` (composite PK, both FKs `ON DELETE CASCADE`) | The tool grant backing `Agent.tool_config.ids`. |
| `tasks` | `id`, `tenant_id`, `input`, `job_id`, `status`, `result`, `created_at`, `updated_at` | `job_id` links 1:1 to a job in `job_svc`. `status`/`result` are a **cached snapshot** of that job's state; `GetTask` refreshes it from job_svc on read for any non-terminal task, and mutating calls (approve/retry) refresh it too. A terminal task is served from the snapshot with no round-trip. |

All tables are scoped by `tenant_id`; there is no cross-tenant foreign key
anywhere in the schema.

## API surface (`proto/aep/agent_execution/v1/service.proto`)

| RPC | Purpose |
| --- | --- |
| `CreateTool` / `GetTool` / `UpdateTool` / `DeleteTool` | Manage the tool catalog for the caller's tenant. |
| `CreateAgent` / `GetAgent` / `UpdateAgent` / `DeleteAgent` | Manage agents: instructions, `LLMConfig`, and the set of tool ids granted to the agent. |
| `CreateTask` | Submit a task (`input` string) for asynchronous execution. Rejected with `FAILED_PRECONDITION` if the tenant has no agents configured. |
| `GetTask` | Read a task's status/result by id; a non-terminal task is refreshed from job_svc, a terminal task served from the local snapshot. |
| `ApproveTask` | Resume a task whose job is paused at a human-approval gate. |
| `RetryTask` | Requeue a task whose job failed or is dead. |
| `GetTaskProgress` | Read a **task-centric** progress view for one task id: lifecycle `status`, `percent_complete`, `steps_completed`/`steps_total`, the complete ordered step history (`steps`), a `requires_approval` flag, a human-readable `summary`, and the final `output`/`error`. Always read live from job_svc — never cached. |

`Get*` calls take a `filter.ids` list; an empty list returns every row for
the tenant, a non-empty list filters to those ids (unknown ids are silently
omitted, not errored — see `_link_tools` in `services/agents.py`).

## Request lifecycle

**Create tool / agent** — `servicer.py` extracts `tenant_id`
(`auth.tenant_id_from_metadata`), validates fields (`validators.py`), then
calls `ToolService.create` / `AgentService.create`. Both run inside one
`session.begin()` transaction: `AgentService.create` inserts the `AgentRow`,
flushes to get its id, then `_link_tools` re-validates the requested
`tool_ids` against this tenant's `tools` table and inserts the surviving
`agent_tools` rows — all atomic, so an agent is never left half-linked.
`ToolService.create` relies on the DB's `UNIQUE(tenant_id, name)` constraint
and turns an `IntegrityError` into a `ConflictError` (`_flush_or_conflict`)
rather than pre-checking existence, avoiding a check-then-insert race.

**Submit a task** — `servicer.CreateTask` validates `input`, then rejects the
call with `FAILED_PRECONDITION` if the tenant has no agents configured
(`AgentService.has_any`, a `LIMIT 1` existence probe) — a task has nothing to
run against otherwise, so it is refused before any job is created. Only then
does `TaskService.create` (`services/tasks.py`) call `job_client.JobClient.create_job`
**before** touching Postgres: if job_svc rejects the job, no orphaned `tasks`
row is created. The job is submitted as `JOB_TYPE_AGENT_EXECUTION` with
`AgentExecutionSpec.instructions = input`. A `TaskRow` is then inserted with
`job_id` and a status snapshot derived from the job's initial status
(`_task_status`, `queued -> pending`).

**Approve / retry** — `TaskService.approve`/`retry` look up the task's
`job_id` (scoped by `tenant_id`, `NotFoundError` if missing or owned by
another tenant), then delegate the actual state transition to job_svc
(`UpdateJob(QUEUED)` / `RetryJob`) and overwrite the local `status`/`result`
columns with whatever job_svc reports back (`_save_status`, an absolute
`UPDATE ... RETURNING`, not a read-modify-write).

**Read task progress** — `GetTaskProgress` is a **live** view, distinct from
the `GetTask` snapshot: after a tenant-scoped task lookup, `TaskService.get_progress`
fetches the backing job's execution checkpoint from job_svc
(`JobClient.get_job_progress`) on **every** call — there is no terminal
short-circuit and nothing is cached on the `tasks` row. It then *distills* that
job checkpoint into a task-centric `TaskProgressView` (`mappers.task_progress_to_proto`
-> `TaskProgress`): the job's own lifecycle becomes the task `status`, the plan
size and completed steps become `steps_completed`/`steps_total` and a
normalized `percent_complete`, each checkpointed step becomes an ordered
`TaskProgressStep` (its sub-prompt as `description`, plus `output`/`agent` and a
per-step `requires_approval`), and a one-line `summary` is composed. Job-internal
mechanics (planning phase, attempt/retry counters) are deliberately **not**
surfaced. Unlike `GetTask`'s best-effort refresh, a job_svc error here is *not*
swallowed — the remote progress is the whole response, so failing to fetch it
fails the call.

## Human approval

`ApproveTask` only resumes the underlying **job**: approving a task whose job
is parked at `waiting_approval` calls `UpdateJob(QUEUED)` on job_svc so the
poller re-runs the paused step from its checkpoint. AES keeps no approval
state of its own. The decision that a step *needs* approval is made upstream:
when an agent tries to call a tool flagged `mutating` (the `tools.mutating`
column above) without an approval signal, `orchestrator` refuses the call and
job_svc parks the job at `waiting_approval`. `ApproveTask` is simply the
edge-facing verb for "re-run that step, approved this time" — the mechanism is
generic to any `mutating` tool, not tied to any particular one.

## Multi-tenancy / auth

There is no authentication in this service — `auth.tenant_id_from_metadata`
assumes an upstream edge has already authenticated the caller and forwards
the verified tenant id as a plain `x-tenant-id` gRPC metadata header
(`AuthenticationError` -> `UNAUTHENTICATED` if absent). Every query in
`services/*` filters or joins on `tenant_id` explicitly (no session-level
"current tenant" context, no row-level security in Postgres) — isolation is
enforced by discipline in the query layer, not a DB-level guarantee. The same
`x-tenant-id` value is forwarded verbatim to job_svc (`job_client._md`), so a
job is always created and mutated under the caller's own tenant.

## Validation and error handling

`validators.py` rejects empty/oversized input at the edge (`MAX_INPUT_LEN`,
`MAX_FILTER_IDS`) and enforces that an agent must be granted at least one
tool (`validate_agent_tool_ids`) — deliberately generous bounds meant to
catch abusive input, not encode business rules. `errors.py` defines a small
typed hierarchy (`NotFoundError`, `ValidationError`, `AuthenticationError`,
`ConflictError`, `StateError`) that `servicer._handle_errors` maps to gRPC
status codes (`NOT_FOUND`, `INVALID_ARGUMENT`, `UNAUTHENTICATED`,
`ALREADY_EXISTS`, `FAILED_PRECONDITION`); anything unmapped, or a bare
`Exception`, is logged with `logger.exception` and returned as `INTERNAL`
with no detail leaked to the caller.

## Idempotency

Not implemented for `CreateTask` / `CreateTool` / `CreateAgent` — each call
creates a new row; there is no client-supplied idempotency key or
dedup-by-content check, so a retried `CreateTask` after a dropped response
will submit a second job. `ApproveTask` and `RetryTask` are naturally
idempotent-safe in effect (not in response): job_svc's atomic conditional
update means a second `ApproveTask` on an already-resumed job fails cleanly
with `StateError`/`FAILED_PRECONDITION` rather than double-resuming.
`ToolService.create` is protected from duplicate names by the DB unique
constraint, which is a content-level safeguard, not a request-idempotency
one.

## Concurrency and state management

All Postgres access is async (`asyncpg` via SQLAlchemy's async engine,
`db.py`), and every mutation is wrapped in `sessions.begin()` so writes are
transactional. `TaskService` deliberately does **not** own job state
transitions: rather than fetch-then-check-then-mutate on a cached status (a
classic race), it forwards `approve`/`retry` to job_svc and treats whatever
comes back as authoritative, writing it with an absolute `UPDATE` (never
read-modify-write) so concurrent refreshes are last-writer-wins against a
single source of truth. `GetTask` applies the same discipline on the read path:
for a non-terminal task it refreshes status/result from job_svc (concurrently
across tasks, **outside** any DB transaction) best-effort — a failed refresh
keeps the last snapshot rather than failing the read — and persists only the
rows that actually changed. `job_client.JobClient` uses one lazily-created,
process-wide gRPC channel (channels multiplex concurrent RPCs internally),
and the remote call to job_svc always happens **outside** any DB transaction
so a slow or unreachable job_svc can never pin a Postgres connection open.
`tests/test_task_service.py::test_concurrent_approvals_only_one_wins` and
`test_concurrent_creates_persist_all_distinct_tasks` exercise this directly
with `asyncio.gather` over a fake gateway.

## Reliability / failure recovery

AES's role in the platform's failure story is narrow: it hands work off to
job_svc and never re-derives job state on its own. If job_svc is unreachable,
`JobClient._call` catches `grpc.aio.AioRpcError` and raises a typed
`AppError` (mapped by code — see `_ERROR_BY_CODE`), which the servicer
surfaces as a normal gRPC error to the caller; `CreateTask` fails outright
rather than silently queuing locally, so no task is ever recorded without a
corresponding job existing in job_svc. AES holds no in-memory execution
state and runs no background workers, so a process restart loses nothing —
all state is in Postgres, and every RPC is independently retryable by the
caller.

## Observability

Logging is stdlib `logging` at `INFO` (`main.py`,
`logging.basicConfig(level=logging.INFO)`), with unhandled exceptions logged
via `logger.exception` in the `_handle_errors` decorator before being
converted to an `INTERNAL` gRPC status. There are no metrics, no structured
log fields (tenant id, request id, etc. are not attached to log records),
and no distributed tracing — this is intentionally a "basic" implementation;
see Known limitations.

## Assumptions

These are the deliberate simplifications this "basic" implementation makes;
each is a place a fuller build would grow structure.

- **Tools are catalog pointers to a built-in handler** — a `tools` row
  (`name`, `description`, `mutating`) carries no input schema or execution
  config; it names an execution binding that already exists in code in
  `mcp_svc` (`handlers.py`'s `_HANDLERS`, e.g. `http_request`,
  `query_database`). Creating a tool whose `name` has no matching handler
  yields a catalog entry that lists but can't execute ("no execution
  binding"). Generic, code-/data-driven tools (arbitrary input schema +
  executor kind/config, or an `mcp_proxy` to external MCP servers) are a
  later phase.
- **An agent is instructions + granted tools** — an `Agent` is a `name`,
  free-text `instructions`, an `LLMConfig` (model name + temperature), and a
  set of granted `tool_config.ids`; nothing more. There is no per-agent
  memory, persisted conversation state, sub-agents, or routing logic —
  run-time behaviour is entirely the instructions plus the tools it is
  allowed to call, and every agent must be granted at least one tool.
- **A task is a single text input** — `CreateTask` takes one free-text
  `input` string as the entire task spec, forwarded verbatim as the job's
  `AgentExecutionSpec.instructions`. There are no structured parameters,
  attachments, explicit target-agent selection, or multi-turn conversation
  state: one task = one prompt = one job.

