"""Unit tests for the human-approval signalling path:

  * AgentToolWorkbench records a tool result carrying the APPROVAL_REQUIRED
    marker into its shared approval sink (see mcp_workbench.py), and
  * run_chat turns a non-empty sink into the APPROVAL_FINISH_REASON, overriding
    the group chat's own stop reason (see run.py).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from mcp.types import TextContent

from orchestrator import mcp_workbench, run
from orchestrator.mcp_workbench import APPROVAL_REQUIRED_MARKER, AgentToolWorkbench


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [TextContent(type="text", text=text)]
        self.is_error = is_error


class _FakeSession:
    def __init__(self, text: str) -> None:
        self._text = text

    async def call_tool(self, name, arguments):  # noqa: ANN001
        return _FakeCallResult(self._text)


def _patch_session(monkeypatch, text: str) -> None:
    @asynccontextmanager
    async def fake_session(tenant_id):  # noqa: ANN001
        yield _FakeSession(text)

    monkeypatch.setattr(mcp_workbench, "_mcp_session", fake_session)


async def test_workbench_records_pending_approval_in_sink(monkeypatch) -> None:
    _patch_session(monkeypatch, f'{{"status": "{APPROVAL_REQUIRED_MARKER}"}}')
    sink: list[str] = []
    wb = AgentToolWorkbench("t1", ["send_email"], sink)

    result = await wb.call_tool("send_email", {"to": "a@b"})

    assert sink and APPROVAL_REQUIRED_MARKER in sink[0]
    # The content still flows back to the model so it can report the hold.
    assert APPROVAL_REQUIRED_MARKER in result.result[0].content


async def test_workbench_leaves_sink_empty_for_ordinary_result(monkeypatch) -> None:
    _patch_session(monkeypatch, '{"invoices": []}')
    sink: list[str] = []
    wb = AgentToolWorkbench("t1", ["retrieve_invoices"], sink)

    await wb.call_tool("retrieve_invoices", {})

    assert sink == []


# --- run_chat finish_reason ------------------------------------------------


class _Usage:
    prompt_tokens = 1
    completion_tokens = 2

    def total_usage(self):  # noqa: ANN201
        return self


class _Result:
    def __init__(self, stop_reason: str) -> None:
        self.messages = []
        self.stop_reason = stop_reason


class _Team:
    def __init__(self, stop_reason: str) -> None:
        self._stop_reason = stop_reason

    async def run(self, task, output_task_messages=False):  # noqa: ANN001
        return _Result(self._stop_reason)


class _FakeSessionObj:
    def __init__(self, approval_sink: list[str]) -> None:
        self.team = _Team("max messages reached")
        self.name_by_slug = {}
        self.clients = [_Usage()]
        self.approval_sink = approval_sink


def _patch_build(monkeypatch, sink: list[str]) -> None:
    async def fake_build(tenant_id):  # noqa: ANN001
        return _FakeSessionObj(sink)

    monkeypatch.setattr(run, "build_group_chat", fake_build)


async def test_run_chat_reports_requires_approval_when_sink_nonempty(monkeypatch) -> None:
    _patch_build(monkeypatch, ["APPROVAL_REQUIRED ..."])
    result = await run.run_chat("t1", [])
    assert result.finish_reason == run.APPROVAL_FINISH_REASON


async def test_run_chat_uses_stop_reason_when_no_approval(monkeypatch) -> None:
    _patch_build(monkeypatch, [])
    result = await run.run_chat("t1", [])
    assert result.finish_reason == "max messages reached"
