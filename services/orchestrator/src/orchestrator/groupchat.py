"""Builds the AutoGen group chat for a tenant: one AssistantAgent per row
in `agents` (see agents_repo.py), each attached to its own MCP tool
workbench scoped to that agent's granted tools (see mcp_workbench.py). The
participants are run under a SelectorGroupChat, whose LLM selector picks the
next speaker each turn.
"""

from __future__ import annotations

import re

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import (
    MaxMessageTermination,
    SourceMatchTermination,
    TextMessageTermination,
)
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

Select the single role from {participants} best suited to make progress on the
user's most recent request. Prefer the one agent that owns the tools or domain
the request needs, and keep picking that same agent until it has produced a
complete natural-language answer. Once the request has been answered, do not pick
another agent to restate or acknowledge it. Return only that role's name.
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

# A passive participant used ONLY to pad a single-agent tenant's execution turn
# to the two participants SelectorGroupChat requires (its constructor rejects
# fewer -- see build_group_chat). It has no tools and is never selected
# (_make_solo_selector routes every turn to the real agent), so it exists purely
# to satisfy the participant-count rule and never actually speaks.
_PLACEHOLDER_NAME = "Placeholder"
_PLACEHOLDER_SYSTEM_MESSAGE = (
    "You are an inactive placeholder and will never be asked to respond. If you "
    "somehow are, reply with a single space and nothing else."
)
_PLACEHOLDER_DESCRIPTION = (
    "Inactive placeholder with no tools; exists only to pad a single-agent team "
    "to the group chat's required minimum. Never select it."
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


def _make_solo_selector(agent_slug: str):
    """Route every execution-turn selection to the tenant's only real agent.

    A single-agent tenant's execution turn is padded with a passive placeholder
    to meet SelectorGroupChat's two-participant minimum (see build_group_chat);
    forcing the real agent here keeps that placeholder from ever being picked --
    and skips the selector's own model call, since there is only one real choice.
    """

    def _select(thread) -> str | None:
        return agent_slug

    return _select


def _execution_termination(agent_slugs: list[str]):
    """Termination for an execution (non-planning) turn.

    A SelectorGroupChat otherwise runs until MaxMessageTermination even after an
    agent has already answered, spending extra selector + agent round-trips (and,
    on a slow provider, real wall-clock) restating a finished reply. So end the
    turn the moment a domain agent emits a natural-language answer -- an AutoGen
    TextMessage, as opposed to the ToolCallSummaryMessage / tool events a tool
    call produces -- while still capping the turn with MaxMessageTermination.

    The text-answer check is scoped to the agent slugs on purpose: the group-chat
    manager runs the caller's own input (a source="user" TextMessage) through the
    termination condition before anyone speaks, so an unscoped
    TextMessageTermination would end the turn immediately, before any agent runs.
    """
    termination = MaxMessageTermination(settings.chat.max_messages)
    for slug in agent_slugs:
        termination = termination | TextMessageTermination(source=slug)
    return termination


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
    agent_slugs: list[str] = []

    for spec in agent_specs:
        slug = _slugify(spec.name, taken)
        name_by_slug[slug] = spec.name
        agent_slugs.append(slug)
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
        termination = MaxMessageTermination(settings.chat.max_messages) | SourceMatchTermination(
            [planner_slug]
        )
    else:
        # Execution call. SelectorGroupChat needs >=2 participants (its
        # constructor rejects fewer), so a single-agent tenant would fail to
        # execute. Pad it with a passive placeholder and force every turn to the
        # real agent, so the placeholder only makes up the count, never speaks.
        if len(participants) < 2:
            placeholder_slug = _slugify(_PLACEHOLDER_NAME, taken)
            name_by_slug[placeholder_slug] = _PLACEHOLDER_NAME
            placeholder_client = GatewayChatCompletionClient(temperature=0.0, stub=stub)
            clients.append(placeholder_client)
            participants.append(
                AssistantAgent(
                    placeholder_slug,
                    model_client=placeholder_client,
                    system_message=_PLACEHOLDER_SYSTEM_MESSAGE,
                    description=_PLACEHOLDER_DESCRIPTION,
                )
            )
            selector_func = _make_solo_selector(agent_slugs[0])
        # Stop as soon as a domain agent actually answers instead of padding out
        # to the message cap. See _execution_termination.
        termination = _execution_termination(agent_slugs)

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
