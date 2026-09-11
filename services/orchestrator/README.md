# orchestrator

Basic AutoGen orchestrator: for each `Chat` request it builds a
round-robin group chat over all of the caller tenant's configured agents
(read from `agent_execution_service`'s `agents` table) plus one
`mcp_connector` participant that carries `mcp_svc`'s live tool catalog,
routing every inference call through `gateway`
(`aep.gateway.v1.GatewayService/Chat`). No streaming, no tool execution,
no persistence of transcripts — see `docs/03-orchestrator.md` at the
platform root for the fuller design this is a stripped-down v0 of.

## How a turn works

1. Extract the tenant id from the `x-tenant-id` metadata header
   (RBAC is assumed to have happened upstream; see `auth.py`).
2. Load the tenant's agents + their granted tool names (`agents_repo.py`);
   `FAILED_PRECONDITION` if the tenant has none.
3. Best-effort fetch of the MCP tool catalog for context — if `mcp_svc` is
   unreachable the chat still runs, just without a tool listing
   (`mcp_connector.py`).
4. Build one `AssistantAgent` per agent (each with its own temperature)
   plus the `mcp_connector`, all backed by a shared gRPC channel to
   `gateway` (`gateway_client.py`, `groupchat.py`).
5. Run the group chat until `chat.max_messages` total messages, then
   return the transcript **produced by** the chat (the caller's own input
   is not echoed back) plus token usage summed across every participant
   (`run.py`).

The tool catalog is surfaced as plain text in the connector's system
prompt: `gateway` is a single-model, no-tools passthrough and `mcp_svc`
has no execution binding yet, so tools are described, never called.

## Run

```sh
uv sync
uv run orchestrator
```

Needs Postgres reachable with the `agents`/`agent_tools`/`tools` tables
that `agent_execution_service` owns and creates, plus `gateway` up on its
configured port for `Chat` to succeed. Config (gRPC host/port, Postgres,
gateway, MCP url, `chat.max_messages`) lives in `config.toml`; override its
path with `ORCHESTRATOR_CONFIG_FILE`.

`chat.max_messages` bounds *total* messages in a turn (input + agent
turns), so size it with the participant count and expected input in mind.

## Proto

`proto/aep/orchestrator/v1/service.proto` defines
`OrchestratorService.Chat`. A vendored copy of `gateway`'s proto lives at
`proto/aep/gateway/v1/service.proto` so this service can generate a client
stub for it — keep it in sync with `gateway`'s copy by hand. After editing
either, regenerate stubs with `./scripts/gen_proto.sh`.

## Test

```sh
uv run --group dev pytest
```

Unit tests cover the pure helpers (`_slugify`, transcript mapping) and the
gateway model client's usage accounting / `finish_reason` mapping against a
fake stub — no Postgres, gateway, or MCP server required.

## Try it

```sh
grpcurl -plaintext -H 'x-tenant-id: t1' \
  -d '{"messages": [{"role": "user", "content": "introduce yourselves"}]}' \
  localhost:50053 aep.orchestrator.v1.OrchestratorService/Chat
```
