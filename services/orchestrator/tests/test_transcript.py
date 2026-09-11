"""Unit tests for run._to_transcript — the mapping from AutoGen output
messages to the response transcript. Guards the fix that keeps input
messages out and never mislabels a role.
"""

from __future__ import annotations

from autogen_agentchat.messages import TextMessage

from orchestrator.run import ChatMessage, _to_transcript


def test_marks_assistant_and_resolves_slug_to_display_name() -> None:
    msgs = [TextMessage(content="hello", source="planner")]
    out = _to_transcript(msgs, {"planner": "Planner Agent"})
    assert out == [ChatMessage(role="assistant", content="hello", agent="Planner Agent")]


def test_unknown_source_falls_back_to_the_raw_slug() -> None:
    out = _to_transcript([TextMessage(content="x", source="mystery")], {})
    assert out[0].agent == "mystery"


def test_non_chat_messages_are_skipped() -> None:
    msgs = [object(), TextMessage(content="keep", source="a")]
    out = _to_transcript(msgs, {"a": "A"})
    assert [m.content for m in out] == ["keep"]
