"""Runs one group-chat turn for a tenant's input messages and returns the
full transcript plus aggregated token usage."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from autogen_agentchat.messages import BaseChatMessage, TextMessage

from orchestrator.groupchat import build_group_chat

# finish_reason returned when a tool paused the turn on a human-approval gate
# (see mcp_workbench.py's sink). job_svc's runner keys off this exact string to
# park the job at `waiting_approval` rather than treating it as completion; must
# match job_svc/runner.py's APPROVAL_FINISH_REASON.
APPROVAL_FINISH_REASON = "requires_approval"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str
    agent: str = ""


@dataclass(frozen=True, slots=True)
class ChatResult:
    messages: list[ChatMessage]
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    total_tokens: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_tokens", self.prompt_tokens + self.completion_tokens)


class InputMessage:
    """Duck-typed: anything with `.role` and `.content` (e.g. a proto Message)."""

    role: str
    content: str


def _to_transcript(
    messages: Iterable[object], name_by_slug: dict[str, str]
) -> list[ChatMessage]:
    """Maps the group chat's output messages to the response transcript.

    Every surviving message is agent-produced (the caller's own input is
    excluded via `output_task_messages=False`), so `role` is always
    "assistant"; `source` (an internal participant slug) is resolved back to
    the agent's display name.
    """
    return [
        ChatMessage(
            role="assistant",
            content=m.content,
            agent=name_by_slug.get(m.source, m.source),
        )
        for m in messages
        if isinstance(m, BaseChatMessage) and isinstance(m.content, str)
    ]


async def run_chat(
    tenant_id: str, messages: Sequence[InputMessage], approved: bool = False
) -> ChatResult:
    # A decomposition ("planner") turn carries the planner instruction as a
    # system-role input message (see job_svc/runner.py); a sub-prompt execution
    # turn carries only a user message. The tool-less planner participant is
    # added only for the former (see build_group_chat).
    is_planning = any(getattr(m, "role", "") == "system" for m in messages)
    session = await build_group_chat(tenant_id, approved=approved, is_planning=is_planning)
    task = [TextMessage(content=m.content, source=m.role or "user") for m in messages]

    # output_task_messages=False keeps the caller's echoed input out of the
    # transcript, which is defined as the messages *produced by* the group
    # chat this turn (see service.proto); it also avoids mislabeling those
    # inputs as role="assistant".
    result = await session.team.run(task=task, output_task_messages=False)

    transcript = _to_transcript(result.messages, session.name_by_slug)
    prompt_tokens = sum(c.total_usage().prompt_tokens for c in session.clients)
    completion_tokens = sum(c.total_usage().completion_tokens for c in session.clients)

    # A pending-approval tool result during the run overrides the group chat's
    # own stop reason: the caller (job_svc) must see the pause, not "max
    # messages"/"stop", so it can park the job instead of marking it done.
    finish_reason = (
        APPROVAL_FINISH_REASON if session.approval_sink else (result.stop_reason or "stop")
    )

    return ChatResult(
        messages=transcript,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        finish_reason=finish_reason,
    )
