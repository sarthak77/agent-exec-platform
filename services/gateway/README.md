# Gateway

The single choke point for every LLM call in the AI Agent Execution
Platform. It is a small stateless gRPC service
(`aep.gateway.v1.GatewayService/Chat`) that does exactly two things per
call: run in-process input guardrails, then forward the (now sanitized)
request to one configured chat-completions model. Nothing upstream of it
is allowed to call a model provider directly.

Today `orchestrator` is the gateway's only caller — every model turn in a
group chat it runs (one per agent participant, per round) goes through
this RPC. `agent_execution_service`, `job_svc`, and `mcp_svc` never talk
to the gateway; they don't call an LLM at all.

This is a deliberately thin v0: one model, no routing, no fallback, no
caching, no per-tenant accounting, no streaming. The sections below
describe what's actually implemented and call out explicitly where a
production version would need more.

## Where it sits in the platform

```
agent_execution_service --(job)--> job_svc --(runs job)--> orchestrator --(Chat)--> gateway --(chat.completions)--> OpenAI / Groq
                                                               ^
                                                     mcp_svc (tool catalog, described but not invoked here)
```

`orchestrator` builds a round-robin multi-agent group chat and, for every
turn any participant takes, calls this service's `Chat` RPC once. The
gateway has no idea it's participating in a multi-agent conversation —
each call is a stateless, independent chat-completion request.

## API surface

Defined in `proto/aep/gateway/v1/service.proto`:

```proto
service GatewayService {
  rpc Chat(ChatRequest) returns (ChatResponse);
}
```

- `ChatRequest`: `model_config` (optional per-call `temperature`/`max_tokens`
  override), `messages` (`role` + `content` + tool-calling fields),
  `tools` (function schemas the model may call — a JSON Schema string per
  tool, forwarded opaquely), `tool_choice` (`"auto"` / `"none"` /
  `"required"`, unset = provider default).
- `ChatResponse`: the assistant `message`, `token_usage`
  (`prompt_tokens`/`completion_tokens`/`total_tokens`), `finish_reason`
  (`"tool_calls"` when the model wants to call a tool), and a
  `tool_calls` list mirroring `message.tool_calls` for convenience.

The shape mirrors a typical model-provider gateway contract
(`model_config` + `messages` + `token_usage`), plus a minimal
single-round tool-calling contract: the caller supplies tool schemas,
the model may answer with `tool_calls`, and it's the **caller's**
responsibility to execute them (e.g. against `mcp_svc`) and feed the
results back as `role="tool"` messages on the next `Chat` call. The
gateway itself never executes a tool — it only passes schemas through to
the model and structured calls back to the caller.

## Request lifecycle

1. `servicer.py::Chat` converts each proto `Message` to the internal
   `gateway.models.Message` dataclass (`_message_from_proto`).
2. `guardrails.screen_and_sanitize()` runs (see below); on rejection it
   raises `GuardrailRejected` before anything reaches the model.
3. Proto `Tool`/`tool_choice` fields are converted to `ToolSpec`s.
4. `OpenAIProvider.complete()` (`provider.py`) calls the configured
   model's chat-completions endpoint with the sanitized messages,
   resolved `max_tokens`/`temperature` (per-call override, else the
   configured default), and any tool schemas.
5. The provider's response is mapped to an internal `Completion`
   (content, `finish_reason`, `Usage`, `tool_calls`), then back to a
   proto `ChatResponse`.

Everything is in-process and synchronous within the call — there is no
queue or async handoff inside the gateway itself; the caller's gRPC call
blocks for the duration of one model round-trip.

## Guardrails (`guardrails.py`) — input only

Applied in this order, to the full message list:

1. **Role check** — every message's `role` must be one of `system`,
   `user`, `assistant`, `developer`, `tool`. An unknown role is rejected
   immediately (`GuardrailRejected`) rather than passed through to fail
   deep inside the provider call.
2. **Empty-input check** — all message contents joined and stripped;
   empty ⇒ rejected.
3. **Size check** — the same joined string over `guardrails.max_input_chars`
   (8000 in `config.toml`) ⇒ rejected.
4. **Blocklist check** — each `guardrails.blocklist` term (case-insensitive
   substring match) is checked only against `user` and `tool` message
   content — the two roles that carry caller- or externally-supplied
   content. This is intentional: the model's own prior `assistant`/`system`
   turns can't trip the guardrail just by discussing a blocked phrase
   (e.g. refusing a jailbreak attempt), and a `tool` message — the result
   of an `mcp_svc` call, e.g. an HTTP body or DB row — gets the *same*
   screening as direct user input, since that's exactly where a
   prompt-injection payload could be smuggled in. The default blocklist
   (`config.toml`) covers a handful of literal prompt-injection phrases
   ("ignore previous instructions", "reveal your system prompt",
   "jailbreak", ...) — a substring match, not a classifier.
5. **PII redaction** (not rejection) — after the checks above pass, every
   message's content is regex-scrubbed for email addresses, SSNs
   (`\d{3}-\d{2}-\d{4}`), card-like 16-digit sequences, and phone-like
   10-digit sequences, replaced with `[EMAIL]`/`[SSN]`/`[CARD]`/`[PHONE]`
   tags before the request ever reaches the model. Tool-calling fields
   (`tool_calls`, `tool_call_id`) pass through unredacted — only free-text
   `content` is touched. This is deliberately broad (favors over-redacting
   digit runs that merely *look* like an SSN/phone over letting a real one
   through) and deliberately not a full DLP pass — no international
   formats, no unformatted SSNs, etc.

Any guardrail failure raises `GuardrailRejected`, mapped to gRPC
`INVALID_ARGUMENT` — the caller gets a clean, immediate rejection rather
than a request that fails deep inside a provider call.

## Provider integration (`provider.py`)

- A single `OpenAIProvider`, built once at startup from `config.toml`'s
  `[model]` section, backs every request — there is no per-request or
  per-tenant model selection.
- Talks to any OpenAI-wire-compatible chat-completions endpoint. Two
  providers are supported today: `"openai"` (default base URL, key from
  `OPENAI_API_KEY`) and `"groq"` (`https://api.groq.com/openai/v1`, key
  from `GROQ_API_KEY`) — `config.toml` in this repo is currently set to
  Groq's `openai/gpt-oss-20b`. An explicit `model.base_url` in the config
  overrides the provider default (e.g. to point at a proxy). The API key
  is **never** read from `config.toml` — only from the provider-specific
  env var — so it can't end up committed or logged.
- `AsyncOpenAI` is constructed with `max_retries=0` and
  `timeout=model.timeout_seconds` (30s by default): the SDK would
  otherwise silently retry twice on its own, which this service
  deliberately opts out of — see "Failure handling" below.
- Tool schemas: `parameters` is a JSON Schema object carried as an opaque
  string end-to-end (proto → `ToolSpec` → provider). A malformed schema
  raises `ValidationError` rather than silently falling back to an empty
  (unconstrained) schema — a broken tool registration should surface as
  an error, not let the model call the tool with arbitrary arguments.
  Tool kwargs are only sent to the provider when `tools` is non-empty
  (OpenAI rejects a `tool_choice` with no `tools`).
- Token usage is read straight from the provider's response
  (`response.usage`) and returned in `ChatResponse.token_usage` on every
  call — no aggregation, budgeting, or persistence happens on the
  gateway side; that's left entirely to the caller (today, nothing
  actually persists it — see "Cost and latency" below).
- **Sample test provider (`provider_zen.py`)**: a `ZenProvider` that talks
  to OpenCode Zen's free public tier for smoke-testing without an
  OpenAI/Groq key. Unlike `OpenAIProvider`, it hits the **Responses API**
  (`POST /zen/v1/responses`) directly over `httpx2`, sending the
  `Bearer public` key (defaulted — no env var needed) and the
  `x-opencode-*` / `User-Agent` headers verbatim. Enable it by setting
  `[model]` in `config.toml` to `provider = "opencode"`, `name =
  "muse-spark-1.3-contributor-free"`. **Tool calling is fully supported on
  this path**: the message list is rendered as the Responses API `input`
  array (a `role="tool"` turn becomes a `function_call_output` item, an
  assistant turn's `tool_calls` become `function_call` items, both keyed on
  the same `call_id`), tool schemas use the Responses API's flat `function`
  shape, and any `function_call` items in the response's `output` array are
  surfaced back as `ChatResponse.tool_calls` — so the same tool-calling
  round trip as `OpenAIProvider`. `max_output_tokens` is intentionally not
  forwarded: Zen's reasoning models spend the output budget on hidden
  reasoning, so a small cap would truncate a tool call or the final answer.

## Failure handling

- `BadRequestError` from the provider (malformed messages, invalid model
  params, a bad tool schema the provider itself rejects) → `ValidationError`
  → gRPC `INVALID_ARGUMENT`. This is treated as **the caller's fault**,
  not an outage, and is deliberately not retried or mapped to
  `UNAVAILABLE`.
- Any other `OpenAIError` (connectivity failure, rate limiting, a
  provider-side 5xx) → `ProviderError` → gRPC `UNAVAILABLE`. This is
  explicitly the *retryable* bucket.
- Empty `choices` in a response (a malformed/unexpected provider
  response) → `ProviderError` as well.
- Any unhandled exception is logged (`logger.exception`) and mapped to
  `INTERNAL`, so a bug here never leaks a stack trace to the client.
- **The gateway itself does not retry.** `max_retries=0` on the SDK
  client is a deliberate choice, documented in `provider.py`: "this
  service's contract is a single forward — retries are the caller's
  concern." The distinction between `ValidationError` (don't retry) and
  `ProviderError` (safe to retry) exists precisely so the caller
  (`orchestrator`, or whatever calls the gateway) can implement retry/backoff
  policy using the gRPC status code, without the gateway making that
  decision — and without double-retrying inside both layers.

## Cost and latency of LLM calls

What's here today: `token_usage` is reported on every response, `model`
and `max_tokens`/`temperature` are fixed platform-side config (bounding
worst-case cost per call), and `timeout_seconds` bounds worst-case
latency per call.

What a production version would need, and isn't implemented:

- **No caching** — identical prompts are recomputed every time.
- **No per-tenant or per-agent budget/rate limiting** — nothing stops
  one tenant from exhausting the shared model quota; there's no tenant
  identity in this service at all (see below).
- **No usage persistence or accounting** — `token_usage` is returned to
  the immediate caller and then lost; there is no ledger anywhere
  mapping tokens spent back to a task, job, or tenant.
- **No model routing or fallback** — one model, one provider, hardcoded
  in config. A cheaper/faster model for simple turns, or a fallback
  model on provider outage, would need a routing layer in front of
  `OpenAIProvider`.
- **No response streaming** — `Chat` is a unary RPC; a caller waits for
  the full completion, which matters for latency on long generations.
