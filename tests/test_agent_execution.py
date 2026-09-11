"""End-to-end integration tests for the Agent Execution Platform.

Structured JUnit-style around a single test class:

* ``setup_class``    (JUnit ``@BeforeAll``): initialise the database and start
  *every* service, once, before any test in the class runs.
* ``teardown_class`` (JUnit ``@AfterAll``): shut every service down after the
  last test, no matter how the tests fared.
* test methods (JUnit ``@Test``): individual checks. Add more methods here and
  they all share the one running fleet.

The model (LLM gateway) API key is exposed as the ``MODEL_API_KEY`` class
variable — set it directly here, or leave it to be picked up from the
``MODEL_API_KEY`` / ``GROQ_API_KEY`` environment variable.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import ClassVar

import grpc
import pytest

from aep.agent_execution.v1 import service_pb2, service_pb2_grpc
from harness import (
    AES_ADDRESS,
    SEED_AGENTS,
    SEED_TENANT_ID,
    SEED_TOOLS,
    ServiceManager,
)

TENANT_ID = SEED_TENANT_ID

# job_svc job statuses still in flight; anything else is terminal.
_IN_FLIGHT_JOB_STATUSES = ("queued", "running")


class TestAgentExecutionPlatform:
    # Model API key for the LLM gateway. Override in place, or via env var.
    MODEL_API_KEY: ClassVar[str] = (
        os.environ.get("MODEL_API_KEY") or os.environ.get("GROQ_API_KEY") or "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"
    )

    manager: ClassVar[ServiceManager]

    @classmethod
    def setup_class(cls) -> None:
        """Bring the whole platform up once for this test class, loading the
        sample customers/invoices tables and seeding the catalog with a few
        tools and agents."""
        cls.manager = ServiceManager(model_api_key=cls.MODEL_API_KEY)
        cls.manager.init_db()
        cls.manager.load_sample_data()
        cls.manager.start_all()
        cls.manager.seed_data()

    @classmethod
    def teardown_class(cls) -> None:
        """Tear the whole platform down after the last test."""
        cls.manager.stop_all()

    # -- tests ------------------------------------------------------------------

    # -- basic CRUD (integration; snapshot the real DB after every operation) ---
    #
    # Each test below drives one resource through its create/read/update/delete
    # surface over real gRPC and, after *every* call, reads the underlying
    # Postgres straight back via ``ServiceManager.snapshot()`` to prove the
    # operation actually hit the database. Nothing is mocked: the calls go to
    # the live services and the assertions read the live tables. Each test runs
    # under its own throwaway tenant, reset first, so the suite is re-runnable
    # against a persistent database and the snapshots stay exact.

    async def test_tool_crud(self) -> None:
        """Create -> read -> update -> delete a tool over gRPC, snapshotting the
        `tools` table after each step."""
        tenant = "crud-tools-tenant"
        metadata = (("x-tenant-id", tenant),)
        await self.manager.reset_tenant(tenant_id=tenant)

        assert (await self.manager.snapshot(tenant_id=tenant))["tools"] == []

        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # CREATE
            created = await stub.CreateTool(
                service_pb2.CreateToolRequest(
                    name="crud_tool", description="initial description", mutating=False
                ),
                metadata=metadata,
            )
            tool_id = created.tool.id
            tools = (await self.manager.snapshot(tenant_id=tenant))["tools"]
            assert [t["id"] for t in tools] == [tool_id]
            assert tools[0]["name"] == "crud_tool"
            assert tools[0]["description"] == "initial description"
            assert tools[0]["mutating"] is False
            assert tools[0]["version"] == 1

            # READ (pure read: the snapshot must be unchanged)
            fetched = await stub.GetTool(
                service_pb2.GetToolRequest(
                    filter=service_pb2.GetToolRequestFilter(ids=[tool_id])
                ),
                metadata=metadata,
            )
            assert [t.id for t in fetched.tools] == [tool_id]
            tools = (await self.manager.snapshot(tenant_id=tenant))["tools"]
            assert [t["id"] for t in tools] == [tool_id]

            # UPDATE
            await stub.UpdateTool(
                service_pb2.UpdateToolRequest(
                    id=tool_id,
                    name="crud_tool_renamed",
                    description="updated description",
                    mutating=True,
                ),
                metadata=metadata,
            )
            tools = (await self.manager.snapshot(tenant_id=tenant))["tools"]
            assert tools[0]["name"] == "crud_tool_renamed"
            assert tools[0]["description"] == "updated description"
            assert tools[0]["mutating"] is True
            assert tools[0]["version"] == 2

            # DELETE
            deleted = await stub.DeleteTool(
                service_pb2.DeleteToolRequest(id=tool_id), metadata=metadata
            )
            assert deleted.success is True
            assert (await self.manager.snapshot(tenant_id=tenant))["tools"] == []

    async def test_agent_crud(self) -> None:
        """Create -> read -> update -> delete an agent over gRPC, snapshotting
        the `agents` and `agent_tools` tables after each step. An agent must be
        granted at least one tool, so a tool is created first."""
        tenant = "crud-agents-tenant"
        metadata = (("x-tenant-id", tenant),)
        await self.manager.reset_tenant(tenant_id=tenant)

        snapshot = await self.manager.snapshot(tenant_id=tenant)
        assert snapshot["agents"] == []
        assert snapshot["agent_tools"] == []

        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # A tool to grant the agent (agents with no tools are rejected).
            tool = await stub.CreateTool(
                service_pb2.CreateToolRequest(name="agent_crud_tool"), metadata=metadata
            )
            tool_id = tool.tool.id

            # CREATE
            created = await stub.CreateAgent(
                service_pb2.CreateAgentRequest(
                    name="crud_agent",
                    instructions="original instructions",
                    llm_config=service_pb2.LLMConfig(name="openai/gpt-oss-20b", temperature=0.5),
                    tool_config=service_pb2.ToolConfig(ids=[tool_id]),
                ),
                metadata=metadata,
            )
            agent_id = created.agent.id
            snapshot = await self.manager.snapshot(tenant_id=tenant)
            assert [a["id"] for a in snapshot["agents"]] == [agent_id]
            assert snapshot["agents"][0]["name"] == "crud_agent"
            assert snapshot["agents"][0]["instructions"] == "original instructions"
            assert snapshot["agents"][0]["llm_config_name"] == "openai/gpt-oss-20b"
            assert snapshot["agents"][0]["llm_config_temperature"] == pytest.approx(0.5)
            assert snapshot["agents"][0]["version"] == 1
            assert snapshot["agent_tools"] == [{"agent_id": agent_id, "tool_id": tool_id}]

            # READ (pure read: the snapshot must be unchanged)
            fetched = await stub.GetAgent(
                service_pb2.GetAgentRequest(
                    filter=service_pb2.GetAgentRequestFilter(ids=[agent_id])
                ),
                metadata=metadata,
            )
            assert [a.id for a in fetched.agents] == [agent_id]
            assert list(fetched.agents[0].tool_config.ids) == [tool_id]
            snapshot = await self.manager.snapshot(tenant_id=tenant)
            assert [a["id"] for a in snapshot["agents"]] == [agent_id]

            # UPDATE
            await stub.UpdateAgent(
                service_pb2.UpdateAgentRequest(
                    id=agent_id,
                    name="crud_agent_renamed",
                    instructions="updated instructions",
                    llm_config=service_pb2.LLMConfig(name="openai/gpt-oss-20b", temperature=0.25),
                    tool_config=service_pb2.ToolConfig(ids=[tool_id]),
                ),
                metadata=metadata,
            )
            snapshot = await self.manager.snapshot(tenant_id=tenant)
            assert snapshot["agents"][0]["name"] == "crud_agent_renamed"
            assert snapshot["agents"][0]["instructions"] == "updated instructions"
            assert snapshot["agents"][0]["llm_config_temperature"] == pytest.approx(0.25)
            assert snapshot["agents"][0]["version"] == 2
            assert snapshot["agent_tools"] == [{"agent_id": agent_id, "tool_id": tool_id}]

            # DELETE (the agent_tools grant cascades away with the agent)
            deleted = await stub.DeleteAgent(
                service_pb2.DeleteAgentRequest(id=agent_id), metadata=metadata
            )
            assert deleted.success is True
            snapshot = await self.manager.snapshot(tenant_id=tenant)
            assert snapshot["agents"] == []
            assert snapshot["agent_tools"] == []

    async def test_task_crud(self) -> None:
        """Create and read a task over gRPC, snapshotting the `tasks` table
        after each step. Tasks expose no update/delete (only approve/retry), so
        basic CRUD here is create + read. CreateTask also submits a job to
        job_svc, which the snapshot confirms landed in the `jobs` table."""
        tenant = "crud-tasks-tenant"
        metadata = (("x-tenant-id", tenant),)
        await self.manager.reset_tenant(tenant_id=tenant)
        # CreateTask requires the tenant to have at least one agent; this test
        # exercises task CRUD, not agent creation, so seed one directly.
        await self.manager.insert_agent(tenant_id=tenant)

        assert (await self.manager.snapshot(tenant_id=tenant))["tasks"] == []

        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # CREATE
            created = await stub.CreateTask(
                service_pb2.CreateTaskRequest(input="summarise the overdue invoices"),
                metadata=metadata,
            )
            task_id = created.task.id
            job_id = created.task.job_id
            assert created.task.status == service_pb2.TASK_STATUS_PENDING
            snapshot = await self.manager.snapshot(tenant_id=tenant)
            assert [t["id"] for t in snapshot["tasks"]] == [task_id]
            assert snapshot["tasks"][0]["input"] == "summarise the overdue invoices"
            assert snapshot["tasks"][0]["job_id"] == job_id
            assert snapshot["tasks"][0]["status"] == "pending"
            # CreateTask submits the task as a job; that job now exists in
            # job_svc's own database under the same tenant.
            assert job_id in [j["id"] for j in snapshot["jobs"]]

            # READ (pure read: the snapshot must be unchanged)
            fetched = await stub.GetTask(
                service_pb2.GetTaskRequest(
                    filter=service_pb2.GetTaskRequestFilter(ids=[task_id])
                ),
                metadata=metadata,
            )
            assert [t.id for t in fetched.tasks] == [task_id]
            snapshot = await self.manager.snapshot(tenant_id=tenant)
            assert [t["id"] for t in snapshot["tasks"]] == [task_id]

    async def test_job_crud(self) -> None:
        """Exercise a job's lifecycle through the AES task API only -- never
        calling job_svc directly -- snapshotting the `jobs` table after each
        step. A task is AES's handle onto exactly one job (see
        services/tasks.py): CreateTask is the job's create, GetTask its read,
        and the mutating RetryTask its update. The `jobs` rows in the snapshot
        are the ground truth proving AES drove job_svc under the hood."""
        tenant = "crud-jobs-tenant"
        metadata = (("x-tenant-id", tenant),)
        await self.manager.reset_tenant(tenant_id=tenant)
        # CreateTask requires the tenant to have at least one agent; this test
        # drives the job lifecycle via the task API, not agent creation, so
        # seed one directly.
        await self.manager.insert_agent(tenant_id=tenant)

        assert (await self.manager.snapshot(tenant_id=tenant))["jobs"] == []

        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # CREATE: CreateTask submits exactly one job to job_svc under the
            # hood and hands back that job's id on the task.
            created = await stub.CreateTask(
                service_pb2.CreateTaskRequest(input="a job created via the AES task API"),
                metadata=metadata,
            )
            task_id = created.task.id
            job_id = created.task.job_id
            assert job_id
            jobs = (await self.manager.snapshot(tenant_id=tenant))["jobs"]
            assert [j["id"] for j in jobs] == [job_id]
            assert jobs[0]["type"] == "agent_execution"

            # READ: GetTask reads the job back through AES by its task handle.
            fetched = await stub.GetTask(
                service_pb2.GetTaskRequest(
                    filter=service_pb2.GetTaskRequestFilter(ids=[task_id])
                ),
                metadata=metadata,
            )
            assert [t.id for t in fetched.tasks] == [task_id]
            assert fetched.tasks[0].job_id == job_id
            jobs = (await self.manager.snapshot(tenant_id=tenant))["jobs"]
            assert [j["id"] for j in jobs] == [job_id]

            # UPDATE: RetryTask is AES's job-mutation path. The just-created job
            # is still queued (not failed), so job_svc's state guard rejects the
            # retry and AES surfaces it as FAILED_PRECONDITION -- the whole round
            # trip exercised through AES, with no direct job_svc call.
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.RetryTask(
                    service_pb2.RetryTaskRequest(task_id=task_id), metadata=metadata
                )
            assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            jobs = (await self.manager.snapshot(tenant_id=tenant))["jobs"]
            assert [j["id"] for j in jobs] == [job_id]

    async def test_retry_task_rejected_once_job_is_dead(self) -> None:
        """A task whose job has been dead-lettered must not be retryable.

        `dead` is terminal: both the automatic (attempts) and manual
        (retry_count) budgets are spent, so job_svc's RetryJob guard -- which
        admits only a `failed` job -- rejects it and AES surfaces that as
        FAILED_PRECONDITION. Same guard test_job_crud sees for a still-queued
        job, at the opposite (terminal) end of the lifecycle. The dead job and
        its task are inserted straight into Postgres (reaching `dead` for real
        would mean burning every retry), and the rejected retry must leave both
        untouched.
        """
        tenant = "e2e-retry-dead-tenant"
        metadata = (("x-tenant-id", tenant),)
        await self.manager.reset_tenant(tenant_id=tenant)

        # Simulate a job that has exhausted every retry and been dead-lettered,
        # plus the AES task that handles it.
        task_id, job_id = await self.manager.insert_dead_job(tenant_id=tenant)
        assert (await self.manager.get_job(job_id))["status"] == "dead"

        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # RetryTask on a dead job is refused by job_svc's state guard and
            # surfaced as FAILED_PRECONDITION -- the task is never requeued.
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.RetryTask(
                    service_pb2.RetryTaskRequest(task_id=task_id), metadata=metadata
                )
            assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION

            # The task still reads as failed (its dead job is terminal).
            fetched = await stub.GetTask(
                service_pb2.GetTaskRequest(
                    filter=service_pb2.GetTaskRequestFilter(ids=[task_id])
                ),
                metadata=metadata,
            )
            assert fetched.tasks[0].status == service_pb2.TASK_STATUS_FAILED

        # The rejected retry left the job dead: no requeue, no retry_count bump.
        jobs = (await self.manager.snapshot(tenant_id=tenant))["jobs"]
        assert [j["id"] for j in jobs] == [job_id]
        assert jobs[0]["status"] == "dead"
        assert jobs[0]["retry_count"] == 3

    async def test_mutating_tool_pauses_for_approval_then_completes(self) -> None:
        """The approval flow: a mutating tool pauses the job for human approval,
        and ApproveTask resumes it to completion.

        The gate lives in the orchestrator's AgentToolWorkbench and is driven
        purely by a tool's catalog `mutating` flag (an unapproved mutating call
        is refused before it ever reaches mcp_svc), so this marks query_database
        -- which has a real mcp_svc execution binding -- as mutating to exercise
        the gate end to end without depending on a tool that has no binding. The
        agent's first tool call is refused pending approval -> job_svc parks the
        job at `waiting_approval` (TASK_STATUS_WAITING_APPROVAL); ApproveTask
        requeues it and the resumed run is allowed to make the call, so the task
        reaches COMPLETED. Its own throwaway tenant keeps it from disturbing the
        (non-mutating) query_database that test_query_database_tool_auto_executes
        relies on.
        """
        tenant = "e2e-approval-tenant"
        metadata = (("x-tenant-id", tenant),)
        await self.manager.reset_tenant(tenant_id=tenant)

        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            tool = await stub.CreateTool(
                service_pb2.CreateToolRequest(
                    name="query_database",
                    description=(
                        "Run a read-only SQL SELECT query against the "
                        "customers/invoices tables and return the matching rows."
                    ),
                    mutating=True,
                ),
                metadata=metadata,
            )
            # A single agent granted the mutating tool. A single-agent tenant is
            # supported on purpose: the orchestrator pads its execute turn with a
            # passive placeholder to meet SelectorGroupChat's two-participant
            # minimum (see groupchat.py's _make_solo_selector), so one real agent
            # is enough to drive the approval flow -- and this exercises that path.
            await stub.CreateAgent(
                service_pb2.CreateAgentRequest(
                    name="Data Analyst",
                    instructions=(
                        "Answer questions about customers and invoices using the "
                        "query_database tool to run SQL SELECT queries."
                    ),
                    llm_config=service_pb2.LLMConfig(name="openai/gpt-oss-20b", temperature=0.2),
                    tool_config=service_pb2.ToolConfig(ids=[tool.tool.id]),
                ),
                metadata=metadata,
            )

            created = await stub.CreateTask(
                service_pb2.CreateTaskRequest(
                    input="How many invoices are in the database? Reply with just the number."
                ),
                metadata=metadata,
            )
            task_id = created.task.id

            async def task_status() -> int:
                fetched = await stub.GetTask(
                    service_pb2.GetTaskRequest(
                        filter=service_pb2.GetTaskRequestFilter(ids=[task_id])
                    ),
                    metadata=metadata,
                )
                return fetched.tasks[0].status

            # 1) The mutating call must pause the job pending approval. Poll for
            # any settled/paused status so a run that never pauses fails fast
            # (rather than burning the whole deadline).
            settled = {
                service_pb2.TASK_STATUS_WAITING_APPROVAL,
                service_pb2.TASK_STATUS_COMPLETED,
                service_pb2.TASK_STATUS_FAILED,
            }
            deadline = time.monotonic() + 180.0
            status = created.task.status
            while status not in settled:
                if time.monotonic() > deadline:
                    pytest.fail(f"task {task_id} never paused for approval (status={status})")
                await asyncio.sleep(1.0)
                status = await task_status()
            assert status == service_pb2.TASK_STATUS_WAITING_APPROVAL, status

            # 2) Approve and drive to completion, re-approving if a later mutating
            # step in a multi-step plan pauses too. ApproveTask requeues the paused
            # job (waiting_approval -> queued); the poller re-runs it from its
            # checkpoint with the mutating call now permitted.
            approvals = 0
            deadline = time.monotonic() + 180.0
            while status not in (
                service_pb2.TASK_STATUS_COMPLETED,
                service_pb2.TASK_STATUS_FAILED,
            ):
                if time.monotonic() > deadline:
                    pytest.fail(f"task {task_id} did not finish after approval (status={status})")
                if status == service_pb2.TASK_STATUS_WAITING_APPROVAL:
                    resp = await stub.ApproveTask(
                        service_pb2.ApproveTaskRequest(task_id=task_id), metadata=metadata
                    )
                    assert resp.success is True
                    approvals += 1
                await asyncio.sleep(1.0)
                status = await task_status()

            assert status == service_pb2.TASK_STATUS_COMPLETED, status
            assert approvals >= 1

    async def test_query_database_tool_auto_executes(self) -> None:
        """Add a tool + agent, then fire a single CreateTask gRPC call at AES
        and let the already-running pipeline execute it on its own.

        No test code talks to mcp_svc or the orchestrator directly: job_svc's
        poller picks up the job CreateTask creates, the agent's own LLM decides
        on its own to call the new `query_database` tool (the task input below
        only describes the business question, not the tool), and mcp_svc
        executes it against the seeded customers/invoices data.
        """
        metadata = (("x-tenant-id", TENANT_ID),)
        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # Add the tool (idempotent: reuse it if a prior run already created it).
            existing_tools = await stub.GetTool(
                service_pb2.GetToolRequest(), metadata=metadata
            )
            tool_id = next(
                (t.id for t in existing_tools.tools if t.name == "query_database"), None
            )
            if tool_id is None:
                created_tool = await stub.CreateTool(
                    service_pb2.CreateToolRequest(
                        name="query_database",
                        description=(
                            "Run a read-only SQL SELECT query against the "
                            "customers/invoices tables and return the matching rows."
                        ),
                    ),
                    metadata=metadata,
                )
                tool_id = created_tool.tool.id

            # Add an agent granted that tool (idempotent by name). Its
            # instructions are the only place the tool is named; the task
            # input the test submits below only describes the business ask.
            existing_agents = await stub.GetAgent(
                service_pb2.GetAgentRequest(), metadata=metadata
            )
            agent = next(
                (a for a in existing_agents.agents if a.name == "Data Analyst"), None
            )
            if agent is None:
                created_agent = await stub.CreateAgent(
                    service_pb2.CreateAgentRequest(
                        name="Data Analyst",
                        instructions=(
                            "Answer questions about customers and invoices using the "
                            "query_database tool to run SQL SELECT queries against "
                            "the customers and invoices tables."
                        ),
                        llm_config=service_pb2.LLMConfig(
                            name="openai/gpt-oss-20b", temperature=0.2
                        ),
                        tool_config=service_pb2.ToolConfig(ids=[tool_id]),
                    ),
                    metadata=metadata,
                )
                agent = created_agent.agent

            # The single gRPC call: this alone creates a job, which the already
            # running job_svc/orchestrator/mcp_svc pick up and execute with no
            # further action from the test. Deliberately phrased as a plain
            # business question, with no mention of any tool.
            created_task = await stub.CreateTask(
                service_pb2.CreateTaskRequest(
                    input=(
                        "What is the total amount of overdue invoices for the "
                        "customer named 'Acme Corp'? Reply with just the number."
                    )
                ),
                metadata=metadata,
            )
            job_id = created_task.task.job_id

        # Poll job_svc's own database for the per-step progress this assertion
        # checks: it is the authoritative source and carries the full step trail
        # (AES's GetTask now refreshes and surfaces the final result, but not the
        # individual steps). See ServiceManager.get_job.
        deadline = time.monotonic() + 180.0
        job = await self.manager.get_job(job_id)
        while job["status"] in _IN_FLIGHT_JOB_STATUSES:
            if time.monotonic() > deadline:
                pytest.fail(f"job {job_id} did not finish within 180s (status={job['status']})")
            await asyncio.sleep(1.0)
            job = await self.manager.get_job(job_id)

        assert job["status"] == "succeeded", job

        # Confirm the reply carries the seeded invoice's real amount, not a
        # hallucinated guess -- i.e. that the agent actually used the tool.
        outputs = " ".join(step["output"] for step in job["progress"].get("steps", {}).values())
        assert re.search(r"75,?000", outputs), outputs

    async def test_task_progress_api_reports_step_history_to_completion(self) -> None:
        """Drive a task to completion and observe it end to end through the new
        `GetTaskProgress` RPC.

        This is the same auto-executing pipeline as
        test_query_database_tool_auto_executes, but where that test had to read
        job_svc's database directly for the per-step trail (no proto surfaced
        it), this drives everything through AES's own gRPC surface: it polls
        `GetTaskProgress` until the task settles, checking the progress
        invariants at every observation, then asserts the completed view carries
        the full ordered step history, a 100% bar and the final answer.
        """
        metadata = (("x-tenant-id", TENANT_ID),)
        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # Ensure the query_database tool + Data Analyst agent exist for this
            # tenant (idempotent by name), so the task has an agent to run
            # against and CreateTask's "tenant must have agents" precondition is
            # satisfied.
            existing_tools = await stub.GetTool(
                service_pb2.GetToolRequest(), metadata=metadata
            )
            tool_id = next(
                (t.id for t in existing_tools.tools if t.name == "query_database"), None
            )
            if tool_id is None:
                created_tool = await stub.CreateTool(
                    service_pb2.CreateToolRequest(
                        name="query_database",
                        description=(
                            "Run a read-only SQL SELECT query against the "
                            "customers/invoices tables and return the matching rows."
                        ),
                    ),
                    metadata=metadata,
                )
                tool_id = created_tool.tool.id

            existing_agents = await stub.GetAgent(
                service_pb2.GetAgentRequest(), metadata=metadata
            )
            if not any(a.name == "Data Analyst" for a in existing_agents.agents):
                await stub.CreateAgent(
                    service_pb2.CreateAgentRequest(
                        name="Data Analyst",
                        instructions=(
                            "Answer questions about customers and invoices using the "
                            "query_database tool to run SQL SELECT queries against "
                            "the customers and invoices tables."
                        ),
                        llm_config=service_pb2.LLMConfig(
                            name="openai/gpt-oss-20b", temperature=0.2
                        ),
                        tool_config=service_pb2.ToolConfig(ids=[tool_id]),
                    ),
                    metadata=metadata,
                )

            created = await stub.CreateTask(
                service_pb2.CreateTaskRequest(
                    input=(
                        "What is the total amount of overdue invoices for the "
                        "customer named 'Acme Corp'? Reply with just the number."
                    )
                ),
                metadata=metadata,
            )
            task_id = created.task.id

            async def get_progress():
                resp = await stub.GetTaskProgress(
                    service_pb2.GetTaskProgressRequest(task_id=task_id),
                    metadata=metadata,
                )
                return resp.progress

            # Poll the progress API until the task settles, approving if a
            # mutating step ever pauses (the seeded query_database is read-only,
            # so it normally won't). The progress invariants must hold at every
            # observation along the way.
            terminal = {
                service_pb2.TASK_STATUS_COMPLETED,
                service_pb2.TASK_STATUS_FAILED,
            }
            deadline = time.monotonic() + 180.0
            progress = await get_progress()
            while progress.status not in terminal:
                if time.monotonic() > deadline:
                    pytest.fail(
                        f"task {task_id} did not finish within 180s "
                        f"(status={progress.status}, summary={progress.summary!r})"
                    )
                assert progress.task_id == task_id
                assert 0.0 <= progress.percent_complete <= 100.0
                # steps_completed always matches the surfaced history length.
                assert progress.steps_completed == len(progress.steps)
                if progress.status == service_pb2.TASK_STATUS_WAITING_APPROVAL:
                    assert progress.requires_approval is True
                    await stub.ApproveTask(
                        service_pb2.ApproveTaskRequest(task_id=task_id),
                        metadata=metadata,
                    )
                await asyncio.sleep(1.0)
                progress = await get_progress()

            # Completed: a full, ordered step history with real per-step outputs,
            # a 100% bar, and the final answer surfaced on the progress view.
            assert progress.status == service_pb2.TASK_STATUS_COMPLETED, progress.summary
            assert progress.percent_complete == pytest.approx(100.0)
            assert progress.requires_approval is False
            assert progress.steps_completed >= 1
            assert len(progress.steps) == progress.steps_completed
            step_indices = [s.index for s in progress.steps]
            assert step_indices == sorted(step_indices)  # ordered history
            assert progress.output  # final answer surfaced on the task
            # The step trail carries the agent's actual tool-backed answer, not a
            # hallucination -- the seeded Acme Corp overdue total is 75,000.
            step_outputs = " ".join(step.output for step in progress.steps)
            assert re.search(r"75,?000", step_outputs), step_outputs
