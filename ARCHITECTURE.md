# Architecture

This document explains how the AI Agent Execution Platform is put together: the
services, their data models, the mechanisms that make execution reliable and
resumable, and the trade-offs behind each decision. It corresponds to
deliverable #3 in `Sarthak_Assignment.pdf` ("Architecture/design documentation
explaining the major components and decisions"); see the mapping table at the
end for how each assignment requirement is addressed.

## 1. System overview

Five services, each independently deployable, backed by a single shared
Postgres instance (they are separated by database/table ownership *within*
that one instance, not by a database server per service — see §9). Services
talk to each other over gRPC, except tool execution, which uses MCP over
streamable HTTP:

```
  external caller
       │  gRPC: CreateTask / GetTask / ApproveTask / RetryTask
       ▼
┌─────────────────────────┐   owns agents / tools / tasks tables
│ agent_execution_service │
│ (AES)                   │
└──────────┬──────────────┘
           │  gRPC: CreateJob / GetJob / UpdateJob / RetryJob
           ▼
┌─────────────────────────┐   owns jobs table; poller + runner + reaper
│ job_svc                 │
│ (queue + execution)     │
└──────────┬──────────────┘
           │  gRPC: Chat  (1x decompose, then 1x per plan step)
           ▼
┌─────────────────────────┐      MCP over streamable HTTP       ┌───────────────┐
│ orchestrator            │ ──── (tools/list, tools/call) ────▶ │ mcp_svc       │
│ (AutoGen group chat)    │                                     │ (catalog +    │
└──────────┬──────────────┘                                     │  execution)   │
           │  gRPC: Chat (per-agent LLM calls)                  └───────┬───────┘
           ▼                                                            │ outbound
┌─────────────────────────┐   HTTP (OpenAI-compatible)  ┌───────────┐  │ HTTP / SQL
│ gateway                 │ ──────────────────────────▶ │ LLM       │  ▼
│ (guardrails + egress)   │        (Groq endpoint)       │ provider  │ customers/invoices,
└─────────────────────────┘                             └───────────┘ external APIs

  Shared Postgres instance (localhost:5432):
    - db `agent_execution_service` — AES-owned agents/tools/tasks (+ demo
      customers/invoices); read directly by orchestrator and mcp_svc
    - db `job_svc` — jobs table, owned by job_svc
    - gateway is stateless (no database)
```

Each arrow that crosses a service boundary is either a generated gRPC stub
call or (orchestrator → mcp_svc) an MCP client session — never a shared
in-process call, so every hop is independently deployable, retryable, and
timeout-able.

**Request flow for "submit a task":**

1. A caller creates a `Task` on **AES** (`CreateTask`). AES creates a `Job` on
   **job_svc** first, then a local `tasks` row that mirrors the job's status.
2. **job_svc**'s background poller claims queued jobs and hands them to the
   **runner**, which calls **orchestrator** twice per sub-step: once to
   decompose the task into a plan, then once per planned step to execute it.
3. **orchestrator** builds an AutoGen `SelectorGroupChat` from the tenant's
   configured agents, each wired to a per-agent **mcp_svc** tool workbench,
   and to **gateway** for the actual LLM calls.
4. **gateway** screens/sanitizes the request, forwards it to the configured
   LLM provider, and translates the response (including tool calls) back.
5. If a tool call is flagged `mutating`, the workbench refuses it locally and
   returns an `APPROVAL_REQUIRED` sentinel instead of calling **mcp_svc** —
   this propagates up as a `requires_approval` finish reason, and the job
   parks at `waiting_approval` without losing any completed work.
6. A human calls `ApproveTask` on AES, which resumes the job on job_svc; the
   poller re-claims it and the runner continues from its checkpoint.

## 2. Why this shape

- **Job queue + poller, not a message broker.** The assignment allows any
  reasonable queue/broker choice. A `jobs` table with atomic conditional
  `UPDATE ... WHERE status IN (...) RETURNING` transitions gives the same
  claim/ack/retry semantics as a broker (see §4.1) without adding an
  operational dependency (Postgres is already required for state). The
  trade-off: no built-in fan-out/priority topics, and the poller's
  `interval_seconds` (default 5s, see `job_svc/config.toml`) trades a small
  amount of pickup latency for a simple polling loop instead of push-based
  delivery. This is the honest limitation to flag: at high job volume a real
  broker (SQS/Kafka/Postgres `LISTEN/NOTIFY`) would reduce both latency and
  polling overhead.
- **Five services, not one monolith.** Each maps to a distinct
  responsibility and scaling profile: AES is CRUD-heavy and cheap to run many
  replicas of; job_svc's poller is the one component doing continuous
  background work and is the natural place to scale worker count
  independently; orchestrator holds the (comparatively expensive, per-tenant
  configured) AutoGen chat construction; gateway is the single choke point for
  LLM egress, guardrails, and (would-be) rate limiting; mcp_svc is the only
  service allowed to reach external tools/APIs, isolating that blast radius.
  The cost is inter-service network hops and duplicated plumbing (each has its
  own error hierarchy, gRPC bootstrap, config loader) — accepted deliberately
  so that a slow orchestrator call can't stall AES's CRUD path, and so tool
  execution has its own trust boundary.
- **gRPC for service-to-service, MCP for tool execution.** gRPC gives typed
  contracts (protobuf) and cheap streaming-capable channels between services
  we control end to end. MCP is used specifically for tool execution because
  it's the interoperability-oriented protocol for exposing tools to agents —
  it's the natural fit if tool catalogs were ever provided by a third party
  rather than by mcp_svc itself.
- **AutoGen `SelectorGroupChat`, not a hand-rolled ReAct loop.** Multiple
  tenant-configured agents can participate in one task, with LLM-driven
  speaker selection rather than a fixed order — closer to "the agent decides
  the sequence of actions" from the assignment's example use case. The cost is
  an extra abstraction layer (a custom `ChatCompletionClient` bridging
  AutoGen's tool-call protocol onto gateway's wire format; see §3.3) and one
  more third-party dependency to pin correctly (autogen's mcp workbench turned
  out to be incompatible with the mcp 2.x client this project needs — see
  §3.3.2 — so tool execution is a small custom `Workbench`).

## 3. Services

### 3.1 gateway — the only path to the LLM

**Responsibility:** guardrail every inbound chat request, forward it to the
configured provider, translate the response (including tool-calling) back
into the platform's own wire format.

- **RPC:** `GatewayService.Chat(ChatRequest) -> ChatResponse`
  (`services/gateway/proto/aep/gateway/v1/service.proto`). `ChatRequest`
  carries `messages`, an optional per-call `model_config` override
  (temperature/max_tokens), a list of `Tool` function schemas the model may
  call, and an optional `tool_choice`. `ChatResponse` carries the reply
  message, summed `token_usage`, and a `finish_reason` (`"tool_calls"` when
  the model wants to call a tool).
- **Guardrails (`guardrails.py`):** role allow-list (rejects any message whose
  `role` isn't one of the expected set), empty/oversized-input rejection
  (`max_input_chars`, config-driven), a phrase blocklist scoped only to
  `user`/`tool` roles (so a system prompt is free to *mention* a blocked
  phrase without tripping the guard on itself), and PII redaction via regex
  (email, SSN, card number, phone) applied to user-authored content before it
  reaches the model.
- **Provider (`provider.py`):** `OpenAIProvider.complete()` — the platform
  runs against Groq's OpenAI-compatible endpoint (`config.toml`'s
  `[model]` section: `provider = "groq"`, model `openai/gpt-oss-20b`), so
  swapping providers is a config change plus a new `Provider` implementation,
  not a rewrite of gateway.
- **Errors:** every `AppError` subclass (`ValidationError`, `GuardrailRejected`,
  `ProviderError`, ...) is mapped to a gRPC status code via a
  `_handle_errors` decorator + `_STATUS_BY_ERROR` table in `servicer.py` — a
  pattern repeated identically in every service (see §4.6) so a caller can
  branch on gRPC status without knowing the callee's internals.
- **No persistence.** gateway is stateless — it holds no database — so it
  scales horizontally trivially and a crash loses nothing but the in-flight
  call (which the caller retries).

### 3.2 agent_execution_service (AES) — agents, tools, tasks

**Responsibility:** the tenant-facing CRUD surface for agents and tools, and
the task↔job bridge.

- **RPC (`services/agent_execution_service/proto/.../service.proto`):**
  `CreateTool`/`GetTool`/`UpdateTool`/`DeleteTool`,
  `CreateAgent`/`GetAgent`/`UpdateAgent`/`DeleteAgent`,
  `CreateTask`/`GetTask`/`ApproveTask`/`RetryTask`.
- **Data model:**
  - `ToolRow` — `id, tenant_id, name, description, mutating, version,
    created_at, updated_at`. `mutating` is the single flag that drives the
    whole human-approval mechanism (§4.3) — it is generic, not hardcoded to
    "email": any tool marked `mutating` requires approval before its first
    (unapproved) call in a job.
  - `AgentRow` — `id, tenant_id, name, instructions, llm_config_name,
    llm_config_temperature, version, created_at, updated_at`, plus a
    many-to-many `AgentToolRow` link table granting specific tools to a
    specific agent (`ToolService`/`AgentService._link_tools()`).
  - `TaskRow` — `id, tenant_id, input, job_id, status, created_at,
    updated_at`. A thin edge-side handle: one task maps to exactly one job.
- **`TaskService` (`services/tasks.py`) — delegation, not ownership.** Job
  state transitions are not decided here; job_svc already enforces them
  atomically (§4.1), so this service never does a fetch-then-check-then-mutate
  on job state. Instead:
  - `create()` submits the job to job_svc *first*; only if that succeeds does
    it write the local `TaskRow` — so a rejected job never leaves an orphan
    task.
  - `approve()` and `retry()` both: look up the task's `job_id` (tenant-scoped,
    `NotFoundError` if missing or owned by another tenant), forward to
    job_svc (`start_job` / `retry_job` over the `JobGateway` protocol — see
    §4.7), then persist the refreshed status snapshot from job_svc's
    authoritative response. `approve()` is a pure pass-through: it does not
    special-case email or any other tool — the approval semantics live
    entirely in job_svc's job state machine and the orchestrator's tool
    workbench (§4.3), not in AES.
  - The remote call to job_svc is always made **outside** a DB transaction,
    so a slow/unreachable job_svc never pins a Postgres connection; the
    snapshot write is an absolute `UPDATE ... SET status = :status`, never a
    read-modify-write, so concurrent refreshes are last-writer-wins against
    the same upstream source of truth rather than a lost-update race. This is
    exercised directly by
    `test_concurrent_approvals_only_one_wins` (ten concurrent `approve()`
    calls on the same task: exactly one wins, nine get `StateError` from
    job_svc's own guard).
  - `job_svc` status strings map to task status strings via
    `_TASK_STATUS_FROM_JOB` (e.g. `queued → pending`, `waiting_approval →
    waiting_approval`, `dead/cancelled → failed`), which is also how
    `TaskStatus` in the proto is populated.
- **Auth (`auth.py`):** every RPC pulls `tenant_id` out of the `x-tenant-id`
  gRPC metadata header. RBAC is explicitly out of scope here — the module's
  docstring states the assumption plainly: an upstream edge is assumed to
  have already authenticated the caller and forwarded a verified tenant
  claim. Every downstream query is scoped by this `tenant_id`.

### 3.3 orchestrator — multi-agent execution

**Responsibility:** given a tenant and a chat turn, run that tenant's
configured agents as an AutoGen group chat, including tool calls, and return
the consolidated transcript.

- **RPC:** `OrchestratorService.Chat(ChatRequest) -> ChatResponse`. Unlike
  gateway's `Chat`, this one takes no model overrides or tool schemas from the
  caller — each agent already carries its own `LLMConfig` from the DB, and the
  tool schemas come from mcp_svc via the workbench, not the caller. It does
  carry one platform-specific field: `approved` — a one-shot signal that this
  call is resuming a job that previously paused on a human-approval gate
  (§4.3).
- **`groupchat.py` — `build_group_chat()`:** for a tenant, loads its agents +
  granted tool names (`agents_repo.list_agents_for_tenant`, a read-only query
  joining `AgentRow`/`AgentToolRow`/`ToolRow`), builds one AutoGen
  `AssistantAgent` per agent row with a system message derived from its
  `instructions`, and a `GatewayChatCompletionClient` (see below) pointed at
  gateway. Speaker order is LLM-driven (`SelectorGroupChat` +
  `_SELECTOR_PROMPT`), not fixed — closer to "the agent determines the
  appropriate sequence of actions" from the assignment brief than a
  hand-coded pipeline. `MaxMessageTermination` (config `[chat].max_messages`,
  default 20) is the safety valve against an unbounded chat: it's sized for a
  multi-step tool sequence (retrieve → draft → send), not just one Q&A turn.
- **`model_client.py` — `GatewayChatCompletionClient`:** a custom
  `ChatCompletionClient` implementation bridging AutoGen's tool-calling
  protocol onto gateway's `ChatRequest`/`ChatResponse` wire format, advertising
  `function_calling=True` so AutoGen's agents know they may emit tool calls.
  This exists because gateway is a bespoke gRPC service, not an
  OpenAI-compatible HTTP endpoint AutoGen could talk to directly.
- **`mcp_workbench.py` — `AgentToolWorkbench(Workbench)`:** see §3.3.2.
- **`run.py` — `run_chat()`:** drives one group-chat turn to completion,
  builds the transcript (`_to_transcript()`), and inspects every
  participant's workbench for a recorded `pending_approvals` entry after the
  run — if any exists, it overrides the turn's `finish_reason` to
  `APPROVAL_FINISH_REASON = "requires_approval"` (this constant must stay
  textually in sync with job_svc's `runner.py` copy — there's no shared
  package between the two services, so this is a hand-maintained invariant,
  called out explicitly in both files' docstrings).

#### 3.3.1 Why AutoGen's own MCP workbench isn't used

`autogen_ext`'s `McpWorkbench` imports `mcp.shared.context.RequestContext`,
which only exists in the `mcp` 1.x client. This project pins `mcp>=2,<3` (the
streamable-HTTP transport used by mcp_svc's server is the 2.x shape), so
`McpWorkbench` is import-incompatible. `AgentToolWorkbench` is a small,
from-scratch `Workbench` built directly on the 2.x `ClientSession` +
`streamable_http_client`, opening a short-lived, per-call MCP session (it's
deliberately stateless — a listing/call failure yields an empty tool set or an
error result rather than blocking the whole chat).

#### 3.3.2 Tool permissioning happens twice, on purpose

`AgentToolWorkbench` is constructed per-agent with that agent's specific
`allowed_tool_names` (from `AgentSpec.tool_names` in `agents_repo.py`).
`list_tools()` filters mcp_svc's catalog down to just that set, and
`call_tool()` refuses (locally, before ever reaching mcp_svc) any name outside
it. This is redundant with the per-agent instruction text also constraining
which tools an agent is told about — deliberately: the instruction text is a
suggestion to the LLM, not an enforcement boundary, so the workbench-level
allow-list is what actually prevents Agent A from invoking a tool only Agent B
was granted, even if the LLM hallucinates the call.

### 3.4 job_svc — the queue, runner, and poller

**Responsibility:** own job state, execute jobs by driving the orchestrator
step-by-step, checkpoint progress for resume, and implement the two-tier
retry/dead-letter budget.

- **RPC:** `CreateJob`/`GetJob`/`UpdateJob`/`RetryJob`. `CreateJob` accepts
  optional `max_attempts`/`max_retries` overrides (falling back to
  config-driven defaults — `default_max_attempts = 3`,
  `default_max_retries = 3`). `GetJob`'s filter supports filtering by id,
  status, and type. `UpdateJob` is the general status-transition entry point;
  `RetryJob` is the distinct budget-resetting manual retry.
- **Data model (`JobRow`):** `id, tenant_id, type, spec (JSON), status,
  attempts, max_attempts, retry_count, max_retries, progress (JSON),
  locked_at, locked_by, created_at, updated_at`.
- **Job state machine.** Six terminal-ish states plus one pause state:

  ```
                    ┌─────────┐  claim (attempt++, lease stamped)
     CreateJob ───▶ │ queued  │ ───────────────────────────────▶ ┌─────────┐
                    └────▲────┘                                  │ running │
        RetryJob(reset)  │        UpdateJob(queued)               └──┬───┬──┘
        attempts=0       │        (non-resetting requeue,             │   │
        retry_count++    │         from failed/dead/cancelled/         │   │
                    ┌─────┴───┐    waiting_approval)                   │   │
                    │ failed  │ ◀───────────────────────────────────────┘   │
                    └────┬────┘  fail: attempts<max_attempts → queued        │
                         │        else retry_count<max_retries → failed      │
                         │        else → dead                                │
                    ┌────▼────┐                                             │
                    │  dead   │ (terminal)                                   │
                    └─────────┘                                             │
                                                                              │
                    ┌───────────────────┐   pause on approval gate           │
                    │ waiting_approval  │ ◀──────────────────────────────────┘
                    └─────────┬─────────┘
                              │ UpdateJob(queued) via ApproveTask
                              ▼
                           queued (resumes from checkpoint)

     running ──▶ succeeded (terminal, via _finalize)
     {queued, running, waiting_approval} ──▶ cancelled (terminal, manual)
  ```

  Implemented as a single table of allowed source statuses per target
  (`_ALLOWED_ENTRY` in `services/jobs.py`), with `running` handled by `_start`
  (also consumes an attempt and stamps a lease) and `failed` handled by
  `_fail` (defers to `_next_status_after_failure`, a SQL `CASE` expression
  shared with the reaper — see below).
- **Two-tier retry budget, why two counters:** `attempts`/`max_attempts` is
  consumed automatically at claim time with **no caller involved** — while
  budget remains, a failed run is silently requeued (a transient error like a
  flaky orchestrator call or dropped connection self-heals with nobody
  noticing). `retry_count`/`max_retries` is a **separate, manual** budget: once
  the automatic budget is exhausted, the job rests at `failed` for a human (or
  an operator script) to call `RetryJob`, which resets `attempts` to 0
  (granting a fresh automatic cycle) while consuming one unit of
  `retry_count`. Only once *both* budgets are exhausted is the job
  dead-lettered (`dead`) — truly terminal. Without the second counter, a
  human's manual retry of a job that immediately fails again would have zero
  auto-retry budget left to absorb a second transient error; without the
  first, every single transient blip would require a human to intervene.
- **Atomic conditional-UPDATE concurrency control** (`_guarded_update`):
  every transition is `UPDATE jobs SET ... WHERE id=:id AND tenant_id=:t AND
  status IN (:allowed) RETURNING *`. If zero rows come back, the row is
  re-fetched to distinguish "doesn't exist / wrong tenant" (`NotFoundError`)
  from "exists but in the wrong state" (`StateError`) — no separate
  locking/version column is needed because the precondition on source status
  *is* the concurrency control: two concurrent transitions can't both pass,
  because the database evaluates the WHERE clause atomically per row.
- **Horizontal scaling of the poller — `claim_batch`:** every pod runs its own
  poller; `claim_batch` uses `SELECT ... FOR UPDATE SKIP LOCKED` so N pods
  claim disjoint batches from the same `queued` backlog instead of all
  racing for the same head-of-queue rows and mostly losing. This is a
  Postgres-specific feature; under SQLite (used in tests) the locking clause
  is a no-op — harmless there because SQLite's single writer already
  serializes access, so batches can't overlap regardless.
- **Crash recovery — lease + heartbeat + reaper.** Claiming a batch stamps
  `locked_at`/`locked_by` (a lease). `JobRunner._heartbeat` (in `runner.py`)
  renews that lease every `heartbeat_interval_seconds` (default 30s) while a
  job is actively running, so a long-running job isn't reaped out from under
  its own runner. `reap_expired(lease_seconds)` (config `[poller].
  lease_seconds`, default 300s) requeues any `running` job whose lease is
  older than the cutoff — the process that claimed it crashed before
  reporting a terminal status — using the *same*
  `_next_status_after_failure` escalation as a normal failure, so a
  reaped job draws down the same retry budget rather than getting an
  unlimited free pass. Combined with progress checkpointing, a reaped job
  resumes from its last completed step rather than restarting.
- **`save_progress` / `renew_lease` are system-wide, not tenant-scoped,** on
  purpose — the poller and runner are platform-internal workers acting on
  behalf of no particular caller, whereas every tenant-facing RPC
  (`create`/`get`/`update`/`retry`) *is* tenant-scoped.

#### 3.4.1 Execution checkpointing (`runner.py`)

Every job's `progress` JSON column is the resume state:
`{phase, plan, steps: {index: {prompt, output, finish_reason, agent}},
result, error, pending_approval}`.

- **Plan once, never re-decompose on resume.** The first attempt asks the
  orchestrator to break the job's prompt into an ordered list of
  independently-executable sub-prompts and checkpoints that list into
  `progress["plan"]`. Every subsequent attempt (retry, resume-after-crash,
  resume-after-approval) reuses the stored plan verbatim. This is a
  deliberate trade-off: re-decomposing on every retry would risk the LLM
  returning a *different* breakdown, which would desynchronize from the
  steps already recorded as completed — reusing the plan sacrifices "the plan
  might improve on retry" for "resume is actually correct."
- **Steps complete at most once.** `_execute_steps` skips any index already
  present in `progress["steps"]` and checkpoints a step's result
  (`save_progress`) the instant it succeeds, before moving to the next one —
  so a crash between step *N* completing and step *N+1* starting loses
  nothing: the resumed attempt sees step *N* in `steps` and starts at *N+1*.
  This directly satisfies the assignment's "recover without unnecessarily
  repeating completed work" requirement.
- **Approval pauses are not checkpointed as completed.** When a step's
  orchestrator call comes back with `finish_reason ==
  APPROVAL_FINISH_REASON`, the runner records a `pending_approval` marker
  (which step, what prompt, what detail) but deliberately does **not** add it
  to `steps` — because it hasn't actually completed. It raises
  `ApprovalRequired`, which `run()` catches and turns into a
  `waiting_approval` job rather than a `failed` one (a pause is a normal
  outcome, not an error). On resume, that *exact* step is re-run with
  `approved=True` passed through to the orchestrator (matched by
  `pending.get("step") == index` — a one-shot grant scoped to that specific
  step, not a durable bypass for the rest of the job's mutating calls).
- **Failure reason is checkpointed, not just logged.** `progress["error"]` is
  set to the failing exception's message before the runner re-raises, so a
  caller inspecting a `failed`/`dead` job via `GetJob` can see *why* without
  correlating log lines — directly satisfying the "execution history" section
  of the assignment ("What failed?").
- **The runner never lets an exception escape to the poller.** `run()` catches
  everything: `ApprovalRequired` → pause, anything else → `_mark_failed` (which
  hands off to job_svc's `_fail`, i.e. the retry-budget decision). A failed job
  is a normal, expected outcome from the poller's point of view, not a poller
  fault — keeping the poller loop itself simple and never crash-looping on a
  bad job.

### 3.5 mcp_svc — tool catalog and execution

**Responsibility:** serve a tenant's tool catalog over MCP, and execute the
tools that have a registered code binding.

- **Transport:** MCP 2.x streamable-HTTP server (`server.py`), listening on
  `:8003` (`config.toml`). `[security].allowed_hosts`/`allowed_origins` are
  present (empty by default, since the service is assumed to sit behind a
  trusted edge in this deployment) as the DNS-rebinding/Origin-check knobs the
  transport supports, for a deployment that puts mcp_svc directly on an
  untrusted network path.
- **Catalog vs. execution — two separate concerns on purpose.** The `tools`
  table (owned by AES, mirrored read-only into mcp_svc's `models.ToolRow`) is
  just a per-tenant catalog: name, description, the `mutating` flag. It
  carries no code. Execution bindings live in `handlers.py` as a small
  in-process registry (`get_handler(name) -> Handler | None`), mirroring the
  pattern where each tool's implementation and its input schema live in a
  code module, not a database row. `server.py`'s `_on_call_tool` gates on
  catalog ownership *first* (does this tenant have a row with this name?),
  then dispatches to `get_handler`; a catalog entry with no registered
  handler returns a normal (non-crashing) "no execution binding configured"
  result rather than erroring the whole call. **Two handlers are implemented
  today: `http_request` and `query_database`.** The seeded demo catalog also
  advertises `web_search`, `calculator`, and `send_email` as catalog-only
  entries with no execution binding — they exist to exercise per-agent tool
  *permissioning* and the `mutating` approval gate end-to-end without
  requiring a real external email/search integration. Wiring a live
  `send_email` handler would need nothing more than a new `Handler` entry in
  `handlers.py`; the approval-pause mechanism it would exercise (§4.3) is
  already fully generic and doesn't need to change to support it.
- **`http_request` — SSRF-guarded generic HTTP tool.** Before dispatching any
  agent-controlled URL, `_assert_url_is_public()` rejects non-`http(s)`
  schemes, a small blocked-hostname set (`localhost`, `metadata`,
  `metadata.google.internal`, `*.localhost`), and IP-literal targets that are
  private/loopback/link-local/reserved/multicast/unspecified. This is a
  static check on the literal host only — no DNS resolution — so it stops the
  common case (an agent tricked into requesting
  `http://169.254.169.254/...` or `http://localhost/...`) but **not**
  DNS-rebinding (a public hostname resolving to a private IP at request
  time); closing that fully needs a resolver-pinning transport or
  network-level egress control, called out explicitly as a known gap rather
  than silently ignored. Network/HTTP errors are caught and returned as a
  structured `{"error": ...}` payload rather than raised, so the model
  gets a usable result to reason about (e.g. retry, tell the user) instead of
  an opaque tool-call failure.
- **`query_database` — SQL-parsed tenant isolation, not regex.** The tool
  accepts a single SQL string and must guarantee it can only read the
  caller's tenant's rows from an allow-listed pair of demo tables
  (`customers`, `invoices`). The query is parsed with `sqlglot`
  (`_validate_query_database_sql`) and rejected if it isn't a single
  statement, isn't a plain `SELECT`, defines its own CTE (a `WITH customers
  AS (...)` could otherwise shadow the allow-list check), references a
  schema-qualified table (`public.customers` — would bypass a
  name-only allow-list), or references any table outside
  `{customers, invoices}`. Every allowed table reference is then rewritten in
  the parsed AST to a distinct scoped name (`customers → __tenant_customers`,
  not just reusing `customers` — some engines reject a CTE shadowing a table
  it also selects from), and the whole thing is run beneath a prepended
  tenant-filtering CTE bound to the caller's `tenant_id` as a query
  parameter. The result: no shape of SELECT the caller can write (join,
  subquery, aggregate) can see another tenant's rows, because the tenant
  filter is structural (in the query the database executes), not something
  the tool's Python code has to remember to apply on the result set. Results
  are capped at 100 rows to bound how much can land in the model's context.
- **`APPROVAL_REQUIRED` sentinel.** A handler that needs to pause for
  approval (none currently do, since neither shipped handler is `mutating`)
  would return a result string containing the literal marker
  `"APPROVAL_REQUIRED"`; `orchestrator/mcp_workbench.py` watches for that
  exact string (`APPROVAL_REQUIRED_MARKER`) in a tool result to detect the
  pause. In practice, the approval gate is enforced one layer up, in the
  workbench itself (§4.3) — the mcp_svc-side marker exists so a handler could
  *also* refuse conditionally on its own logic (e.g. only large amounts need
  approval) rather than every call to a `mutating` tool always pausing.

## 4. Cross-cutting mechanisms

### 4.1 Concurrency: the conditional-UPDATE pattern

Every state-owning service (job_svc for jobs, AES for tasks) uses the same
shape for every mutation: `UPDATE <table> SET ... WHERE id = :id [AND
tenant_id = :t] AND status IN (:allowed_source_statuses) RETURNING *`. If the
row comes back, the transition succeeded; if not, a second read distinguishes
"not found / wrong tenant" from "wrong state," and the caller gets a typed
`NotFoundError` or `StateError` respectively. This means:

- No optimistic-locking version column and no explicit row lock are needed —
  the precondition on source status *is* the lock, enforced by the database's
  atomic evaluation of the WHERE clause per row.
- It scales to any number of concurrent callers without contention beyond
  normal row-level locking: two racing `approve()` calls on the same task
  each attempt the same conditional UPDATE, but only one can see the
  precondition satisfied (Postgres serializes the two UPDATEs; whichever
  commits first changes the status out from under the other). This is
  exercised directly in `test_task_service.py`
  (`test_concurrent_approvals_only_one_wins`) and job_svc's own test suite.

### 4.2 Idempotency and duplicate requests

- **Job claiming is exactly-once per attempt.** `claim_batch`'s
  `SKIP LOCKED` + atomic UPDATE guarantees a given job is claimed by exactly
  one poller at a time; there is no window where two pods both believe they
  own the same job.
- **Step execution is at-most-once per attempt, exactly-once overall.** A
  step is only ever executed if its index is absent from
  `progress["steps"]`; the checkpoint write happens immediately after success
  and before moving to the next step, so a crash can't cause a step to be
  silently skipped (the step wasn't checkpointed, so it re-runs) or double
  counted in the final result (the checkpoint always reflects the true
  execution state at the time of the crash).
- **Duplicate task submission** is a client-level concern this platform does
  not currently deduplicate (e.g. no idempotency-key parameter on
  `CreateTask`) — each `CreateTask` call unconditionally creates a new task
  and a new job. Flagged here as a known gap rather than silently absent: a
  production version would accept a caller-supplied idempotency key on
  `CreateTaskRequest` and upsert against it.
- **Approval is not re-appliable.** Once a `waiting_approval` job is resumed
  (`queued`), a second `ApproveTask` on the same task fails with
  `StateError` (the job is no longer `waiting_approval`) — approval is a
  one-time transition, not a durable flag, matching "waiting_approval →
  queued" being in `_ALLOWED_ENTRY`'s `queued` sources only from that state.

### 4.3 Human approval — a generic, tool-driven pause

The whole mechanism is driven by one boolean, `ToolRow.mutating`, not any
hardcoded notion of "email":

1. A tool is marked `mutating = true` in its catalog row (e.g. a real
   `send_email` handler would be).
2. `agents_repo.list_agents_for_tenant` surfaces, per agent, which of its
   granted tools are mutating (`AgentSpec.mutating_tool_names`).
3. `groupchat.build_group_chat` constructs each agent's `AgentToolWorkbench`
   with that set. When the model calls a mutating tool and the run has not
   been granted `approved=True` for that exact call, `call_tool` refuses
   **locally** — mcp_svc is never even contacted, so an unapproved mutating
   call has zero side effects — and records a `pending_approvals` entry
   containing the `APPROVAL_REQUIRED_MARKER`.
4. `run_chat` notices any participant's `pending_approvals` after the turn
   and overrides the turn's `finish_reason` to `"requires_approval"`
   (`APPROVAL_FINISH_REASON`).
5. job_svc's runner treats that finish reason as `ApprovalRequired`, not a
   failure: it records which step paused and why, and transitions the job to
   `waiting_approval` (not consuming any additional retry budget beyond the
   attempt already spent) — the job simply waits, indefinitely, until a human
   acts. The poller and reaper both ignore `waiting_approval` jobs (`_start`
   only claims `queued`, `reap_expired` only reaps `running`), so a paused job
   is never auto-advanced or reaped for exceeding a lease.
6. A human calls `AES.ApproveTask`, which resolves the task's `job_id` and
   calls job_svc's `UpdateJob(queued)` — the non-resetting requeue path (not
   `RetryJob`, which is reserved for the *failed* retry budget). The poller
   re-claims the job on its next tick and the runner re-executes the paused
   step, this time passing `approved=True` scoped to that exact step index —
   so the *next* mutating call the same job makes (if any) still gates unless
   it too is the resumed step.

This design means adding a new mutating tool (a real `send_email`, a
"delete customer record" tool, a payment tool, ...) requires **zero** changes
to the approval mechanism itself — only setting `mutating = true` on its
catalog row.

### 4.4 Execution history / observability

`GetJob`/`GetTask` answer every question the assignment's "Execution History"
section asks for, without a separate audit log:

| Question | Where it's answered |
|---|---|
| What task was requested? | `TaskRow.input` / `JobSpec.agent_execution_spec.instructions` |
| Which agent executed it? | `JobStep.agent` per step in `progress["steps"]` |
| Which steps were performed? | `progress["plan"]` + `progress["steps"]` (prompt/output per step) |
| Which tools were called? | Recoverable from each step's orchestrator transcript (tool-call messages); tool identity is not currently persisted as a first-class field — see gap below |
| What failed? | `progress["error"]` (latest failure message) |
| What was retried? | `JobRow.attempts`/`retry_count` vs. `max_attempts`/`max_retries` |
| What is the current status? | `JobRow.status` / `TaskRow.status` |
| What was the final result? | `progress["result"]` |

**Gap, called out honestly:** individual tool calls and their arguments/results
within a step are not separately persisted rows — they live inside the
orchestrator's transcript for that turn, which is not itself stored beyond
the final message. A production system would want a `tool_calls` table (job
id, step index, tool name, arguments, result, timestamp) for precise
"which tools were called, with what, and what did they return" auditing —
currently satisfiable only by re-running with logs, not by querying
persisted state.

### 4.5 Multi-tenant isolation

Enforced at every layer that touches data, redundantly by design (belt and
suspenders, since a single missed filter would otherwise leak data):

- **Metadata:** every gRPC call carries `x-tenant-id`; every service's
  `auth.py`-equivalent extracts it and raises `AuthenticationError` if
  missing.
- **Query scoping:** every SQLAlchemy query in every service filters by
  `tenant_id` — `TaskService.get`, `JobService.get`, `agents_repo`'s tool
  join, etc.
- **SQL-level scoping inside a tool:** mcp_svc's `query_database` goes one
  step further and enforces tenant isolation *inside the SQL the database
  executes* (the tenant-scoping CTE, §3.5) rather than trusting the
  application layer to filter results after the fact — the strongest of the
  three layers, since it holds even if a future caller of the same handler
  forgot to check the tenant on the returned rows.
- **Cross-tenant negative tests exist** at the service-unit level (e.g.
  `test_approve_other_tenant_task_not_found`, `test_get_returns_only_own_tenant_tasks`)
  rather than only at the integration level — cheaper to run and pinpoint a
  regression to.

### 4.6 Error handling

Every service defines its own typed `AppError` hierarchy (`NotFoundError`,
`ValidationError`, `StateError`, `ConflictError`, `DependencyError`,
`GuardrailRejected`, `ProviderError`, ...) and a `_handle_errors` decorator
mapping each to a specific gRPC status code (`_STATUS_BY_ERROR`), applied
uniformly across every servicer method. This gives callers a small, stable
vocabulary to branch on (e.g. `StateError → FAILED_PRECONDITION` means "retry
after checking state," `DependencyError → UNAVAILABLE` means "safe to retry
the call itself") without leaking each service's internal exception types
across the wire. `DependencyError` in particular is how a downstream gRPC
failure (job_svc calling orchestrator, orchestrator calling gateway) is
translated at the client boundary, rather than letting a raw `AioRpcError`
propagate.

### 4.7 Protocol-based dependency injection

Rather than depending on the generated protobuf stub directly, each service
that calls another defines a small `Protocol` (`JobGateway` in AES,
`OrchestratorGateway` in job_svc) describing exactly the methods it needs, in
plain dataclasses (`JobRef`, `ChatTurn`, `ChatReply`) with no protobuf types
in the signature. The production implementation (`JobClient`,
`OrchestratorClient`) wraps the real stub and translates gRPC errors into the
service's own typed errors; unit tests inject a fake implementing the same
`Protocol` (e.g. `FakeJobGateway`) to exercise `TaskService`/`JobRunner`
without standing up a real dependent service. This is why the full unit
test suites (250+ tests across all five services) run in
seconds with no service processes, containers, or network calls involved.

### 4.8 Connection reuse

Every gRPC client (`JobClient`, `OrchestratorClient`, AES's gateway stub) is a
single, lazily-initialized, process-wide channel, created on first use and
reused for every subsequent call — gRPC channels multiplex concurrent RPCs
over one HTTP/2 connection, so this is both cheaper (no per-call handshake)
and correct (`_get_stub()`'s lazy-init has no `await` before the assignment,
so under a single event loop the first caller fully initializes the singleton
before any other observes it — safe without an explicit lock).

### 4.9 Cost and latency of LLM calls

- **Planning is a separate, cheap LLM call from execution**, and is
  checkpointed so it happens exactly once per job regardless of how many
  times the job is retried or resumed (§3.4.1) — the single biggest lever
  against wasted LLM spend on a job that fails and retries repeatedly.
- **`MaxMessageTermination`** bounds the worst case of an unbounded
  tool-calling loop or a group chat that never converges on an answer.
  **`max_input_chars`** in gateway bounds the cost of a single oversized
  request before it ever reaches the model.
- **`token_usage` is threaded back through every layer** (gateway →
  orchestrator's summed `TokenUsage` → available to a caller of
  `OrchestratorService.Chat`), which is the primitive a cost-tracking or
  rate-limiting layer would build on — not implemented as a bonus feature
  here, but the field exists precisely so it can be.

## 5. Reliability checklist (assignment §"Reliability")

| Failure mode | Handling |
|---|---|
| LLM failures | `ProviderError` (gateway) → `DependencyError` at every calling layer → job `_fail` → auto-retry within budget |
| Tool/API failures | mcp_svc handlers catch and return structured `{"error": ...}` payloads (model can reason about it) rather than raising; workbench-level exceptions become an `is_error` `ToolResult` rather than crashing the chat |
| Timeouts | Configured per hop (`gateway.timeout_seconds`, `mcp.timeout_seconds`, `tool_execution.timeout_seconds`) so a stuck downstream call fails fast into the retry path rather than hanging a worker indefinitely |
| Temporary infra failures | Two-tier retry budget (§3.4) absorbs transient failures without caller involvement while `attempts` budget remains |
| Worker/process failures | Claim lease + heartbeat + reaper (§3.4) — a crashed runner's job is requeued from its last checkpoint, not lost or stuck forever |
| Invalid tool requests | Handlers validate required arguments and raise `ValueError` before doing any work (e.g. `http_request` requires `method`+`url`); `query_database` rejects malformed/disallowed SQL structurally |
| Duplicate requests | Job claiming is exactly-once (§4.2); task/job *submission* dedup is a known, called-out gap (§4.2) |
| Unexpected LLM behaviour | Planner output is parsed defensively (`_parse_plan` — JSON array preferred, falls back to bullet/numbered-line splitting so prose-wrapped answers still yield a usable plan); an empty plan raises `RunnerError`, failing the job cleanly instead of silently no-op'ing |

## 6. Testing

Each service ships its own `pytest` suite exercising the mechanisms above
directly (state-machine transitions, concurrency races, tenant isolation,
SSRF/SQL-injection-style bypass attempts, resume-after-crash checkpointing)
against fakes/in-memory SQLite rather than requiring live dependent services —
see §4.7. A top-level `tests/` project additionally provides a
`ServiceManager` harness capable of booting all five services against real
Postgres for full end-to-end scenarios; that suite is heavier (spins up real
service processes) and is treated as a separate, opt-in verification layer
from the fast per-service unit suites.

## 7. Known gaps and deliberate scope cuts

Being explicit about what's *not* done, and why, per the assignment's request
to "explain the approach and trade-offs":

- **No real `send_email`/`retrieve_invoices`/`retrieve_customer`/
  `create_email_draft` handlers.** The seeded catalog advertises these names
  (mirroring the assignment's example accounting-assistant tools) so that
  per-agent tool *permissioning* can be demonstrated end-to-end, but only
  `http_request` and `query_database` have code behind them. The approval
  mechanism (§4.3) and the generic tool-execution plumbing (mcp_svc handler
  registry, workbench allow-listing) do not need any changes to support a
  real `send_email` — it's additive, not a redesign.
- **No idempotency key on `CreateTask`/`CreateJob`** (§4.2) — duplicate
  *submissions* (as opposed to duplicate *execution* of an already-submitted
  job, which is fully handled) are not deduplicated.
- **SSRF protection is host-literal, not DNS-rebinding-proof** (§3.5) —
  called out explicitly rather than implied to be complete.
- **No per-tool-call audit trail** beyond what's inside a step's transcript
  (§4.4) — sufficient to answer "what was the outcome" but not "list every
  tool call this job ever made" via a single query.
- **No rate limiting, LLM provider fallback, or distributed tracing** — all
  listed as bonus/optional in the assignment; the error-handling and
  dependency-injection patterns (§4.6, §4.7) are structured so that adding
  them later (a `RateLimiter` in gateway, a second `Provider` implementation,
  OpenTelemetry spans around each gRPC/MCP call) is additive rather than a
  refactor.
- **Shared-database coupling (deliberate).** The platform runs against a
  single shared Postgres instance, so both orchestrator and mcp_svc read
  AES-owned tables directly (their own read-only ORM mappings of `agents` /
  `tools`) rather than calling AES over gRPC for that data. This is an
  intentional choice — one database instance to operate, and lower latency on
  every group-chat construction / `tools/list` than an extra RPC hop — and is
  safe because neither service writes those tables. The trade-off: it couples
  three services to one schema, so splitting them onto physically separate
  stores later would first require replacing those direct reads with an
  internal `GetAgent`/`GetTool`-style RPC or a cache/event feed. See §9 for
  the full topology.

## 8. Deliverable / evaluation mapping

| Assignment ask | Where it's satisfied |
|---|---|
| Create and manage agents, configurable tools | AES `AgentService`/`ToolService` (§3.2) |
| Accept and execute tasks via an LLM | AES `CreateTask` → job_svc → orchestrator → gateway (§1) |
| Multi-step execution | job_svc's plan/execute loop (§3.4.1) |
| State + history of an execution | `JobRow.progress`, `GetJob`/`GetTask` (§4.4) |
| Asynchronous execution | `CreateTask` returns immediately; poller executes in the background (§1, §3.4) |
| Failures and retries | Two-tier retry budget (§3.4) |
| Resume after failure | Checkpointed `progress` + lease/reaper (§3.4.1, §4.2) |
| Human approval | Generic `mutating`-flag gate (§4.3) |
| Multiple users, concurrent executions | Tenant scoping everywhere (§4.5) + atomic conditional updates (§4.1) + horizontally-scalable poller claiming (§3.4) |
| Security / guardrails | gateway guardrails (§3.1), mcp_svc SSRF + SQL-parsed tenant scoping (§3.5) |
| Trade-offs explained | §2, §7 |

## 9. Ports, databases, and configuration reference

| Service | Port | Config file |
|---|---|---|
| gateway | 50054 (gRPC) | `services/gateway/config.toml` |
| agent_execution_service | 50051 (gRPC) | `services/agent_execution_service/config.toml` |
| job_svc | 50052 (gRPC) | `services/job_svc/config.toml` |
| orchestrator | 50053 (gRPC) | `services/orchestrator/config.toml` |
| mcp_svc | 8003 (HTTP/MCP) | `services/mcp_svc/config.toml` |

**Database topology.** All stateful services share a **single Postgres
instance** (`localhost:5432` in dev, configured under each `config.toml`'s
`[postgres]` section). Within that one instance there are two databases:

- `agent_execution_service` — owned by AES: the `agents`, `tools`, and `tasks`
  tables, plus the demo `customers`/`invoices` tables. **orchestrator**
  (agents/tools, for group-chat construction) and **mcp_svc** (tools +
  customers/invoices, for its handlers) connect to this same database and read
  those tables directly.
- `job_svc` — owned by job_svc: the `jobs` table.

**gateway** is stateless and has no database. So services are separated by
table/database ownership *within* one shared instance, not by an instance per
service — see the coupling trade-off in §7.
