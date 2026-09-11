# AI Agent Execution Platform

A backend platform for defining AI agents (instructions + LLM config + granted
tools), submitting tasks, and executing them **asynchronously** with multi-step
planning, real tool calls, two-tier retries, resume-after-crash, and a human
approval gate for mutating actions.

It is built as **five independently deployable services** over a single shared
Postgres instance. Services talk over gRPC, except tool execution, which uses
MCP over streamable HTTP.

This file is the single entry point that summarizes the whole system. Each
service keeps its own detailed `README.md` (linked below), and the full design
rationale lives in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## What it does

- **Define agents and tools** — per-tenant CRUD for agents (instructions, LLM
  config, granted tool ids) and a tool catalog (name, description, `mutating`
  flag).
- **Submit tasks** — a task is submitted once and executed asynchronously; the
  caller polls for status and result.
- **Plan then execute** — a task is decomposed into an ordered list of
  sub-steps once, then each step is executed by a multi-agent group chat.
- **Call tools for real** — agents call tools (HTTP requests, read-only SQL)
  end to end via MCP, not just described in a prompt.
- **Survive failure** — automatic + manual retry budgets, a dead-letter state,
  and crash recovery via a lease/heartbeat/reaper so a job resumes from its
  last completed step.
- **Pause for human approval** — any tool flagged `mutating` pauses the job at
  `waiting_approval` with zero side effects until a human approves.
- **Guard the edges** — a single LLM egress choke point with input guardrails,
  SSRF-guarded HTTP, and SQL-parsed tenant isolation for database reads.

## Tech stack

| Concern | Choice |
| --- | --- |
| Language / runtime | Python, one `uv`-managed project per service (each pins its own `.python-version` and dependencies) |
| Service-to-service RPC | gRPC + Protocol Buffers |
| Tool protocol | MCP 2.x over streamable HTTP (`mcp_svc`) |
| Multi-agent framework | AutoGen `SelectorGroupChat` (`orchestrator`) |
| LLM provider | Groq via its OpenAI-compatible endpoint (`openai/gpt-oss-20b`), behind `gateway` |
| Persistence | PostgreSQL via SQLAlchemy async (`asyncpg`); SQLite for unit tests |
| SQL safety | `sqlglot` parsing for the `query_database` tool |
| Testing | `pytest` with in-memory SQLite + fakes; a subprocess harness for e2e |

Rationale for the notable picks lives in [`ARCHITECTURE.md`](ARCHITECTURE.md) §2
(job queue vs. broker, five services, gRPC + MCP, AutoGen vs. a hand-rolled loop).

## Architecture at a glance

```
external caller
     │  gRPC: CreateTask / GetTask / ApproveTask / RetryTask
     ▼
agent_execution_service (AES)  ── owns agents/tools/tasks
     │  gRPC: CreateJob / GetJob / UpdateJob / RetryJob
     ▼
job_svc  ── owns jobs; poller + runner + reaper
     │  gRPC: Chat (1x decompose, then 1x per plan step)
     ▼
orchestrator  ── AutoGen SelectorGroupChat ──── MCP (tools/list, tools/call) ────▶ mcp_svc
     │  gRPC: Chat (per-agent LLM calls)                                            (catalog + execution)
     ▼
gateway  ── guardrails + egress ── HTTP (OpenAI-compatible) ──▶ LLM provider (Groq)
```

| Service | Port | Protocol | State / DB | Role |
| --- | --- | --- | --- | --- |
| [`agent_execution_service`](services/agent_execution_service/README.md) (AES) | `50051` | gRPC | `agents`, `tools`, `agent_tools`, `tasks` (DB `agent_execution_service`) | Tenant-facing CRUD + task↔job bridge |
| [`job_svc`](services/job_svc/README.md) | `50052` | gRPC | `jobs` (DB `job_svc`) | Async queue, poller, runner, retries, resume, approval state |
| [`orchestrator`](services/orchestrator/README.md) | `50053` | gRPC | none (reads AES DB) | Runs one multi-agent task turn incl. tool calls |
| [`gateway`](services/gateway/README.md) | `50054` | gRPC | none (stateless) | The only path to the LLM; guardrails + provider egress |
| [`mcp_svc`](services/mcp_svc/README.md) | `8003` | MCP over HTTP | reads AES `tools` + demo `customers`/`invoices` | Tool catalog + execution (`http_request`, `query_database`) |

The shared Postgres instance is separated by database/table ownership: AES owns
the `agent_execution_service` database (also read by orchestrator and mcp_svc),
job_svc owns the `job_svc` database, and gateway holds no state.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full request flow, state
machines, concurrency model, and the trade-offs behind each decision.

## Example use case: an accounting assistant

The assignment's motivating example — *"Find all overdue invoices above
₹50,000 and prepare reminder emails for the respective customers"*, pausing for
approval before actually sending — maps onto the platform like this:

1. **Submit** the task to AES (`CreateTask`); AES creates a job in `job_svc` and
   returns a task id immediately (async — the caller never blocks).
2. **Plan** — `job_svc`'s runner asks `orchestrator` to decompose the task into
   an ordered list of sub-steps, checkpointed once and reused on every resume.
3. **Execute** — for each step, `orchestrator` runs the tenant's agents as a
   group chat via `gateway`, calling tools through `mcp_svc` (e.g.
   `query_database` to retrieve overdue invoices and customers).
4. **Pause for approval** — when an agent tries to call a tool marked
   `mutating` (e.g. `send_email`), the workbench refuses it locally *before any
   side effect* and the job parks at `waiting_approval`, with the pending step
   checkpointed.
5. **Approve & resume** — a human calls `ApproveTask`; `job_svc` re-queues the
   job and the runner re-runs *that* step with approval granted, then finalizes
   and stores the result.

### Run it

Against a running fleet (see *Setup & run*); every call carries an `x-tenant-id`:

```sh
# 1. Register a mutating tool — its calls will require approval
grpcurl -plaintext -H 'x-tenant-id: t1' \
  -d '{"name": "send_email", "description": "Send an email", "mutating": true}' \
  localhost:50051 aep.agent_execution.v1.AgentExecutionService/CreateTool

# 2. Submit a task (returns a Task with an id + job_id)
grpcurl -plaintext -H 'x-tenant-id: t1' \
  -d '{"input": "Find overdue invoices over 50000 and email the customers"}' \
  localhost:50051 aep.agent_execution.v1.AgentExecutionService/CreateTask

# 3. Approve the paused step (task_id from the CreateTask response)
grpcurl -plaintext -H 'x-tenant-id: t1' \
  -d '{"task_id": "<TASK_ID>"}' \
  localhost:50051 aep.agent_execution.v1.AgentExecutionService/ApproveTask
```

**What's actually wired:** the shipped tool handlers are `query_database`
(read-only SQL over the demo `customers`/`invoices` tables — the stand-in for
"retrieve invoices/customers") and `http_request`. Names like `send_email` are
catalog entries that exercise per-agent permissioning and the `mutating`
approval gate end-to-end; wiring a real send only adds a handler in `mcp_svc`
(see [`ARCHITECTURE.md`](ARCHITECTURE.md) §7). `CreateTask` currently accepts
only `input` — agent selection is a known gap (see *Current limitations*).

## Services

### agent_execution_service (AES) — agents, tools, tasks

The tenant-facing edge API. gRPC CRUD for `agents` and `tools`, plus task
submit/read/approve/retry. AES executes nothing itself: `CreateTask` forwards
the task to `job_svc` as a job (creating the job **before** the local `tasks`
row so a rejected job never orphans a task) and returns immediately. It stays a
thin, fast, tenant-scoped record system in front of the execution pipeline;
each `tasks` row mirrors the status of exactly one job. Approve/retry delegate
the actual state transition to job_svc and persist whatever it reports back.
Details: [`services/agent_execution_service/README.md`](services/agent_execution_service/README.md).

### job_svc — queue, runner, and poller

The async job queue and execution engine, and where "submit a task, track it,
resume it after a crash, pause it for approval" is actually implemented. A
background poller claims queued jobs (`SELECT ... FOR UPDATE SKIP LOCKED` +
atomic conditional `UPDATE`) and drives each through a `JobRunner`: plan once,
execute each step at most once (checkpointing `progress` after every step so a
resumed run skips completed work), then finalize. It enforces a two-tier retry
budget (automatic `attempts`, manual `retry_count`) escalating to a terminal
`dead` state, and uses a lease + heartbeat + reaper for crash recovery.
Details: [`services/job_svc/README.md`](services/job_svc/README.md).

### orchestrator — multi-agent execution

Runs one multi-agent, multi-tool turn of a task. Builds an AutoGen
`SelectorGroupChat` over the tenant's configured agents (one `AssistantAgent`
per row), gives each agent a tool workbench scoped to its granted mcp_svc
tools, routes every model call through gateway, executes tool calls against
mcp_svc, and returns the transcript — pausing the turn instead of finishing it
if a tool requires approval. It is stateless across turns: every call rebuilds
the chat from Postgres plus the caller-supplied messages.
Details: [`services/orchestrator/README.md`](services/orchestrator/README.md).

### gateway — the only path to the LLM

A small stateless gRPC service that does exactly two things per call: run
in-process input guardrails (role allow-list, empty/oversized rejection, a
scoped phrase blocklist, PII redaction), then forward the sanitized request to
one configured chat-completions provider (Groq's OpenAI-compatible endpoint by
default) and translate the response — including tool calls — back. Nothing
upstream is allowed to call a model provider directly. Deliberately thin: one
model, no routing/fallback/caching/streaming.
Details: [`services/gateway/README.md`](services/gateway/README.md).

### mcp_svc — tool catalog and execution

The tool-execution boundary, and the one service that isn't gRPC: a real MCP
server over streamable HTTP. It reads the per-tenant tool catalog (owned by
AES) and supplies the executable behavior behind tool names via an in-process
handler registry. Two handlers ship today: `http_request` (SSRF-guarded generic
HTTP) and `query_database` (read-only, `sqlglot`-parsed SQL with structural
tenant isolation over the demo `customers`/`invoices` tables). It enforces
tenant isolation; per-agent tool permissioning is enforced upstream in
orchestrator. Details: [`services/mcp_svc/README.md`](services/mcp_svc/README.md).

## API & data model

**Key RPCs** (each service vendors its own proto and regenerates stubs with its
`scripts/gen_proto.sh`):

| Service (proto package) | RPCs |
| --- | --- |
| AES — `aep.agent_execution.v1.AgentExecutionService` | `Create/Get/Update/Delete` for `Tool` and `Agent`; `CreateTask`/`GetTask`/`ApproveTask`/`RetryTask` |
| job_svc — `aep.job.v1.JobService` | `CreateJob`, `GetJob`, `UpdateJob`, `RetryJob` |
| orchestrator — `aep.orchestrator.v1.OrchestratorService` | `Chat` |
| gateway — `aep.gateway.v1.GatewayService` | `Chat` |
| mcp_svc — MCP (streamable HTTP) | `tools/list`, `tools/call` |

**Core entities** (all tenant-scoped):

| Entity | Owner | Key fields |
| --- | --- | --- |
| `Agent` | AES | `id`, `name`, `instructions`, `llm_config{name,temperature}`, granted tool ids, `version` |
| `Tool` | AES | `id`, `name`, `description`, `mutating`, `version` |
| `agent_tools` | AES | `agent_id`, `tool_id` (the grant, many-to-many) |
| `Task` | AES | `id`, `input`, `job_id`, `status` (`PENDING`/`RUNNING`/`WAITING_APPROVAL`/`COMPLETED`/`FAILED`) |
| `Job` | job_svc | `id`, `type`, `spec`, `status`, `attempts`/`max_attempts`, `retry_count`/`max_retries`, `progress`, lease |

Full message definitions live in each service's `proto/` directory and README.

## Approach & key decisions

Each decision below is summarized here and explained in depth in the linked
package-level README (and in [`ARCHITECTURE.md`](ARCHITECTURE.md)):

- **Postgres-backed job queue instead of a broker** — atomic conditional
  `UPDATE ... RETURNING` transitions give claim/ack/retry semantics without
  adding an operational dependency. See
  [`job_svc`](services/job_svc/README.md) and [`ARCHITECTURE.md`](ARCHITECTURE.md) §2.
- **Five services split by responsibility and scaling profile** — CRUD edge,
  queue/worker, agent execution, LLM egress, and tool execution each isolate a
  distinct concern and trust boundary. See
  [`ARCHITECTURE.md`](ARCHITECTURE.md) §2–§3.
- **gRPC between services, MCP for tools** — typed internal contracts, with MCP
  as the interoperable protocol for exposing tools to agents. See
  [`mcp_svc`](services/mcp_svc/README.md) and
  [`orchestrator`](services/orchestrator/README.md).
- **AutoGen `SelectorGroupChat`, not a hand-rolled loop** — LLM-driven speaker
  selection so the agent decides the sequence of actions. See
  [`orchestrator`](services/orchestrator/README.md).
- **Resume via progress checkpoints** — plan once, execute each step at most
  once, checkpoint after every step so a resumed run skips completed work. See
  [`job_svc`](services/job_svc/README.md).
- **Generic human-approval gate** — driven by a single `mutating` tool flag and
  enforced locally before any side effect. See
  [`orchestrator`](services/orchestrator/README.md) and
  [`agent_execution_service`](services/agent_execution_service/README.md).
- **Single LLM egress choke point** — one gateway for guardrails, provider
  config, and token accounting. See [`gateway`](services/gateway/README.md).
- **Tenant isolation at every layer** — per-query scoping plus SQL-level
  scoping inside `query_database`. See
  [`mcp_svc`](services/mcp_svc/README.md) and
  [`ARCHITECTURE.md`](ARCHITECTURE.md) §4.5.

## Execution history & observability

The platform retains enough state to answer the assignment's execution-history
questions, primarily from a job's `progress` JSON checkpoint (read via
`job_svc`'s `GetJob`); AES's `GetTask` returns a cached status snapshot:

| Question | Where it's answered |
| --- | --- |
| What task was requested? | `Task.input` / the job spec `instructions` |
| Which agent executed it? | `agent` recorded per step in `progress["steps"]` |
| Which steps were performed? | `progress["plan"]` + `progress["steps"]` (prompt/output per step) |
| Which tools were called? | Inside each step's transcript — not yet a first-class table (known gap) |
| What failed? | `progress["error"]` (latest failure message) |
| What was retried? | `attempts`/`retry_count` vs. their maxima on the job |
| Current status? | `Job.status` / `Task.status` |
| Final result? | `progress["result"]` |

**Caveat:** `GetTask` only refreshes its snapshot on a mutating call
(`ApproveTask`/`RetryTask`), so a task left running reports its last-known status
until then — the authoritative live state is the job's `progress`.
**Observability today** is stdlib logging plus the persisted `progress["error"]`
and per-turn `token_usage`; metrics and distributed tracing are future work.
Full mapping: [`ARCHITECTURE.md`](ARCHITECTURE.md) §4.4.

## Production considerations

How each concern the assignment calls out is addressed (details in
[`ARCHITECTURE.md`](ARCHITECTURE.md) §4):

| Concern | Approach |
| --- | --- |
| Concurrency | Atomic conditional `UPDATE ... WHERE status IN (...) RETURNING`; `SELECT ... FOR UPDATE SKIP LOCKED` batch claim (§4.1) |
| Idempotency | A step runs once via the `progress["steps"]` checkpoint; job claim is exactly-once (submission dedup is a known gap) (§4.2) |
| Retries | Two-tier budget — automatic `attempts`, manual `retry_count` — then dead-letter (§3.4) |
| Timeouts | Bounded LLM egress at `gateway`; `job_svc→orchestrator` and `orchestrator→mcp` timeouts are known gaps (see *Current limitations*) |
| State management | The job's `progress` JSON is the single resume state (§3.4.1) |
| Failure recovery | Claim lease + heartbeat + reaper requeue a crashed job from its last checkpoint (§3.4) |
| Security | `gateway` guardrails; `mcp_svc` SSRF guard + `sqlglot`-parsed SQL; provider keys env-only (§3.1, §3.5) |
| Isolation (tenants) | `x-tenant-id` on every hop, per-query scoping, and a SQL-level tenant CTE in `query_database` (§4.5) |
| Observability | stdlib logging + persisted failure reason + per-turn token usage; metrics/tracing are future (§4.4) |
| Scalability | Stateless services; horizontally-scalable poller via `SKIP LOCKED`; one shared LLM egress (§2, §3.4) |
| Cost & latency of LLM calls | Plan once (checkpointed), message cap, input-size cap, token usage threaded back (§4.9) |

## Repository layout

```
.
├── ARCHITECTURE.md            # Full design doc: components, state machines, trade-offs
├── FIXES_PLAN.md              # Code-review findings and remediation plan
├── README.md                  # This file
├── services/
│   ├── agent_execution_service/   # AES — gRPC :50051
│   ├── job_svc/                   # queue + runner — gRPC :50052
│   ├── orchestrator/              # AutoGen group chat — gRPC :50053
│   ├── gateway/                   # LLM egress — gRPC :50054
│   ├── mcp_svc/                   # MCP tool server — HTTP :8003
│   └── postgres/                  # docker-compose for a local Postgres
└── tests/                     # End-to-end tests that boot the whole fleet
```

## Prerequisites

- **[`uv`](https://docs.astral.sh/uv/)** on `PATH` — each service is a
  self-contained project with its own pinned dependencies, launched via
  `uv run` inside its own directory.
- **Postgres** reachable on `localhost:5432`. The service configs default to
  user/password `postgres`/`postgres` and expect two databases:
  `agent_execution_service` and `job_svc`.
- **A model API key** for gateway — `GROQ_API_KEY` (default provider) or
  `OPENAI_API_KEY`.

## Setup & run

There is no single all-in-one script; bring up Postgres, then start each
service. The steps below are a clean-checkout path that matches the service
defaults (`postgres`/`postgres`, two databases).

**1. Start Postgres and create the two databases.** This is one self-contained
option that matches the service defaults (any Postgres works):

```sh
docker run -d --name aep-postgres \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 postgres:16-alpine

# create the two databases the services expect
docker exec -u postgres aep-postgres createdb agent_execution_service
docker exec -u postgres aep-postgres createdb job_svc
```

Alternatively use the provided `services/postgres/docker-compose.yml`, but note
its `.env` defaults to `aep`/`aep`/`aep` and creates a single `aep` database —
so either edit `.env` to `postgres`/`postgres` (and still create the second
database), or override every service's `[postgres]` settings via env vars
(`AEP_POSTGRES_*`, `JOB_SVC_POSTGRES_*`, `MCP_SVC_POSTGRES_*`,
`ORCHESTRATOR_POSTGRES_*`; see Configuration).

Tables are created automatically on service startup
(`Base.metadata.create_all`); there is no separate migration step. The demo
`customers`/`invoices` tables that `query_database` reads are seeded by
`tests/sql/sample_data.sql` (applied automatically by the integration harness;
apply it manually if you want them outside the test path).

**2. Start the services** (each in its own terminal). A sensible order is
gateway and mcp_svc first, then orchestrator, then job_svc, then AES:

```sh
# services/gateway
export GROQ_API_KEY=gsk-...   # or OPENAI_API_KEY=sk-... if provider = "openai"
uv sync && uv run gateway

# services/mcp_svc
uv sync && uv run mcp-svc

# services/orchestrator
uv sync && uv run orchestrator

# services/job_svc
uv sync && uv run job-svc

# services/agent_execution_service
uv sync && uv run agent-execution-service
```

Each `uv run` is executed from within that service's directory.

## Testing

**Unit tests** (fast, no live dependencies — SQLite + fakes). Run per service
from its directory:

```sh
uv run --group dev pytest
```

**End-to-end tests** boot the entire fleet plus its Postgres databases and
drive it over the real wire protocols:

```sh
# from tests/
uv run pytest -v
```

The e2e harness needs Postgres on `localhost:5432` (creates the two databases
if missing), `uv` on `PATH`, and a model API key (`MODEL_API_KEY` /
`GROQ_API_KEY`). Details: [`tests/README.md`](tests/README.md).

## Configuration

Each service reads a `config.toml` in its own directory (gRPC host/port,
Postgres, downstream addresses, and service-specific settings). Two override
mechanisms apply across services:

- **Config file path** — set `<SERVICE>_CONFIG_FILE`, e.g. `AEP_CONFIG_FILE`,
  `GATEWAY_CONFIG_FILE`, `JOB_SVC_CONFIG_FILE`, `MCP_SVC_CONFIG_FILE`,
  `ORCHESTRATOR_CONFIG_FILE`.
- **Individual fields** — set `<PREFIX>_<SECTION>_<FIELD>`, e.g.
  `AEP_POSTGRES_PASSWORD`, `JOB_SVC_POLLER_LEASE_SECONDS`,
  `MCP_SVC_POSTGRES_HOST`. This keeps secrets and per-deployment tuning out of
  committed TOML.

Provider API keys are **only** read from environment variables
(`GROQ_API_KEY` / `OPENAI_API_KEY`), never from `config.toml`.

## AI tools used

Per the assignment's disclosure requirement, AI coding assistants were used
while building this project:

- **Claude Code (Anthropic)** — primary AI pair-programmer: scaffolding and
  refactoring service code (gRPC servicers, config loaders, typed error
  hierarchies), drafting the design and service documentation
  ([`ARCHITECTURE.md`](ARCHITECTURE.md) and the per-service READMEs), and
  producing the structured code review in [`FIXES_PLAN.md`](FIXES_PLAN.md).
- **Windsurf / Cascade** — consolidating the per-service docs into this root
  `README.md` and evaluating the documentation against the assignment brief.

All AI output was reviewed, tested, and edited by a human; the architecture and
the engineering trade-offs are the author's own.

## Current limitations and known bugs

These are tracked in full — with file/line references and proposed fixes — in
[`FIXES_PLAN.md`](FIXES_PLAN.md). The highlights:

**Critical (P0)**

- **Committed test API key** — the e2e test (`tests/test_agent_execution.py`)
  falls back to a hardcoded, live-format Groq key; it must be rotated and
  replaced with an env-only lookup.
- **SSRF guard is bypassable** — `mcp_svc`'s `http_request` host check allows
  non-standard IP encodings (integer/hex/octal, trailing-dot host), so
  loopback / cloud-metadata targets can slip through; it needs a
  resolve-then-validate approach.
- **Hung orchestrator stalls a job forever** — the `job_svc → orchestrator`
  `Chat` call has no timeout and the heartbeat keeps renewing the lease, so a
  stuck call leaves the job `running` indefinitely instead of failing into the
  retry path.

**High (P1) — correctness & safety**

- **Approval isn't bound to the exact call** — `approved` is a coarse per-run
  boolean, so a resumed run may call a different mutating tool (or different
  args) than what the human reviewed.
- **The approval "pause" is a post-hoc relabel** — the group chat continues
  after a mutating refusal, so later non-mutating tool calls still execute and
  repeat on resume.
- **Step checkpoint is written after the side effect** — a crash in that window
  re-runs a (possibly mutating) step, making execution at-least-once rather than
  the documented at-most-once.
- **Terminal states are revivable** — `UpdateJob(queued)` / `ApproveTask` can
  requeue `dead`/`cancelled` jobs, and approving a `failed` task re-runs it
  outside the retry budget.
- **Batch-claim lease decay** — a job late in a claimed batch can have its lease
  expire before it starts, letting another pod double-run it.
- **A finished job can be dead-lettered** — a crash between the `completed`
  checkpoint and the `succeeded` transition lets the reaper mark it `dead`.
- **Timeouts / error codes** — `orchestrator`'s `mcp.timeout_seconds` is unused,
  and downstream outages surface as `INTERNAL` instead of a retryable
  `UNAVAILABLE`.
- **Tool safety** — `query_database` permits arbitrary SQL functions
  (`pg_sleep`, `pg_read_file`) with no statement timeout; `UpdateTool` can
  silently clear the `mutating` approval flag; `http_request` JSON responses are
  unbounded in size.

**Lower priority (P2) & gaps** — per-service robustness nits, several testing
gaps (AES CRUD happy-path, gateway servicer, a real AutoGen integration test,
Postgres-backed concurrency, deeper e2e scenarios), and doc reconciliation
between `ARCHITECTURE.md`'s intended design and the items above. All enumerated
in [`FIXES_PLAN.md`](FIXES_PLAN.md) §3–§6.

## Future extensions

The assignment calls out several optional/bonus areas. The platform is
structured so these are additive rather than redesigns; natural next steps:

**Agent / LLM execution**

- **Streaming execution updates** — `Chat` is unary today; stream token/step
  events to the caller for long multi-tool turns.
- **Parallel tool execution** — the group chat runs one speaker/tool at a time;
  independent tool calls within a step could run concurrently.
- **LLM provider fallback** — `gateway` is single-provider; add routing and a
  fallback provider on outage (the `Provider` seam is where it belongs).

**Cost, limits, and safety**

- **Token / cost management** — `token_usage` is already surfaced per turn;
  build per-tenant budgets and accounting on top of it.
- **Rate limiting** — per-tenant / per-agent request and token quotas at the
  gateway.
- **Finer-grained tool permissions** — per-agent tool grants exist; extend to
  per-argument / per-scope policies.

**Reliability & operations**

- **First-class execution cancellation** — cancellation exists via
  `UpdateJob(cancelled)`; expose it as a task-level API with in-flight
  interruption.
- **Dead-letter tooling** — the `dead` state exists; add inspection / replay
  tooling for dead-lettered jobs.
- **Advanced observability & distributed tracing** — today it is stdlib logging
  only; add metrics and a trace id propagated across the five hops.
- **Load testing** — a harness to validate the horizontally-scaled poller and
  the concurrency claims under load.

Broader production hardening (idempotency keys, DB migrations, a whole-system
Docker/compose, pagination) is tracked in [`FIXES_PLAN.md`](FIXES_PLAN.md) §5.

## Further documentation

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — system overview, per-service
  design, cross-cutting mechanisms (concurrency, idempotency, human approval),
  and the assignment-requirement mapping.
- **[`FIXES_PLAN.md`](FIXES_PLAN.md)** — a code-review findings/remediation
  plan (P0–P2), testing gaps, and outstanding deliverables.
- **Per-service READMEs** —
  [AES](services/agent_execution_service/README.md) ·
  [job_svc](services/job_svc/README.md) ·
  [orchestrator](services/orchestrator/README.md) ·
  [gateway](services/gateway/README.md) ·
  [mcp_svc](services/mcp_svc/README.md).
- **[`tests/README.md`](tests/README.md)** — integration test layout,
  lifecycle, seed/sample data, and prerequisites.
