"""Single-model provider: forwards a screened request to the one configured
OpenAI chat model. No fallback, no retries, no caching — this is the basic
single-model path. Supports OpenAI-native tool/function calling: tool schemas
are forwarded and any tool calls the model returns are surfaced back.
"""

from __future__ import annotations

import json

from openai import AsyncOpenAI, OpenAIError

from gateway.config import ModelSettings
from gateway.errors import ProviderError
from gateway.models import Completion, Message, ToolCall, ToolSpec, Usage


def _to_openai_message(m: Message) -> dict:
    """Render an internal Message as an OpenAI chat message dict, including
    the tool-calling roles: an assistant turn may carry tool_calls, and a
    role="tool" turn carries the matching tool_call_id."""
    msg: dict = {"role": m.role, "content": m.content}
    if m.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        msg["tool_call_id"] = m.tool_call_id
    return msg


def _to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    result: list[dict] = []
    for t in tools:
        # `parameters` is a JSON Schema string; fall back to an empty schema so
        # a malformed/absent schema doesn't fail the whole request.
        try:
            parameters = json.loads(t.parameters) if t.parameters else {}
        except json.JSONDecodeError:
            parameters = {"type": "object", "properties": {}}
        result.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": parameters,
                },
            }
        )
    return result


class OpenAIProvider:
    def __init__(self, *, api_key: str, model: ModelSettings) -> None:
        # max_retries=0: the SDK retries twice by default, but this service's
        # contract is a single forward — retries are the caller's concern.
        # base_url points the OpenAI-compatible client at the configured
        # provider (unset => the OpenAI API; the Groq endpoint for provider
        # "groq"). None is passed through as the SDK's own default.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=model.base_url,
            timeout=model.timeout_seconds,
            max_retries=0,
        )
        self._settings = model

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float | None,
        tools: list[ToolSpec] | None = None,
        tool_choice: str | None = None,
    ) -> Completion:
        # Only pass tool-related kwargs when tools are present: OpenAI rejects
        # a tool_choice with no tools, and omitting them keeps the plain path
        # identical to before.
        extra: dict = {}
        if tools:
            extra["tools"] = _to_openai_tools(tools)
            if tool_choice:
                extra["tool_choice"] = tool_choice

        try:
            response = await self._client.chat.completions.create(
                model=self._settings.name,
                messages=[_to_openai_message(m) for m in messages],
                max_tokens=max_tokens if max_tokens is not None else self._settings.max_tokens,
                temperature=(
                    temperature if temperature is not None else self._settings.temperature
                ),
                **extra,
            )
        except OpenAIError as exc:
            raise ProviderError(str(exc)) from exc

        if not response.choices:
            raise ProviderError("model returned no choices")

        choice = response.choices[0]
        usage = Usage(
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
            total_tokens=response.usage.total_tokens if response.usage else 0,
        )
        tool_calls = tuple(
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments or "{}",
            )
            for tc in (choice.message.tool_calls or [])
            if tc.type == "function"
        )
        return Completion(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            tool_calls=tool_calls,
        )
