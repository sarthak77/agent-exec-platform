"""Builds the AutoGen group chat for a tenant: one AssistantAgent per row
in `agents` (see agents_repo.py), each attached to its own MCP tool
workbench scoped to that agent's granted tools (see mcp_workbench.py). The
participants are run under a SelectorGroupChat, whose LLM selector picks the
next speaker each turn.
"""

from __future__ import annotations

import re

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, SourceMatchTermination
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

# A tool-less planner participant, added to every group chat. The job_svc runner
# first asks the orchestrator to decompose a job's prompt into an ordered list of
# sub-prompts (see job_svc/runner.py). That turn must be answered by an agent
# that *cannot* call tools: a tool-enabled domain agent tends to just execute the
# request, so the runner gets back a tool result where it expected a JSON array
# of sub-prompts. This agent only ever plans. See _make_plan_selector for how it
# is picked and build_group_chat for the matching termination.
_PLANNER_NAME = "Planner"
_PLANNER_SYSTEM_MESSAGE = (
    "You are the team's task planner. Break the user's request into the FEWEST "
    "self-contained sub-prompts needed, each one carried out end to end by a "
    "single tool-using agent (an agent may make several tool calls within one "
    "sub-prompt). Prefer a SINGLE sub-prompt whenever the whole request can be "
    "answered in one agent turn; do not split a single lookup-and-compute into "
    "separate steps. Return ONLY a JSON array of strings (one sub-prompt per "
    "element), with no surrounding text. You have no tools: never execute the "
    "task or call a tool, only produce the plan."
)
_PLANNER_DESCRIPTION = (
    "Task planner: decomposes the initial request into an ordered plan (a JSON "
    "array of sub-prompts). Has no tools and never executes a step -- pick it only "
    "to produce the plan, never to carry out a concrete sub-task."
)


def _make_plan_selector(planner_slug: str):
    """Speaker-selection override for the SelectorGroupChat.

    A decomposition ("planner") call arrives with the planner instruction as a
    system-role input message; a sub-prompt execution call carries only a user
    message. So hand the first turn of a planner call to the tool-less planner --
    deterministically, so a tool-using agent can't pre-empt it -- and defer every
    other selection to the LLM selector by returning None.
    """

    def _select(thread) -> str | None:
        is_planning = any(getattr(m, "source", None) == "system" for m in thread)
        if not is_planning:
            return None
        planner_spoke = any(getattr(m, "source", None) == planner_slug for m in thread)
        return None if planner_spoke else planner_slug

    return _select


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


async def build_group_chat(
    tenant_id: str, approved: bool = False, is_planning: bool = False
) -> GroupChatSession:
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
        workbench = AgentToolWorkbench(
            tenant_id, spec.tool_names, spec.mutating_tool_names, approved=approved
        )
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

    # The tool-less planner is a participant ONLY on a decomposition ("planner")
    # call. Kept out of execution calls entirely so the LLM selector can't pick
    # it to answer a concrete sub-prompt -- where, having no tools, it would just
    # re-plan instead of using a domain agent's tools. See is_planning above and
    # job_svc/runner.py's two phases (decompose, then execute each sub-prompt).
    selector_func = None
    termination = MaxMessageTermination(settings.chat.max_messages)
    if is_planning:
        planner_slug = _slugify(_PLANNER_NAME, taken)
        name_by_slug[planner_slug] = _PLANNER_NAME
        planner_client = GatewayChatCompletionClient(temperature=0.0, stub=stub)
        clients.append(planner_client)
        participants.append(
            AssistantAgent(
                planner_slug,
                model_client=planner_client,
                system_message=_PLANNER_SYSTEM_MESSAGE,
                description=_PLANNER_DESCRIPTION,
            )
        )
        selector_func = _make_plan_selector(planner_slug)
        # End the planner call the instant the planner speaks, so the run's final
        # message is the plan itself and no later speaker overwrites it (the
        # runner reads only that last non-empty message -- orchestrator_client.py).
        termination = termination | SourceMatchTermination([planner_slug])

    # SelectorGroupChat runs an LLM to pick the next speaker each turn; give it
    # its own deterministic client and account for its usage alongside the
    # participants'.
    selector_client = GatewayChatCompletionClient(temperature=0.0, stub=stub)
    clients.append(selector_client)

    team = SelectorGroupChat(
        participants,
        model_client=selector_client,
        termination_condition=termination,
        selector_prompt=_SELECTOR_PROMPT,
        allow_repeated_speaker=True,
        selector_func=selector_func,
    )
    return GroupChatSession(team, name_by_slug, clients, workbenches)
