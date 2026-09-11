"""OpenCode Zen provider (Responses API) with tool-calling support.

Talks OpenCode Zen's free public tier directly over httpx2, replicating this
working curl (a full single-round tool call round-trip):

    curl https://opencode.ai/zen/v1/responses \
      -H "Authorization: Bearer public" \
      -H "Content-Type: application/json" \
      -H "x-opencode-session: ses_test123" \
      -H "x-opencode-request: msg_test123" \
      -H "x-opencode-client: cli" \
      -H "User-Agent: opencode/local" \
      -d '{"model":"muse-spark-1.3-contributor-free",
           "input":[{"role":"user","content":"..."}],
           "tools":[{"type":"function","name":"...","parameters":{...}}],
           "tool_choice":"auto","stream":false}'

Unlike OpenAIProvider (chat-completions via the SDK), this speaks the Zen
**Responses API** so it can carry a tool-calling conversation as an `input`
array of message / `function_call` / `function_call_output` items. It exposes
the same `complete()` shape as OpenAIProvider, so the servicer drops onto it
unchanged.
"""

from __future__ import annotations

import json

import httpx2

from gateway.config import ModelSettings
from gateway.errors import ProviderError, ValidationError
from gateway.models import Completion, Message, ToolCall, ToolSpec, Usage

_DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"

# Zen serves reasoning models that default to "high" effort, which makes a
# single completion take ~90s (the hidden reasoning dominates). This is a
# smoke-test path where responsiveness matters more than deep reasoning, so
# cap the effort: "low" cuts a tool-calling call from ~90s to a few seconds
# while the model still emits correct tool calls.
_REASONING_EFFORT = "low"

# Copied verbatim from the working curl. On the free "public" tier these
# opencode client identifiers are the only auth-adjacent headers required.
_ZEN_HEADERS = {
    "Content-Type": "application/json",
    "x-opencode-session": "ses_test123",
    "x-opencode-request": "msg_test123",
    "x-opencode-client": "cli",
    "User-Agent": "opencode/local",
}


def _to_input(messages: list[Message]) -> list[dict]:
    """Render internal Messages as Responses API `input` items.

    The Responses API takes a typed list, not a chat-completions message array:
    a plain turn is a ``{"role", "content"}`` item, a tool call the model made
    is a ``{"type": "function_call", ...}`` item, and a tool result is a
    ``{"type": "function_call_output", ...}`` item keyed on the same
    ``call_id`` -- there is no ``role="tool"`` message on this wire.
    """
    items: list[dict] = []
    for m in messages:
        if m.role == "tool":
            # A tool result relays back keyed on the originating call id.
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.tool_call_id or "",
                    "output": m.content,
                }
            )
            continue
        if m.tool_calls:
            # Echo an assistant turn that made tool calls. Any accompanying
            # text (a "thought") becomes its own message item; the calls follow
            # as function_call items (the model may emit several in parallel).
            if m.content:
                items.append({"role": m.role, "content": m.content})
            items.extend(
                {
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                for tc in m.tool_calls
            )
            continue
        items.append({"role": m.role, "content": m.content})
    return items


def _to_tools(tools: list[ToolSpec]) -> list[dict]:
    """Render ToolSpecs as Responses API tool declarations.

    The Responses API uses a *flat* function shape (``type``/``name`` at the
    top level), not chat-completions' ``{"type":"function","function":{...}}``
    nesting. A malformed `parameters` schema raises rather than silently
    forwarding an empty (unconstrained) schema -- mirrors OpenAIProvider.
    """
    result: list[dict] = []
    for t in tools:
        try:
            parameters = json.loads(t.parameters) if t.parameters else {}
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"tool {t.name!r} has an invalid parameters schema: {exc}"
            ) from exc
        result.append(
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": parameters,
            }
        )
    return result


def _parse_output(body: dict) -> tuple[str, tuple[ToolCall, ...]]:
    """Split the Responses `output` array into assistant text and tool calls.

    ``reasoning`` items are ignored (Zen does not require them to be echoed
    back on the next turn); ``message`` items contribute their ``output_text``
    chunks; ``function_call`` items become ToolCalls keyed on ``call_id`` --
    the id the caller must echo back on the matching tool result.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in body.get("output", []) or []:
        item_type = item.get("type")
        if item_type == "message":
            for chunk in item.get("content", []) or []:
                piece = chunk.get("text")
                if isinstance(piece, str):
                    text_parts.append(piece)
        elif item_type == "function_call":
            tool_calls.append(
                ToolCall(
                    id=item.get("call_id") or item.get("id") or "",
                    name=item.get("name") or "",
                    arguments=item.get("arguments") or "{}",
                )
            )
    # Some responses also carry a flattened `output_text`; use it only as a
    # fallback when there were no explicit message items to read.
    if not text_parts:
        flat = body.get("output_text")
        if isinstance(flat, str):
            text_parts.append(flat)
    return "".join(text_parts), tuple(tool_calls)


class ZenProvider:
    def __init__(self, *, api_key: str, model: ModelSettings) -> None:
        self._settings = model
        self._client = httpx2.AsyncClient(
            base_url=model.base_url or _DEFAULT_BASE_URL,
            timeout=model.timeout_seconds,
            headers={**_ZEN_HEADERS, "Authorization": f"Bearer {api_key}"},
        )

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float | None,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Completion:
        payload: dict = {
            "model": self._settings.name,
            "input": _to_input(messages),
            "stream": False,
            # Keep calls fast on the free tier (see _REASONING_EFFORT).
            "reasoning": {"effort": _REASONING_EFFORT},
        }
        resolved_temp = (
            temperature if temperature is not None else self._settings.temperature
        )
        if resolved_temp is not None:
            payload["temperature"] = resolved_temp
        # NOTE: intentionally no `max_output_tokens`. Zen's models are
        # reasoning models whose hidden reasoning counts against the output
        # budget (a short answer can spend hundreds of reasoning tokens), so
        # capping at the config default would truncate a tool call or the final
        # answer mid-flight. An explicit `max_tokens` is only honoured when the
        # caller sets one on this call.
        if max_tokens is not None:
            payload["max_output_tokens"] = max_tokens
        # Only send tool kwargs when tools are present: a tool_choice with no
        # tools is rejected, and omitting them keeps the plain path unchanged.
        if tools:
            payload["tools"] = _to_tools(tools)
            if tool_choice:
                payload["tool_choice"] = tool_choice

        try:
            response = await self._client.post("/responses", json=payload)
        except httpx2.RequestError as exc:
            # Connectivity trouble -- retryable upstream, like OpenAIProvider.
            raise ProviderError(str(exc)) from exc

        if response.status_code >= 500:
            raise ProviderError(f"zen upstream {response.status_code}: {response.text}")
        if response.status_code >= 400:
            # The caller's request was rejected -- their fault, not an outage.
            raise ValidationError(
                f"zen rejected request {response.status_code}: {response.text}"
            )

        body = response.json()
        if body.get("error"):
            raise ProviderError(f"zen error: {body['error']}")

        content, tool_calls = _parse_output(body)
        # A tool-call turn legitimately carries no text, so only an empty reply
        # with no tool calls is a malformed/unexpected response.
        if not content and not tool_calls:
            raise ProviderError("zen returned no output")

        usage_raw = body.get("usage") or {}
        prompt = usage_raw.get("input_tokens", 0)
        completion = usage_raw.get("output_tokens", 0)
        usage = Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=usage_raw.get("total_tokens", prompt + completion),
        )
        return Completion(
            content=content,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=usage,
            tool_calls=tool_calls,
        )
