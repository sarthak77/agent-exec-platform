# Orchestrator

Runs one multi-agent, multi-tool "turn" of a tenant's task: builds an AutoGen
`SelectorGroupChat` over the tenant's configured agents (one `AssistantAgent`
per row in `agent_execution_service`'s `agents` table), gives each agent a
tool workbench scoped to its granted `mcp_svc` tools, routes every model call
through `gateway`, executes tool calls against `mcp_svc`, and returns the
resulting transcript — pausing the turn instead of finishing it if a tool
requires human approval.

This is the step-execution core of the platform. It does not decide *when*
to run a turn, does not own persistence of jobs/steps, and does not own agent
or tool configuration — those live elsewhere:

- **`agent_execution_service`** owns the `agents` / `tools` / `agent_tools`
  tables (instructions, model config, tool grants, which tools are
  `mutating`). `orchestrator` only reads them (`agents_repo.py`, `models.py`
  — a read-only mirror of that schema; orchestrator never writes to or runs
  DDL against these tables).
- **`job_svc`** is the caller. It drives a task through however many
  `Chat` turns it takes, decides when a job is `waiting_approval` vs.
  `running` vs. `completed` based on this service's `finish_reason`, and is
  the one that resends `approved=true` on the resumed call after a human
  approves. Orchestrator itself is stateless across turns — every fact
  needed for resumption is either in the request or reloaded from Postgres.
- **`gateway`** is the only path to an LLM. Orchestrator never calls a model
  directly; every `AssistantAgent` and the group chat's speaker-selector are
  backed by a `GatewayChatCompletionClient` that calls `gateway`'s
  `Chat` RPC.
- **`mcp_svc`** hosts the actual tool implementations (invoices, customers,
  email draft/send). Orchestrator calls it directly over MCP
  streamable-HTTP (not through gateway) to list and invoke tools.

## API surface

`proto/aep/orchestrator/v1/service.proto`:

```proto
service OrchestratorService {
  rpc Chat(ChatRequest) returns (ChatResponse);
}

message ChatRequest {
  repeated Message messages = 1;
  bool approved = 2;   // one-shot resume signal, see "Human approval" below
}

message ChatResponse {
  repeated Message messages = 1;   // transcript produced by the group chat this turn
  TokenUsage token_usage = 2;      // summed across every participant's gateway calls
  string finish_reason = 3;        // "stop" | "max messages reached" | ... | "requires_approval"
}
```

A vendored copy of gateway's proto (`proto/aep/gateway/v1/service.proto`) is
checked in so this service can generate a client stub for it; there is no
shared proto package yet, so it must be kept in sync with `gateway`'s copy by
hand. `ChatRequest.tools`/`tool_choice` in that proto is what makes gateway
function-calling-capable — see `model_client.py` below.

## Request lifecycle

For one `Chat` call (`servicer.py` → `run.py`):

1. **Tenant extraction** (`auth.py`) — the tenant id comes from the
   `x-tenant-id` gRPC metadata header. Missing header → `AuthenticationError`
   → `UNAUTHENTICATED`. RBAC/permission checking is assumed to have already
   happened upstream of this service; `auth.py` only extracts identity, it
   does not authorize.
2. **Build the group chat** (`groupchat.py:build_group_chat`):
   - Load the tenant's agents plus their granted tool names via
     `agents_repo.list_agents_for_tenant` (one query for `agents` +
     `agent_tools`, one follow-up query for the referenced `tools` rows).
     Empty result → `NoAgentsError` → `FAILED_PRECONDITION`.
   - For each agent: slugify its name into a unique AutoGen participant id
     (`_slugify`, collision-safe), create a `GatewayChatCompletionClient`
     carrying that agent's own `temperature`, create an `AgentToolWorkbench`
     scoped to that agent's `tool_names` and `mutating_tool_names`, and wrap
     them in an `AssistantAgent` whose system message appends an explicit
     "you may ONLY call these tools" instruction on top of the agent's
     configured instructions.
   - Add one more `GatewayChatCompletionClient` (temperature `0.0`) for the
     `SelectorGroupChat`'s own speaker-selection LLM call.
   - Assemble a `SelectorGroupChat` over all participants, terminating at
     `chat.max_messages` total messages (`MaxMessageTermination`), with
     `allow_repeated_speaker=True` so a single agent can take several turns
     in a row (e.g. call a tool, see the result, call another tool).
3. **Run the turn** (`run.py:run_chat`) — the caller's input messages become
   one `TextMessage` per message and are fed to `team.run(...,
   output_task_messages=False)`, which excludes the caller's own input from
   the returned message list — the transcript is defined as *only* what the
   group chat produced this turn.
4. **Tool execution loop** — this happens *inside* `team.run`, driven by
   AutoGen, not by orchestrator's own code: when a `GatewayChatCompletionClient.create`
   call returns tool calls, AutoGen invokes them against that agent's
   `AgentToolWorkbench.call_tool`, feeds the results back as
   `FunctionExecutionResultMessage`s, and calls the model again — repeating
   until the model stops requesting tools or `max_messages` is hit.
5. **Transcript + usage** — `_to_transcript` keeps only `BaseChatMessage`
   entries with string content, labels every one `role="assistant"` (every
   surviving message is agent-produced), and resolves the internal slug back
   to the agent's display name via `name_by_slug`. Token usage is summed
   across every `GatewayChatCompletionClient.total_usage()` created for the
   run, including the selector's.
6. **Finish reason** — normally the group chat's own `stop_reason` (e.g.
   `"max messages reached"`, or AutoGen's default `"stop"`). If any
   participant's workbench recorded a pending approval during the run
   (`GroupChatSession.approval_sink`), that overrides the stop reason to the
   constant `APPROVAL_FINISH_REASON = "requires_approval"` — job_svc's
   runner keys off this exact string.

## Human approval

The approval gate is enforced **locally in `AgentToolWorkbench.call_tool`**,
before any call reaches `mcp_svc` — not as an LLM instruction, not by
mcp_svc, and not by job_svc:

- Each tool grant carries a `mutating` flag (`tools.mutating` in
  `agent_execution_service`'s schema); `agents_repo.py` exposes the subset of
  an agent's granted tools that are mutating as `AgentSpec.mutating_tool_names`.
- `AgentToolWorkbench` is constructed per-agent, per-run with that agent's
  `mutating_tool_names` and the request's `approved` flag. If a requested
  tool name is in `mutating` and `approved` is `False`, the workbench refuses
  the call **without ever opening an mcp_svc session** — a `send_email`-style
  call has zero side effects until approved. The refusal is recorded as an
  `APPROVAL_REQUIRED` marker string, appended to that workbench's own
  `pending_approvals` list, and returned to the model as an ordinary (non-error)
  tool result so the model can tell the user the action is on hold.
- `mcp_svc` itself can also independently decline an action pending approval
  (its own `APPROVAL_REQUIRED` marker in a tool's *result* content, e.g. for
  `send_email` — see `mcp_svc/handlers.py`); the workbench detects that marker
  in the result text too and records it into the same sink. This means the
  gate is enforced twice — once cheaply and locally by `mutating` flag lookup
  (no network call), once authoritatively by the tool implementation itself —
  so a bug in either the catalog's `mutating` flag or the workbench's local
  set alone does not by itself allow an unapproved mutation through.
- After the run, `GroupChatSession.approval_sink` flattens every
  participant's `pending_approvals` (each workbench owns its own list; there
  is no single list shared across concurrent agents to write into, so no
  ordering assumption about interleaved tool calls is required). A non-empty
  sink flips `run_chat`'s `finish_reason` to `requires_approval` regardless of
  what the group chat's own stop condition says.
- **Resuming**: `approved` on `ChatRequest` is a one-shot, per-call signal,
  not a durable grant — it is threaded straight into every
  `AgentToolWorkbench` built for that one call (`build_group_chat(tenant_id,
  approved=...)`). job_svc is expected to replay the same step (the same
  pending tool call, from the same conversation state it reloads) with
  `approved=true` after a human approves; the *next* unrelated mutating call
  in a later turn still gates unless it too arrives with `approved=true`.
  Orchestrator holds no memory of "this particular call was approved" beyond
  the lifetime of that one `Chat` RPC — durability of the pending-approval
  state across the pause is job_svc's responsibility, not orchestrator's.

## Tool execution

Tool calling is fully wired end to end, not merely described in a system
prompt:

- `gateway`'s `ChatRequest`/`ChatResponse` carry OpenAI-style function
  calling (`tools`, `tool_choice`, `tool_calls`) — `model_client.py`'s
  `GatewayChatCompletionClient` bridges AutoGen's tool protocol to that wire
  format: outgoing `Tool`/`ToolSchema` objects become gateway `Tool`s (JSON
  Schema `parameters` serialized to a string); a gateway response with
  `tool_calls` becomes a `CreateResult` with `finish_reason="function_calls"`
  and content = a list of `FunctionCall`s, which AutoGen then executes.
- Execution goes through `AgentToolWorkbench` (`mcp_workbench.py`), built
  directly on the `mcp>=2,<3` streamable-HTTP client rather than
  `autogen_ext`'s `McpWorkbench` (that helper imports an mcp 1.x-only symbol
  incompatible with this service's pin). Each `list_tools`/`call_tool` opens
  a short-lived, tenant-scoped mcp_svc session (`x-tenant-id` header) — the
  workbench itself holds no persistent connection.
- Tool visibility is scoped twice: `list_tools` only returns schemas for
  names in the agent's `_allowed` set (so the model never even sees a tool
  it isn't granted), and `call_tool` independently re-checks `_allowed`
  before doing anything — a tool named outside the grant is refused locally
  with `is_error=True`, never reaching mcp_svc. The agent's system message
  (`groupchat.py:_agent_system_message`) also tells the model explicitly
  which tools it may call, as a second, non-authoritative layer (guardrail
  against "unexpected LLM behaviour" attempting an out-of-grant call — the
  authoritative enforcement is the workbench check, not the prompt).
- A tool listed in `mcp_svc`'s catalog but with no registered execution
  handler still comes back through as mcp_svc's own "no execution binding
  configured" response — orchestrator does not special-case this; it flows
  back to the model like any other tool result.
- Failures are best-effort at the listing level: if `mcp_svc` is unreachable,
  `list_tools` swallows the exception and returns an empty tool list (logged
  as a warning) rather than failing the whole turn — an agent with no
  reachable tools just proceeds tool-less for that call. `call_tool` failures
  are surfaced to the model as an `is_error=True` tool result (a normal,
  retryable-by-the-model outcome), not raised up to fail the RPC.

## Execution / context state

- **Tracked per run, in memory only:** the group chat's message history for
  that turn, each participant's per-call and cumulative token usage
  (`GatewayChatCompletionClient.actual_usage()` / `total_usage()`), and each
  workbench's `pending_approvals`.
- **Not persisted anywhere by this service:** there is no transcript store,
  no conversation/session table, no cross-turn memory. Every `Chat` call
  rebuilds the group chat from scratch from Postgres (agents/tools) plus
  whatever `messages` the caller supplies. Multi-turn continuity — "what did
  we already say in this task" — is entirely the caller's (job_svc's)
  responsibility: it must supply the running conversation as `messages` on
  each call and persist the transcript on its own side (execution history is
  a job_svc/agent_execution_service concern, not orchestrator's).
- This statelessness is what makes crash recovery simple on this service's
  side: a worker process crash mid-turn loses nothing that mattered, because
  orchestrator never held authoritative state — job_svc can simply retry the
  `Chat` call.

## Failure handling

| Failure | Behavior |
| --- | --- |
| Missing `x-tenant-id` | `UNAUTHENTICATED` (`auth.py` → `AuthenticationError`) |
| Tenant has no agents configured | `FAILED_PRECONDITION` (`NoAgentsError`) |
| `gateway` unreachable / times out / overloaded | The underlying `grpc.aio.AioRpcError`'s code is propagated verbatim if it's `UNAVAILABLE`, `DEADLINE_EXCEEDED`, or `RESOURCE_EXHAUSTED` (so callers can tell a transient dependency failure from a real bug), else collapsed to `INTERNAL` (`servicer.py:_handle_errors`) |
| `mcp_svc` unreachable during tool listing | Swallowed — empty tool list for that agent, chat proceeds |
| `mcp_svc` unreachable during a tool call | Surfaced to the model as an `is_error=True` tool result, chat proceeds |
| Any other unhandled exception | Logged with a stack trace, collapsed to `INTERNAL` — no internal detail leaks to the caller |

`servicer.py`'s `_handle_errors` decorator centralizes this mapping (mirrors
`agent_execution_service/servicer.py`'s convention) so `Chat` itself has no
try/except noise.
