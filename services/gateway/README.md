# gateway

Basic LLM gateway: runs in-process input guardrails, then forwards the
request to a single configured OpenAI model over gRPC (`aep.gateway.v1.GatewayService/Chat`).
No routing, fallback, caching, or accounting — see `docs/04-gateway.md` at
the platform root for the fuller design this is a stripped-down v0 of.

## Guardrails (input only)

- reject empty input
- reject input over `guardrails.max_input_chars`
- reject input containing a `guardrails.blocklist` term (covers a few
  common prompt-injection phrases too)
- redact obvious PII (email, SSN, credit card, phone) before it reaches
  the model

## Run

```sh
export OPENAI_API_KEY=sk-...
uv sync
uv run gateway
```

Config (host/port, model name, guardrail limits) lives in `config.toml`;
override its path with `GATEWAY_CONFIG_FILE`. The API key is only ever
read from `OPENAI_API_KEY` — never from config.toml.

## Proto

`proto/aep/gateway/v1/service.proto` defines `GatewayService.Chat`. After
editing it, regenerate stubs with `./scripts/gen_proto.sh`. Consumers
(e.g. `orchestrator`) vendor their own copy of this proto and generate
their own client stubs — there's no shared package yet.

## Try it

```sh
grpcurl -plaintext -d '{"messages": [{"role": "user", "content": "say hi in 3 words"}]}' \
  localhost:50054 aep.gateway.v1.GatewayService/Chat
```
