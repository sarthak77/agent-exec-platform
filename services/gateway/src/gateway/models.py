"""Internal message/usage types, decoupled from the generated protobuf
classes — guardrails.py and provider.py operate on these, servicer.py
converts to/from aep.gateway.v1 proto messages at the edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A function call the model requested. `arguments` is a JSON string as
    the model produced it (not necessarily schema-valid)."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str
    # Set on assistant turns that requested tool calls.
    tool_calls: tuple[ToolCall, ...] = ()
    # Set on role="tool" result messages, linking back to the call's id.
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A function the model may call. `parameters` is a JSON Schema object
    encoded as a JSON string, forwarded to the provider verbatim."""

    name: str
    description: str
    parameters: str


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class Completion:
    content: str
    finish_reason: str
    usage: Usage
    tool_calls: tuple[ToolCall, ...] = field(default=())
