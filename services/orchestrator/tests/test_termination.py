"""Unit tests for the execution-turn early termination (_execution_termination).

The group-chat manager runs the caller's own input through the termination
condition before any agent speaks (see autogen _base_group_chat_manager), so the
text-answer termination must be scoped to the agent slugs: it has to stop the
turn on a domain agent's answer but never on the user's input.
"""

from __future__ import annotations

from autogen_agentchat.messages import TextMessage

from orchestrator.groupchat import _execution_termination


async def test_does_not_terminate_on_user_input() -> None:
    term = _execution_termination(["data_analyst", "email_assistant"])
    # The initial task is a source="user" TextMessage -- it must not end the turn
    # before any agent has run.
    assert await term([TextMessage(content="total overdue for Acme?", source="user")]) is None


async def test_does_not_terminate_on_unlisted_source() -> None:
    term = _execution_termination(["data_analyst"])
    assert await term([TextMessage(content="picking a speaker", source="selector")]) is None


async def test_terminates_on_agent_answer() -> None:
    term = _execution_termination(["data_analyst", "email_assistant"])
    stop = await term([TextMessage(content="75000", source="data_analyst")])
    assert stop is not None


async def test_terminates_on_any_listed_agent() -> None:
    term = _execution_termination(["data_analyst", "email_assistant"])
    stop = await term([TextMessage(content="sent", source="email_assistant")])
    assert stop is not None
