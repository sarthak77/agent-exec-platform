# Fixes Plan — AI Agent Execution Platform

> **Purpose.** This is an implementation plan produced from a full code review of this
> repository against `Sarthak_Assignment.pdf` and general LLD/HLD principles. It is
> written to be **self-contained**: a fresh session with no prior context should be able
> to read this file top-to-bottom and implement the fixes without re-deriving the
> analysis. Work top-down (P0 → P1 → P2 → deliverables). Check items off as you go.
>
> **How this was produced.** AI-assisted review (Claude Code): the assignment PDF,
> `ARCHITECTURE.md`, all 5 proto contracts/configs were read; all unit suites were run;
> each service was deep-reviewed with the two security-critical validators (SSRF, SQL
> isolation) verified empirically. Paths below are **repo-relative** from the repo root.

---

## 0. Context primer (read first)

**What the system is.** A backend "AI Agent Execution Platform": users define agents
(instructions + LLM config + granted tools), submit tasks, and the platform executes them
asynchronously via an LLM with multi-step planning, tool calls, retries, resume-after-
failure, and human approval for mutating actions.

**Services (all under `services/<svc>/src/<svc>/`), single shared Postgres:**
- `agent_execution_service` (AES) — tenant-facing CRUD for agents/tools/tasks; bridges a task to one job. gRPC :50051. Owns `agents`/`tools`/`agent_tools`/`tasks`.
- `job_svc` — the queue + execution engine: poller claims jobs, runner drives the orchestrator step-by-step, checkpoints progress, two-tier retry/dead-letter, lease/reaper. gRPC :50052. Owns `jobs`.
- `orchestrator` — runs a tenant's agents as an AutoGen `SelectorGroupChat`; tool calls via MCP; LLM calls via gateway. gRPC :50053. Reads AES tables (read-only).
- `gateway` — the only path to the LLM (Groq, OpenAI-compatible); guardrails + provider egress. Stateless. gRPC :50054.
- `mcp_svc` — MCP (2.x streamable-HTTP) tool catalog + execution (`http_request`, `query_database`). HTTP :8003. Reads AES `tools` + demo `customers`/`invoices`.

**How to run the unit tests** (fast, no live deps — SQLite + fakes):
```
cd services/<svc> && uv run --group dev pytest -q
```
All five suites currently pass (job_svc 158, AES 64, orchestrator 25, mcp_svc 20, gateway 19 = 286). **After each fix, re-run the affected service's suite and add a test that would have caught the bug.**

**Key invariants the design intends (some are currently violated — that's what this plan fixes):**
- A job's `dead` and `cancelled` states are **terminal** (no revival).
- A completed step runs **at most once** (checkpoint before advancing).
- Human approval gates a **specific** mutating tool call; nothing mutating runs unapproved.
- Every service-to-service hop has a **timeout** so a stuck downstream fails into the retry path.
- Every tenant-facing query is **tenant-scoped**; tool execution enforces tenant isolation in SQL.
- The remote LLM call is held **outside** any DB transaction/connection.

**Cross-service invariant maintained by hand (keep in sync when touched):** the approval
finish-reason string `"requires_approval"` appears in both
`services/orchestrator/src/orchestrator/run.py` (`APPROVAL_FINISH_REASON`) and
`services/job_svc/src/job_svc/runner.py`. The approval marker `"APPROVAL_REQUIRED"`
appears in `orchestrator/run.py`, `orchestrator/mcp_workbench.py`, and
`mcp_svc/handlers.py`. There is no shared package. Consider extracting a shared constants
module (see P2-XCUT).

---

## 1. P0 — Fix before anything else

### [ ] P0-1 — Revoke & remove committed live API key
- **Severity:** Critical (secret leak)
- **Location:** `tests/test_agent_execution.py:40` — falls back to a hardcoded `gsk_…` Groq key.
- **Problem:** A working, live-format Groq API key is committed to source (and is in git history).
- **Fix:**
  1. **Revoke the key in the Groq console immediately** (rotate it) — removal from code is not enough; it's already in history.
  2. Remove the literal; require the key from env only (e.g. `os.environ["GROQ_API_KEY"]`), and `pytest.skip(...)` the live test if unset.
  3. Consider scrubbing history (`git filter-repo`) or at least noting the rotation.
- **Verify:** `grep -rn "gsk_" .` returns nothing; the e2e test skips cleanly without the env var set.

### [ ] P0-2 — SSRF guard is bypassable to cloud metadata / loopback
- **Severity:** Critical (SSRF)
- **Location:** `services/mcp_svc/src/mcp_svc/handlers.py:147-159` (`_assert_url_is_public`).
- **Problem:** The guard parses the host with strict `ipaddress.ip_address()` (dotted-quad/IPv6 only) and **allows anything that fails to parse**, but the request is then issued via httpx → `getaddrinfo`, which honors lenient `inet_aton` encodings. **Empirically verified bypasses (all currently ALLOWED):** `http://2852039166/` and `http://0xa9fea9fe/` → `169.254.169.254` (cloud metadata / IAM creds); `http://2130706433/`, `http://0x7f000001/`, `http://127.1/`, `http://0177.0.0.1/` → 127.0.0.1; `http://0/` → 0.0.0.0; `http://localhost./` (trailing dot) → 127.0.0.1.
- **Fix:** Don't allow-by-default on parse failure. Normalize/resolve the host and validate **every resolved IP** against the private/loopback/link-local/reserved/multicast/unspecified set:
  - Resolve via `socket.getaddrinfo(host, port)`, reject if **any** returned address is non-public.
  - Strip a trailing dot from the hostname before the blocked-name check; handle `inet_aton`-style integer/hex/octal/short forms (either normalize with `socket.inet_aton` before checking, or reject non-standard host syntaxes outright).
  - Best practice: **pin** the validated IP for the actual connection (resolve-then-connect-to-pinned-IP) to also close DNS-rebinding — or document that as a remaining gap explicitly.
  - Keep `follow_redirects=False` (currently correct); if redirects are ever enabled, re-validate each hop.
- **Verify:** Add the bypass encodings above to `tests/test_handlers.py` and assert each is **rejected**.

### [ ] P0-3 — Hung orchestrator leaves a job stuck in `running` forever
- **Severity:** Critical (unrecoverable liveness bug); also falsifies the "timeouts per hop" claim.
- **Location:** `services/job_svc/src/job_svc/orchestrator_client.py:101` (`Chat(...)` has no `timeout=`) + `services/job_svc/src/job_svc/runner.py:120-138` (heartbeat).
- **Problem:** The `Chat` gRPC call has no deadline and gRPC has no default. A hung/slow orchestrator blocks the runner indefinitely; meanwhile `_heartbeat` (a separate task) keeps renewing the lease, so `reap_expired` never treats the job as abandoned. The job is stuck in `running` **forever**, and (see P1-6) stalls the rest of its claimed batch.
- **Fix:**
  1. Add a per-call deadline on the orchestrator `Chat` (`timeout=` on the stub call), driven by config (add e.g. `[orchestrator].timeout_seconds` to `services/job_svc/config.toml`). On timeout, translate to `DependencyError` so it flows into the normal retry path.
  2. Ensure a hung call can't be masked by the heartbeat: bound total runner time per step, or stop renewing once a call exceeds a hard cap, so the reaper can reclaim.
- **Verify:** New test: a fake orchestrator that sleeps beyond the timeout → the step fails into `_fail` (retry budget consumed), job does **not** remain `running`.

---

## 2. P1 — Correctness & security bugs undermining headline features

### Human approval (a core requirement)

### [ ] P1-1 — Approval is not bound to the reviewed action
- **Severity:** High (safety/correctness of the approval feature)
- **Location:** `services/orchestrator/src/orchestrator/groupchat.py:107` + `services/orchestrator/src/orchestrator/mcp_workbench.py:161` (blanket `approved` bool); resume re-runs the whole sub-prompt at `services/job_svc/src/job_svc/runner.py:200-204`.
- **Problem:** `approved` is a coarse per-run boolean handed to every workbench and checked as a bare flag, and job_svc re-runs the *entire* sub-prompt on resume. The nondeterministic LLM may, on the approved re-run, call a **different** mutating tool, or the same tool with **different arguments**, than what the human was shown — all permitted. Approval authorizes "the step," not the specific call.
- **Fix:** Capture the exact pending call (tool name + a hash/normalized copy of arguments) when pausing; on resume, only permit a mutating call that **matches** the approved (name, args). A mismatch should re-pause (or fail) rather than execute. Thread the approved call identity through `ChatRequest` (extend the proto beyond the bare `approved` bool) into the workbench check.
- **Verify:** Test that a resumed run calling a *different* tool/args than the approved one is refused again, not executed.

### [ ] P1-2 — The "pause" is a post-hoc relabel, not a real suspension
- **Severity:** High (non-idempotent side effects)
- **Location:** `services/orchestrator/src/orchestrator/mcp_workbench.py:171-175` (refusal returns a normal result and chat continues) + `services/orchestrator/src/orchestrator/run.py:86-88` (finish_reason relabeled only after `team.run` returns).
- **Problem:** After a mutating call is refused, the group chat **continues to completion**. Any *non-mutating* tool invoked after the refusal executes real side effects, and because the whole step re-runs on resume, those side effects **repeat**. Agents may also claim completion in the transcript despite having paused, and tokens are wasted on post-refusal chatter.
- **Fix:** Terminate the turn promptly on the first mutating refusal (e.g. a custom termination condition on `pending_approvals`, or raise/short-circuit so `team.run` stops), so nothing after the refusal executes. Reconcile with P1-1 (capture pending call) and P1-3 (checkpoint semantics).
- **Verify:** Test that no tool call is executed after a mutating refusal in the same turn.

### [ ] P1-3 — Step checkpoint written after the side-effect (at-least-once, not at-most-once)
- **Severity:** High (idempotency; contradicts `ARCHITECTURE.md` §3.4.1)
- **Location:** `services/job_svc/src/job_svc/runner.py:200` (orchestrator executes) → `:229` (checkpoint persisted).
- **Problem:** A crash/reap between execution and checkpoint re-runs the (possibly mutating) step on resume. The guarantee is really **at-least-once**, not the documented "at most once / no double-execution window."
- **Fix (choose one, document the choice):**
  - Thread an **idempotency key** (job id + step index) to the orchestrator/tools so re-execution is safe; **or**
  - Accept at-least-once explicitly and update `ARCHITECTURE.md` §3.4.1 to stop claiming at-most-once; **or**
  - Persist an "intent" record before the side-effect and reconcile on resume.
- **Verify:** Test resume after a crash in the execute→checkpoint window and assert the desired semantics.

### `dead` must be terminal (violated from two directions)

### [ ] P1-4 — `_ALLOWED_ENTRY` lets `dead`/`cancelled` be requeued
- **Severity:** High (reliability invariant)
- **Location:** `services/job_svc/src/job_svc/services/jobs.py:72` — `_ALLOWED_ENTRY["queued"]` includes `dead` and `cancelled`.
- **Problem:** `UpdateJob(queued)` revives a job documented as "truly terminal." Also `_ALLOWED_ENTRY["dead"]` allows `running→dead` directly, not shown in the state diagram.
- **Fix:** Remove `dead` (and `cancelled`, unless intentional) from the `queued` source set so terminal states cannot transition out. Reconcile the diagram in `ARCHITECTURE.md` §3.4 with the final table.
- **Verify:** Test `UpdateJob(queued)` from `dead`/`cancelled` raises `StateError`. (Note: `test_update_queued_from_dead_preserves_attempts` currently asserts the *buggy* behavior — update it.)

### [ ] P1-5 — `ApproveTask` is over-permissive (unbounded re-run of failed jobs; revives dead)
- **Severity:** High (defeats retry budget + terminal state)
- **Location:** `services/agent_execution_service/src/agent_execution_service/services/tasks.py:83-86` → `job_client.py:105-113` (`start_job` sends unconditional `UpdateJob(QUEUED)`).
- **Problem:** No status precondition in AES; job_svc accepts `queued` from `{failed,dead,cancelled,waiting_approval}`. Approving a *failed* task re-runs it via the non-resetting path (never draws down `retry_count`, never reaches `dead`) → unbounded re-runs. Approving a *dead* task resurrects it. Untested (only `waiting_approval` is approved in tests) and **masked** by the test fake (see P1-9).
- **Fix:** Restrict approval to jobs actually `waiting_approval` — either add a precondition in AES before calling, or (better) tighten job_svc `_ALLOWED_ENTRY` per P1-4 so the approval requeue path only accepts `waiting_approval`. Ensure the error surfaces as `StateError`/`FAILED_PRECONDITION`.
- **Verify:** Test `ApproveTask` on a `failed`/`dead` task returns a state error, not a silent requeue.

### Reliability / recovery

### [ ] P1-6 — Batch-claim lease decay → cross-pod double-run; no intra-pod concurrency
- **Severity:** High (concurrency safety)
- **Location:** `services/job_svc/src/job_svc/poller.py:107-108` (sequential `for row in claimed`) + `services/job_svc/src/job_svc/services/jobs.py:255-268` (all leases stamped at claim time).
- **Problem:** `claim_batch(batch_size)` stamps `locked_at=claim-time` on **all** claimed rows, then the poller runs them **one at a time**. A job late in the batch may not start until earlier (LLM-latency) jobs finish; its claim-time lease can exceed `lease_seconds` before it ever starts → another pod's reaper requeues and re-claims it → **two runners on the same job**. `batch_size>1` also yields zero intra-pod concurrency.
- **Fix (choose one):** claim one job at a time; **or** process the batch concurrently (`asyncio.gather` with a concurrency cap) so leases reflect real progress; **or** (re)stamp the lease when each job's runner actually starts.
- **Verify:** Test/simulate a slow multi-job batch and assert no member's lease expires before it starts.

### [ ] P1-7 — A fully-completed job can be dead-lettered by a finalize-window crash
- **Severity:** Medium-High (data loss of successful work)
- **Location:** `services/job_svc/src/job_svc/runner.py:231-239` — `save_progress(phase=completed,result)` then a separate txn `update(succeeded)`.
- **Problem:** A crash between the two → job stays `running`; the reaper runs `_next_status_after_failure` and, if budget is exhausted, marks it `dead`, discarding a job whose steps all succeeded. The reaper doesn't consult `progress["phase"]=="completed"`.
- **Fix:** Make the reaper (and `_fail`) check `progress["phase"]=="completed"`/`result` and finalize to `succeeded` instead of escalating; or make finalize a single atomic transition.
- **Verify:** Test reap of a `running` job whose progress phase is `completed` → becomes `succeeded`, not `dead`.

### Timeouts / dependency errors

### [ ] P1-8 — `mcp.timeout_seconds` is dead config
- **Severity:** Medium (timeout not enforced on the tool hop)
- **Location:** declared `services/orchestrator/src/orchestrator/config.py:42` + `config.toml` (=10s) but never applied; only the gateway timeout is used at `model_client.py:149`.
- **Problem:** MCP tool calls fall back to library defaults (~30s connect / ~300s SSE read); a hung tool can block ~300s, not the configured 10s.
- **Fix:** Apply `settings.mcp.timeout_seconds` when creating the MCP client/session (`mcp_workbench.py`), and set the `ClientSession` request timeout.
- **Verify:** Test that a slow tool call is bounded by the configured timeout.

### [ ] P1-9 — Downstream outages surface as `INTERNAL`, not a retryable code
- **Severity:** Medium (breaks documented retryability; contradicts `ARCHITECTURE.md` §4.6)
- **Locations & two parts:**
  - **AES:** `services/agent_execution_service/src/agent_execution_service/errors.py` has **no** `DependencyError`; `job_client.py:124` raises base `AppError` for unmapped codes; `servicer.py:64-66` maps it to `INTERNAL`. A transient job_svc outage tells the caller "server bug, don't retry" instead of `UNAVAILABLE`.
  - **Orchestrator:** `services/orchestrator/src/orchestrator/servicer.py:54-58` catches `AioRpcError` to propagate `UNAVAILABLE/DEADLINE/RESOURCE_EXHAUSTED`, but the gateway call happens **inside** `team.run`, and AutoGen wraps model-client exceptions as `RuntimeError(str(...))`, so the original `AioRpcError` never reaches the handler → all gateway outages collapse to `INTERNAL`. **This is dead code.**
- **Fix:** Add `DependencyError` to AES and map `UNAVAILABLE`/`DEADLINE_EXCEEDED`/`RESOURCE_EXHAUSTED` → `DependencyError` → `UNAVAILABLE` at the client boundary. In the orchestrator, catch the AutoGen `RuntimeError` (or wrap the model client so gateway errors are detectable) and re-map to the right gRPC status, or update the docs to state the real behavior.
- **Verify:** Tests: job_svc-down → AES returns `UNAVAILABLE`; gateway-down → orchestrator returns `UNAVAILABLE` (not `INTERNAL`). (Softened in practice because job_svc retries regardless of code — but the contract should be correct.)

### Tool safety

### [ ] P1-10 — `query_database` allows arbitrary SQL functions; no statement timeout
- **Severity:** Medium (High under the shipped superuser config)
- **Location:** `services/mcp_svc/src/mcp_svc/handlers.py:267-281` (validator only inspects `exp.Table`) + `services/mcp_svc/src/mcp_svc/db.py:29` (no `command_timeout`) + `config.toml:11` (superuser `postgres`).
- **Problem:** Function calls are unconstrained. **Verified PASS:** `pg_read_file('/etc/passwd')`, `pg_sleep(30)`, `version()`, `current_user`, `current_setting('data_directory')`. Not a cross-tenant leak (isolation holds), but breaks "safe read-only": `pg_sleep` is a DoS; superuser `pg_read_file` discloses server files. A cartesian self-join also passes and can pin a connection.
- **Fix:** (a) Reject or allow-list function calls in the validator (walk `exp.Anonymous`/`exp.Func` nodes); (b) set `statement_timeout` (per-session `SET LOCAL statement_timeout` or engine `command_timeout`); (c) run mcp_svc's DB connection as a **least-privilege, read-only** role, not `postgres`.
- **Verify:** Add adversarial tests: `pg_sleep`, `pg_read_file`, `version()`, cartesian join → all rejected or bounded.

### [ ] P1-11 — `UpdateTool` can silently clear the `mutating` approval flag
- **Severity:** Medium (security-adjacent: drops human-approval requirement)
- **Location:** `services/agent_execution_service/src/agent_execution_service/services/tools.py:48-49` (full-replace) + proto `UpdateToolRequest.mutating`/`description` are non-`optional` (`services/agent_execution_service/proto/.../service.proto:48-53`).
- **Problem:** A client updating only the name (omitting `mutating`) sends `mutating=false` (proto3 default), silently flipping a `mutating=true` tool to `false` and removing its approval gate. Same issue clears `description`.
- **Fix:** Add a field mask (or make the fields `optional` in the proto and only update provided fields). Preserve existing values when not explicitly set.
- **Verify:** Test partial `UpdateTool` preserves `mutating`/`description`.

### [ ] P1-12 — `http_request` response size is unbounded for JSON bodies
- **Severity:** Medium (memory / context blow-up)
- **Location:** `services/mcp_svc/src/mcp_svc/handlers.py:192-206` — `max_response_chars` truncation applied only on the non-JSON text branch; `response.text` read in full unconditionally; JSON parsed and re-embedded whole.
- **Fix:** Cap the download size (stream with a byte limit) and truncate/limit JSON payloads too before returning.
- **Verify:** Test a large response is truncated/capped on both branches.

---

## 3. P2 — Robustness & quality (grouped by service)

> These are Medium/Low findings. Fix opportunistically or in a second pass. Each is a
> one-liner: `location — problem → fix`.

### job_svc
- [ ] **Retry budget charges infra flakiness** — `services/jobs.py:270-302` reap re-claim increments `attempts`; repeated pod crashes can dead-letter a good job. → Consider not counting reap-requeues against the *automatic* budget, or document the trade-off.
- [ ] **Tenant `UpdateJob` exposes worker semantics** — `servicer.py:114-120` → a tenant can `UpdateJob(running)` (self-claim, consumes an attempt) or `UpdateJob(failed)` (force escalation). → Restrict which target statuses are allowed on the tenant RPC.
- [ ] **Dead enum value** — `JOB_PHASE_PLANNING` (`mappers.py:37`) is never written (runner only writes `executing`/`completed`). → Either write it during planning or drop it.

### gateway
- [ ] **Blocklist trivially bypassable & mis-scoped as security** — `guardrails.py:62-68` skips non-user/tool roles (so a payload labeled `role="system"` bypasses); `:65-67` substring match defeated by double-space/newline/zero-width/split-across-messages. → Keep as a basic filter but stop presenting it as prompt-injection protection in docs; optionally normalize whitespace/unicode before matching.
- [ ] **PII redaction runs on all roles** — `guardrails.py:72-80` redacts every message, contradicting `ARCHITECTURE.md` §3.1 ("user-authored"); can corrupt a system/assistant example number. → Either scope to user/tool roles or fix the doc (README §Guardrails already says "every message"). Also `tool_calls[].arguments` are never redacted (`:76`).
- [ ] **No `Provider` abstraction despite the claim** — only concrete `OpenAIProvider` (`provider.py:61`); `servicer.py:18,68`, `main.py:24` depend on it by name. → Introduce a one-method `Provider` Protocol (`complete(...)`) and type it into `GatewayServicer.__init__` (also enables fakes).
- [ ] **Error map uses exact type, not isinstance** — `servicer.py:58` `_STATUS_BY_ERROR.get(type(exc), …)`; a future `AppError` subclass maps to `INTERNAL`. → Walk the MRO or use isinstance.
- [ ] **Malformed model tool_call → INTERNAL** — `provider.py:126,130` `tc.function.name` raises `AttributeError` if `function is None`. → Handle defensively, return a typed error.
- [ ] **Empty `parameters` → `{}` unconstrained schema** — `provider.py:45`; comment claims otherwise. → Validate/require a schema, or document.
- [ ] **Upstream provider error text echoed to caller** — `servicer.py:59` `context.abort(code, str(exc))` surfaces raw provider strings. → Return a sanitized message.

### orchestrator
- [ ] **mcp outage degrades to "success"** — `mcp_workbench.py:120-124` returns `[]` on list failure; agent proceeds tool-less and returns `finish_reason="stop"`; job_svc marks the step succeeded having done nothing. → Distinguish "tools unavailable" from "completed"; fail the step instead of silently succeeding.
- [ ] **Approval marker is a substring match on arbitrary tool output** — `mcp_workbench.py:192` fires if any tool result merely contains `"APPROVAL_REQUIRED"`. → Use a structured signal (a dedicated result field), not substring.
- [ ] **Max-message truncation indistinguishable from success** — `groupchat.py:133` + `run.py:87` pass the stop reason through as a normal finish. → Map truncation to a distinct non-success finish reason.
- [ ] **`role="system"` delivered as a chat message** — `run.py:71` maps every input to `TextMessage(source=m.role)`. → Route system inputs to an actual system message.
- [ ] **MCP httpx client leak** — `mcp_workbench.py:60-61` passes a `create_mcp_http_client(...)` the library won't close (it only closes clients it created). One client leaks per `list_tools`/`call_tool`. → Let the transport create the client, or close it explicitly.
- [ ] **DB engine never disposed on shutdown** — `main.py:44-45` closes only the gateway channel; module-level engine (`db.py:30`) left open. → Dispose on shutdown.
- [ ] **`create_stream` doesn't stream** — `model_client.py:182-199` yields only the final result. → Implement or clearly mark unsupported (harmless while streaming off).
- [ ] **NITs** — crude token heuristics + hardcoded `_ASSUMED_MAX_TOKENS=8192` (`model_client.py:41,210-214`); `_content_str` leaks Python `repr` for list content (`:90-93`); `InputMessage` should be a `typing.Protocol` (`run.py:39-43`).

### agent_execution_service (AES)
- [ ] **Orphan job on post-create failure** — `services/tasks.py:61-74`: job created first; if the `TaskRow` write fails or the response is lost, the job runs invisibly (or parks at `waiting_approval` forever, un-approvable). `JobGateway` has no cancel. → Add a cancel/compensation path, or an idempotency key so retry reconciles.
- [ ] **`≥1 tool` invariant bypassable** — `validators.py:51-56` checks only that the *input* list is non-empty; `services/agents.py:101-116` silently drops ids that aren't real tools in this tenant and commits anyway → agent with zero effective tools. → Validate resolved tool ids; error if none resolve.
- [ ] **No `llm_config` validation** — `servicer.py:91-92,120-121` pass `name`/`temperature` straight through; temperature unbounded (negative/huge/NaN), model name may be empty. → Add a validator (bounds + non-empty).
- [ ] **Unknown/UNSPECIFIED job status → `pending`** — `services/tasks.py:52-53` fallback. → Default unknown to an explicit unspecified/error status, not "not yet run."
- [ ] **No optimistic locking; lost updates** — `services/agents.py:79-91`, `tools.py:43-53` are read-modify-write with a decorative `version += 1` and no precondition. → Add a version precondition (conditional UPDATE) like jobs/tasks use, and carry version on the Update proto.
- [ ] **Tool delete relies solely on FK cascade; untested; SQLite FK off** — `services/tools.py:55-60`, `models.py:77-79`. Cascade silently un-grants the tool from every agent (can re-break the ≥1-tool invariant) and won't fire in the SQLite suite (`PRAGMA foreign_keys` off) → prod/test divergence. → Handle un-grant explicitly; enable FK in tests; add coverage.
- [ ] **No cross-tenant negative tests for agents/tools** — only tasks have them. → Add them (matches `ARCHITECTURE.md` §4.5's belt-and-suspenders claim).
- [ ] **Unbounded name/instructions lengths** — `validators.py:41-48` check only non-empty (unlike task input `:19-23`). → Add max lengths.
- [ ] **No pagination** — `Get*` with empty filter returns every tenant row (`services/tasks.py:76-81`, `agents.py:49-66`, `tools.py:33-38`). → Add limit/offset or cursor.
- [ ] **Approve/Retry discard the refreshed snapshot** — `servicer.py:189-201` returns only `success=True`; callers must re-`GetTask`. → Return the updated task (needs proto change) or document.
- [ ] **`start_job` misnamed** — `job_client.py:105-113` sends `UpdateJob(QUEUED)`, not RUNNING. → Rename (e.g. `requeue_job`).
- [ ] **Config bool coercion broken** — `config.py:62` `typ(override)` → `bool("false") is True` (latent; no bool config today). → Special-case bool parsing.

### mcp_svc
- [ ] **Call args not validated against advertised `inputSchema`** — `server.py:93` passes raw args to `handler.run`; `enum`/`required`/`additionalProperties:false` are advisory (e.g. `method:"CONNECT"` accepted). → Validate against the JSON Schema before dispatch.
- [ ] **Inconsistent error surfacing** — `http_request` raises (→ `isError=True`) while `query_database` returns soft `{"error":...}` (`server.py:94-96` vs `handlers.py:301-302`). → Pick one convention for bad-input.
- [ ] **Any tenant can self-bind privileged handlers by name** — `server.py:86` only checks the tenant owns *a row with that name*; AES `CreateTool` has no name restriction. → Add a notion of restricted/system handlers, or gate which catalog names may map to code handlers. (Cross-tenant data isolation still holds.)

### Cross-cutting (P2-XCUT)
- [ ] **Extract shared constants/plumbing** — the finish-reason/marker strings and the near-identical `errors.py`/`auth.py`/`config.py` are duplicated across all 5 services with no shared package. → Introduce a small internal `common` package (or shared proto dir + codegen) to remove the hand-maintained sync invariants and ~450 lines of duplication. Weigh against the deliberate "independently deployable" choice documented in `ARCHITECTURE.md` §2.
- [ ] **Proto contracts are hand-copied per consumer** — `job/v1` in AES+job_svc, `gateway/v1` in gateway+orchestrator, `orchestrator/v1` in job_svc+orchestrator. Drift risk. → Single source-of-truth proto dir with per-service codegen.
- [ ] **Observability** — stdlib logging only; no correlation/trace IDs across services. → Add a request/trace id propagated in gRPC metadata and logged in each service; consider OpenTelemetry spans around each hop (listed as a bonus in the assignment).

---

## 4. Testing gaps to close (do alongside the fixes above)

- [ ] **AES CRUD happy-path untested** — agent/tool tests only feed invalid inputs and abort at the validator; no `test_agent_service.py`/`test_tool_service.py`. Add tests for create/get/update/delete, `_link_tools`, name `ConflictError`, version bump, delete cascade, `agent_to_proto`/`tool_to_proto`.
- [ ] **Test fakes diverge from real service guards** — `agent_execution_service/tests/conftest.py:61-67` `FakeJobGateway` allows different transitions than job_svc (`start_job` too strict, `retry_job` too loose → hides P1-5). Align the fake with real `_ALLOWED_ENTRY`, or assert against the real transitions.
- [ ] **gateway servicer entirely untested** — no `test_servicer.py`; the `_handle_errors`/`_STATUS_BY_ERROR` map, `HasField` logic, proto conversion, and guardrail→provider ordering are unexercised. Also add a provider happy-path + tool-translation test.
- [ ] **orchestrator has no real AutoGen integration test** — `run_chat`/`build_group_chat` fully monkeypatch the session; `servicer.py`, `auth.py`, `agents_repo` cross-tenant filtering untested.
- [ ] **mcp_svc `server.py` untested** — catalog-ownership gate, "no execution binding" path, `_on_list_tools`, `auth.py`. SSRF tests miss the actual bypass encodings (P0-2); SQL tests miss function abuse (P1-10) and UNION; no negative cross-tenant assertion (tenant-b returns 0 of tenant-a's rows).
- [ ] **`SKIP LOCKED` / conditional-UPDATE concurrency untested on Postgres** — SQLite makes the locking clause a no-op, so the core horizontal-scaling correctness has no real test. Add a Postgres-backed concurrency test (or mark clearly as untested).
- [ ] **e2e is shallow + flaky** — `tests/test_agent_execution.py` is 2 trivial reads + 1 live-LLM happy-path with a loose regex assertion; **no** approval-pause→resume, resume-after-crash, or retry/dead-letter e2e despite the doc's framing. Add these scenarios and make the LLM assertion hermetic (mock the provider or assert on tool-call events, not model prose). Also: e2e verification currently polls the job_svc DB directly (`harness.py:210-243`) because `GetTask` reports stale `pending` for running tasks — fix the product gap so the assertion can go through the public gRPC API.
- [ ] **Harness robustness** — hardcoded ports (`harness.py:54-60`) and TCP-only readiness (`:332-353`) can bind to a stale process from a prior run. Add dynamic port allocation and a real readiness probe. (Teardown is already solid — process-group SIGTERM→SIGKILL.)

---

## 5. Deliverables (assignment-required, currently missing)

- [ ] **Add a top-level `README.md`** — the assignment explicitly requires a README with **setup instructions** and an **explanation of the approach**. Today there's only `ARCHITECTURE.md` + per-service READMEs and no root entry point. Include: one-command bring-up, ports/config table, how to run unit vs e2e tests, and a link to `ARCHITECTURE.md`.
- [ ] **Add the "AI tools used" disclosure** — the assignment explicitly requires mentioning which AI tools were used and how they contributed (this review itself was AI-assisted — worth stating). Put it in the README.
- [ ] **Fix clean-checkout bring-up** — `services/postgres/.env` sets `aep/aep/aep` and a single DB, but the services' `config.toml` and the test harness default to `postgres/postgres` and need **two** databases (`agent_execution_service`, `job_svc`). Reconcile the compose `.env` with the configs (and document env overrides), or add an init script that creates both DBs + the demo `customers`/`invoices` tables outside the test path (today they're created only by `tests/sql/sample_data.sql` via the harness).
- [ ] **(Optional, production polish)** — service Dockerfiles + a whole-system compose, and DB migrations (currently `Base.metadata.create_all` at startup, no Alembic).

---

## 6. Documentation reconciliation (`ARCHITECTURE.md`)

The design doc is a genuine asset but **overclaims** in places the code doesn't deliver.
After the fixes, reconcile each (fix the code or fix the doc):
- [ ] §3.1 "PII redaction applied to **user-authored** content" — code redacts **all** roles (P2/gateway).
- [ ] §3.1 provider swap = "new `Provider` implementation" — there is **no** Provider abstraction (P2/gateway).
- [ ] §5 "**Timeouts** configured per hop" — job_svc→orchestrator has none (P0-3) and orchestrator→mcp is dead config (P1-8).
- [ ] §3.4.1 "a step runs **at most once**… no double-execution window" — actually at-least-once (P1-3).
- [ ] §3.4 "`dead` — truly terminal, no further retries by anyone" — revivable (P1-4, P1-5).
- [ ] §4.6 "`DependencyError → UNAVAILABLE`" — AES has no `DependencyError`; outages map to `INTERNAL` (P1-9).
- [ ] Stale proto comment — `services/orchestrator/proto/aep/orchestrator/v1/service.proto:9-10` says "mcp_svc has no execution binding yet," but two handlers exist.

---

## 7. Suggested order of work

1. **P0-1, P0-2, P0-3** (secret, SSRF, hung-orchestrator) — security + a hard liveness bug.
2. **P1-4 + P1-5** together (terminal-state invariant), then **P1-1/P1-2/P1-3** (approval correctness), then **P1-6/P1-7** (recovery), then **P1-8/P1-9** (timeouts/errors), then **P1-10/P1-11/P1-12** (tool safety).
3. **Testing gaps (§4)** interleaved with the above — every fix ships with a test that would have caught it.
4. **Deliverables (§5)** — README + AI-tools disclosure + bring-up reconciliation.
5. **P2 (§3)** opportunistically.
6. **Doc reconciliation (§6)** last, once behavior is final.

_Each `file:line` reference was accurate at review time on branch `master`; line numbers may drift as you edit — search by the described symbol if a line doesn't match._
