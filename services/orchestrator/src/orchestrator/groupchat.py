"""Builds the AutoGen group chat for a tenant: one AssistantAgent per row
in `agents` (see agents_repo.py), each attached to its own MCP tool
workbench scoped to that agent's granted tools (see mcp_workbench.py). The
participants are run under a SelectorGroupChat, whose LLM selector picks the
next speaker each turn.
"""

from __future__ import annotations

import re

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.teams import SelectorGroupChat

from orchestrator.agents_repo import AgentSpec, list_agents_for_tenant
from orchestrator.config import settings
from orchestrator.db import Sessions
from orchestrator.errors import NoAgentsError
from orchestrator.gateway_client import get_gateway_stub
from orchestrator.mcp_workbench import AgentToolWorkbench
from orchestrator.model_client import GatewayChatCompletionClient

_SELECTOR_PROMPT = """You are coordinating a team of agents. The available roles are:
{roles}

Read the conversation so far:
{history}

Select the single next role from {participants} to respond. Return only that role's name.
"""


def _slugify(name: str, taken: set[str]) -> str:
    slug = re.sub(r"\W", "_", name).strip("_") or "agent"
    if slug[0].isdigit():
        slug = f"a_{slug}"
    base, i = slug, 2
    while slug in taken:
        slug = f"{base}_{i}"
        i += 1
    taken.add(slug)
    return slug


def _agent_system_message(spec: AgentSpec) -> str:
    if not spec.tool_names:
        return spec.instructions
    tools = ", ".join(spec.tool_names)
    return (
        f"{spec.instructions}\n\n"
        f"You are attached to the MCP tool server. You may ONLY make the following "
        f"tool calls and none other: {tools}. You must not call any other tool "
        f"under any circumstances."
    )


class GroupChatSession:
    """One built group chat for a single run. `clients` lists every
    GatewayChatCompletionClient created for the run (one per participant plus
    the selector's own client) so a caller can sum `.total_usage()` across the
    whole run. The gateway channel the clients share is process-wide (see
    gateway_client.py), so a session owns nothing that needs closing.
    """

    def __init__(
        self,
        team: SelectorGroupChat,
        name_by_slug: dict[str, str],
        clients: list[GatewayChatCompletionClient],
        workbenches: list[AgentToolWorkbench],
    ) -> None:
        self.team = team
        self.name_by_slug = name_by_slug
        self.clients = clients
        self._workbenches = workbenches

    @property
    def approval_sink(self) -> list[str]:
        """Non-empty after the run iff some tool paused on a human-approval
        gate (see mcp_workbench.py). Pulled from each agent's own workbench
        after the run rather than written into a list shared across agents
        during it, so no ordering assumption about concurrent tool calls is
        baked into how the signal gets back to run.py."""
        return [item for wb in self._workbenches for item in wb.pending_approvals]


async def build_group_chat(tenant_id: str) -> GroupChatSession:
    async with Sessions() as session:
        agent_specs = await list_agents_for_tenant(session, tenant_id)
    if not agent_specs:
        raise NoAgentsError(f"tenant {tenant_id!r} has no agents configured")

    stub = get_gateway_stub()
    taken: set[str] = set()
    name_by_slug: dict[str, str] = {}
    clients: list[GatewayChatCompletionClient] = []
    workbenches: list[AgentToolWorkbench] = []
    participants = []

    for spec in agent_specs:
        slug = _slugify(spec.name, taken)
        name_by_slug[slug] = spec.name
        client = GatewayChatCompletionClient(temperature=spec.llm_config_temperature, stub=stub)
        clients.append(client)
        workbench = AgentToolWorkbench(tenant_id, spec.tool_names)
        workbenches.append(workbench)
        participants.append(
            AssistantAgent(
                slug,
                model_client=client,
                workbench=workbench,
                system_message=_agent_system_message(spec),
                description=f"Domain agent {spec.name!r} (agent id {spec.id}).",
            )
        )

    # SelectorGroupChat runs an LLM to pick the next speaker each turn; give it
    # its own deterministic client and account for its usage alongside the
    # participants'.
    selector_client = GatewayChatCompletionClient(temperature=0.0, stub=stub)
    clients.append(selector_client)

    # MaxMessageTermination bounds *total* messages in the thread. Input
    # messages are excluded from the response transcript (see run.py) but
    # still counted here, so size max_messages with the participant count and
    # expected input in mind.
    team = SelectorGroupChat(
        participants,
        model_client=selector_client,
        termination_condition=MaxMessageTermination(settings.chat.max_messages),
        selector_prompt=_SELECTOR_PROMPT,
        allow_repeated_speaker=True,
    )
    return GroupChatSession(team, name_by_slug, clients, workbenches)
