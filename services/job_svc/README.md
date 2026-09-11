# job_svc

Generic async job queue and execution engine for the AI Agent Execution
Platform. It owns the `jobs` table, exposes `JobService` over gRPC
(`aep.job.v1.JobService`), and runs an in-process background poller that
claims queued jobs and drives each one to a terminal status by calling
`orchestrator`. This is where the platform's "submit a task, track it
asynchronously, resume it after a crash, pause it for human approval"
requirements are actually implemented.

## Where this sits in the platform

```
client → agent_execution_service --CreateJob--> job_svc (queued)
                                                    │
                                          poller claims it (running)
                                                    │
                                          JobRunner.run(job) ──Chat──> orchestrator ──Chat──> gateway (LLM)
                                                    │                        └──tool catalog──> mcp_svc
                                          progress checkpointed after
                                          every plan/step, terminal
                                          status written back to `jobs`
```

`agent_execution_service` (AES) is the user-facing edge: it validates a task
against an agent's config and calls `job_svc.CreateJob` with a
`JOB_TYPE_AGENT_EXECUTION` spec (agent name/instructions/LLM config/tool
ids), then hands the caller back a job id to poll. From there job_svc owns
the job end-to-end — AES never talks to `orchestrator` directly, and
`orchestrator`/`gateway`/`mcp_svc` never talk back to AES. job_svc is a
gRPC **server** for `aep.job.v1.JobService` (consumed by AES) and a gRPC
**client** of `aep.orchestrator.v1.OrchestratorService` (via
`orchestrator_client.py`).

## Data model

Single table, `jobs` (`models.py`, `JobRow`):

| column | purpose |
| --- | --- |
| `id`, `tenant_id` | primary key, tenant scoping (every query is filtered by `tenant_id`) |
| `type` | domain string, currently only `"agent_execution"` |
| `spec` | JSON blob — the protobuf `JobSpec` round-tripped via `mappers.spec_to_dict`/`spec_from_dict` |
| `status` | `queued` / `running` / `succeeded` / `failed` / `dead` / `cancelled` / `waiting_approval` |
| `attempts`, `max_attempts` | automatic retry budget, consumed once per claim |
| `retry_count`, `max_retries` | manual retry budget, consumed once per `RetryJob` call |
| `progress` | JSON checkpoint written by the runner (see below) — the resume/idempotency state |
| `locked_at`, `locked_by` | claim lease (crash-recovery lock) |
| `created_at`, `updated_at` | timestamps |

`progress` is the important one. Its shape (all keys optional/absent until
the runner reaches that phase):

```json
{
  "phase": "planning|executing|completed",
  "plan": ["sub-prompt 1", "sub-prompt 2", ...],
  "steps": {"0": {"prompt": "...", "output": "...", "finish_reason": "stop", "agent": "..."}},
  "pending_approval": {"step": 1, "prompt": "...", "detail": "..."},
  "error": "message from the most recent failed attempt",
  "result": "final answer, set once phase=completed"
}
```

`steps` is keyed by stringified step index (JSON object keys are strings);
`mappers.progress_to_proto` sorts and re-numbers them into the protobuf
`repeated JobStep` field.

## API surface (`aep.job.v1.JobService`, `proto/aep/job/v1/service.proto`)

- `CreateJob(type, spec, max_attempts?, max_retries?)` — validated by
  `JobValidator` (spec shape must match `type`; rejecting a malformed spec at
  creation avoids burning a whole retry budget in the runner before it dies —
  see `validators.py`). Defaults for `max_attempts`/`max_retries` come from
  `config.toml`'s `[jobs]` section when omitted.
- `GetJob(filter: ids | statuses | types)` — tenant-scoped read; this is how
  a caller polls a job's status/progress (the assignment's "track progress
  and final outcome" requirement). Once a job completes, its final answer is
  also surfaced as the typed `Job.result`
  (`JobResult.agent_execution_result.output`, mapped from `progress["result"]`)
  so a caller need not dig into the progress blob.
- `UpdateJob(id, status)` — a constrained state-machine transition (see
  below), not a free-form field update.
- `RetryJob(id)` — manual retry of a job resting at `failed`; resets
  `attempts` to 0 and consumes one unit of `retry_count`.

There is deliberately no `CancelJob`/`DeleteJob` RPC; `UpdateJob(status=
CANCELLED)` covers cancellation from `queued`/`running`/`waiting_approval`.

## Execution loop: poller + runner

### Claiming (`poller.py`, `services/jobs.py::claim_batch`)

`JobPoller.tick()` runs on an interval (`[poller].interval_seconds`) inside
the same asyncio event loop as the gRPC server (started as a task in
`main.serve`):

1. `reap_expired(lease_seconds)` — an atomic `UPDATE ... WHERE status='running'
   AND locked_at < now() - lease_seconds` that requeues (or fails/dead-letters,
   via the same escalation as a normal failure) any job whose claiming worker
   died without reporting a terminal status.
2. `claim_batch(limit, owner)` — `SELECT ... FOR UPDATE SKIP LOCKED` over the
   oldest `queued` rows, then an atomic `UPDATE` to `running` (incrementing
   `attempts`, stamping `locked_at`/`locked_by=owner`). `SKIP LOCKED` is what
   lets multiple poller pods share the backlog without contention: each pod
   walks away with a disjoint batch instead of all pods racing the same
   head-of-queue rows. `owner` is `hostname-pid-<random>` (`poller.py::
   _default_owner`), so a claim is traceable to a specific process.
3. Each claimed row is dispatched to a `RunnerDispatcher`, which looks up a
   runner by `row.type` (only `"agent_execution"` → `JobRunner.run` is
   registered) and fails the job outright if no runner matches.

A poller failure on one job (or one bad tick) is logged and does not kill the
loop or affect the rest of the batch — the loop is driven off a `stop_event`
so shutdown is prompt rather than `sleep`-based.

### Running one job (`runner.py::JobRunner`)

Three phases per job, each checkpointed into `progress` before moving to the
next:

1. **Plan** (`_ensure_plan`) — if `progress["plan"]` is already set (a
   resumed run), reuse it; otherwise call `orchestrator.Chat` with a
   planner system prompt asking for a JSON array of independent sub-prompts,
   parse the reply (`_parse_plan` — JSON array preferred, falls back to
   stripping bullet/numbered lines), and checkpoint `plan` + `phase=
   "executing"`. The plan is computed **once** and never recomputed on
   resume — recomputing could yield a different decomposition and desync
   already-completed step indices from a new plan.
2. **Execute** (`_execute_steps`) — walk `plan` in order; a step whose index
   is already a key in `progress["steps"]` is skipped (already done on a
   prior attempt — this is the "resume without repeating completed work"
   guarantee). Each step is a fresh `orchestrator.Chat` call; on success the
   output is written into `steps[str(index)]` and immediately persisted via
   `save_progress` — a crash right after this point cannot cause a re-run of
   that step. If a step's `finish_reason` is `requires_approval`, the runner
   stores a `pending_approval` marker (but deliberately does **not** add the
   step to `steps`, since it didn't complete) and raises `ApprovalRequired`.
3. **Finalize** (`_finalize`) — once every step is done, `phase=completed`,
   `error` cleared, `result` set to the last step's output, and the job is
   marked `succeeded`.

`JobRunner.run` wraps `_execute` with a background heartbeat
(`_heartbeat`, every `lease_seconds / 3`) that calls
`JobService.renew_lease` so a job whose real runtime exceeds the poller's
`lease_seconds` isn't reaped out from under a still-alive worker. Three
outcomes at the top level:

- `ApprovalRequired` → `UpdateJob(waiting_approval)` (job pauses; the poller
  and reaper both ignore this status, so it waits indefinitely).
- any other exception → checkpoint `progress["error"]`, then
  `UpdateJob(failed)`; job_svc's own retry budget (see below) decides the
  real next status.
- normal completion → already handled inside `_finalize`.

The runner never lets an exception escape to the poller — a failed job is a
normal, logged outcome, not a poller crash.

## Human approval

`orchestrator.Chat` takes a one-shot `approved: bool` on `ChatRequest` (see
`aep.orchestrator.v1.service.proto`); a tool that would mutate real-world
state (e.g. "send email") refuses to act and returns
`finish_reason=requires_approval` unless that flag is set for that specific
call. job_svc is the piece that turns that per-call signal into durable,
resumable state:

1. Step N returns `requires_approval` → job parks at `waiting_approval` with
   `progress["pending_approval"] = {"step": N, "prompt": ..., "detail": ...}`.
   The step is *not* recorded as completed, so it will run again.
2. A human-facing caller (through AES, presumably) calls
   `UpdateJob(id, QUEUED)` — the only way out of `waiting_approval` other
   than `CANCELLED` (`services/jobs.py::_ALLOWED_ENTRY`). This is a plain
   requeue: it does **not** reset `attempts`/`retry_count`, so the approval
   cycle draws down the same automatic retry budget as any other run.
3. On the next claim, `_execute_steps` recomputes `approved = (pending_approval
   is set and its step == index)` for step N specifically — a one-shot grant
   for *that* step, not a durable bypass for any other mutating call the job
   might later make — passes `approved=True` to `orchestrator.Chat`, and (if
   it now succeeds) clears `pending_approval` and records the step.

Note there is no explicit `Approve`/`Reject` RPC on `JobService` itself —
approval is expressed by transitioning the job back to `queued` (approve) or
to `cancelled` (reject) via `UpdateJob`. `orchestrator`'s test suite
(`services/orchestrator/tests/test_approval.py`) covers the tool-side gate
that produces `requires_approval` in the first place; this service only
owns the pause/resume state machine around it.

## Retries and failure handling

Two independent budgets, both enforced with atomic conditional `UPDATE`s
(`services/jobs.py`), not read-then-write:

- **Automatic** (`attempts`/`max_attempts`) — consumed at claim time
  (`_start`/`claim_batch`). A `running` job that fails or has its lease
  reaped is silently requeued (`failed → queued`, same row, `attempts`
  preserved) while budget remains — a transient error (flaky orchestrator
  call, dropped connection) is retried with no caller involvement.
- **Manual** (`retry_count`/`max_retries`) — once the automatic budget is
  exhausted the job rests at `failed` for a human/caller to notice and call
  `RetryJob`, which resets `attempts` to 0 (a fresh automatic cycle) and
  consumes one unit of `retry_count`.
- Once **both** are exhausted, the job is dead-lettered (`dead`) —
  `JobRow.status = "dead"` is genuinely terminal; nothing in this codebase
  transitions a `dead` job back out except a direct `UPDATE` (not exposed
  over the RPC surface). This is the platform's dead-letter mechanism; there
  is no separate dead-letter queue/topic.

The decision is centralized in one SQL `CASE` expression
(`_next_status_after_failure`), shared by `_fail` (explicit failure) and
`reap_expired` (crash detection), so both escalation paths behave
identically. `DependencyError` (raised by `orchestrator_client.py` on any
gRPC failure calling `orchestrator`) is the only failure type distinguished
by name in this service — it exists to make "the failure was a downstream
dependency, not this job's own logic" legible in logs, but it is still
treated as an ordinary retryable job failure by the runner.

There is no backoff — a requeued job re-enters the FIFO queue immediately
and is eligible for claim on the very next poller tick.

## Idempotency and duplicate requests

- **Job creation**: `CreateJob` has no client-supplied idempotency key or
  dedup check — two identical `CreateJob` calls create two distinct jobs.
  Duplicate-submission protection, if wanted, would need to live in AES
  (e.g. dedup on a caller-supplied task id) or be added here as an
  `idempotency_key` column with a unique constraint.
- **Step execution**: idempotent by construction. `_execute_steps` skips any
  step index already present in `progress["steps"]`, so re-running a job
  (via auto-retry, manual `RetryJob`, or reap-and-resume after a crash)
  never re-executes a completed step — this is the mechanism satisfying the
  assignment's "resume without repeating completed work" requirement.
- **Status transitions**: every transition is a single conditional `UPDATE
  ... WHERE status IN (allowed_from)`, so a duplicate/concurrent call that
  loses the race gets zero rows back and a `StateError`, rather than
  double-applying.

## Concurrency model

- **Claiming**: `claim_batch` uses `SELECT ... FOR UPDATE SKIP LOCKED` +
  a single atomic `UPDATE`, so N poller instances (one per pod, in a
  horizontally-scaled deployment) each claim a disjoint batch with no
  explicit coordination or shared lock service. Under SQLite (used in
  tests), `SKIP LOCKED` is a no-op — harmless, since SQLite's single-writer
  model already serializes access.
- **Crash recovery**: every claim stamps a lease (`locked_at`/`locked_by`,
  TTL = `[poller].lease_seconds`). `reap_expired` — run by every pod, every
  tick — requeues any `running` job whose lease has expired, so a pod dying
  mid-run never strands a job in `running` forever; it resumes from its last
  `progress` checkpoint on the next claim. A long-running job renews its own
  lease via a heartbeat (every `lease_seconds/3`) so it isn't reaped while
  still legitimately in flight.
- **State mutation**: every write to `jobs` (status transition, progress
  checkpoint, lease renewal) is a single-statement conditional `UPDATE`
  scoped by primary key (+ tenant for caller-facing RPCs), not a
  fetch-then-mutate-then-save round trip — so two concurrent writers can
  never each pass a stale check and then both commit. `save_progress` and
  `renew_lease` are safe as blind overwrites specifically because a job is
  claimed by exactly one runner at a time; nothing else writes `progress` for
  a `running` job.

## Multi-tenancy / auth

`auth.py::tenant_id_from_metadata` pulls `x-tenant-id` off gRPC metadata; every
`JobService` query and mutation is scoped by it (`WHERE tenant_id = :tenant_id`
folded into the same statement as the status guard). RBAC/authentication
itself is assumed to happen upstream (an already-authenticated edge decodes a
verified JWT and forwards the tenant claim) — this service only enforces
tenant *isolation* of data, not *authentication*. The background poller's
claim/reap/renew operations are deliberately **not** tenant-scoped — the
poller is a platform-level worker acting across all tenants, not a caller
acting on behalf of one.

## Observability

Structured-ish logging via the stdlib `logging` module (`INFO` by default,
set in `main.py`): poller start/stop, batch claim/reap counts, per-job
dispatch failures, and runner state transitions (`job %s failed (attempt
%d/%d, retry %d/%d); now %s`) that make attempts/retries/final-status
legible without a DB query. Failure detail is also persisted, not just
logged — `progress["error"]` on the job row itself means a caller inspecting
`GetJob` sees *why* a job failed without correlating log lines. There is no
metrics/tracing integration (no OpenTelemetry, no Prometheus counters) —
logs are the only observability surface today.

## Run it

```sh
uv sync
uv run job-svc
```

Needs Postgres reachable (`[postgres]` in `config.toml`) and, for the poller
to do anything besides log the backlog, `orchestrator` up on its configured
port (`[orchestrator]`). Config path can be overridden with
`JOB_SVC_CONFIG_FILE`; any field can be overridden by an env var named
`JOB_SVC_<SECTION>_<FIELD>` (e.g. `JOB_SVC_POLLER_LEASE_SECONDS=600`), so
secrets/tuning stay out of committed TOML.

Set `[poller].enabled = false` to run job_svc as a pure CRUD API with no
background execution (e.g. for a read-replica-style deployment).

## Test

```sh
uv run --group dev pytest
```

Runs entirely against an in-memory SQLite database (`aiosqlite` +
`StaticPool`) and a `FakeOrchestrator` test double (`tests/conftest.py`) —
no live Postgres or orchestrator required. Coverage: `test_job_service.py`
(state machine, both retry budgets, claim/reap/lease semantics),
`test_runner.py` (plan/execute/resume/approval/heartbeat behavior),
`test_poller.py` (batch claim + dispatch loop), `test_servicer.py` (gRPC
error-code mapping), `test_mappers.py`, `test_validators.py`.

## Known limitations

- No idempotency key on `CreateJob` — duplicate submissions from a retried
  client call create duplicate jobs.
- No backoff on auto-retry — a failed job is immediately re-eligible for
  claim, so a systemically-failing dependency gets hammered every poller
  tick rather than backed off.
- `dead` jobs have no path back to life over the RPC surface (by design —
  they're meant to be terminal — but there's also no operator tool/CLI to
  inspect or manually resurrect them beyond a direct DB edit).
- No cross-tenant rate limiting or fairness — the poller claims strictly
  oldest-first across all tenants, so one tenant's large backlog can starve
  another's.
- Approval resume is implemented generically at the job/step level, but the
  actual "is this tool call the kind that needs approval" decision lives in
  `orchestrator`'s tool workbench, not here — this service only reacts to
  the `requires_approval` signal it's given.
