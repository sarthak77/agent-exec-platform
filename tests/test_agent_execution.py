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
from harness import AES_ADDRESS, SEED_AGENTS, SEED_TENANT_ID, SEED_TOOLS, ServiceManager

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

    async def test_get_task_query_against_aes(self) -> None:
        """Fire a real gRPC query at the running agent_execution_service.

        GetTask is a pure read: it exercises the live gRPC surface plus the
        service's Postgres connection end to end, without depending on the
        downstream execution pipeline. A fresh tenant simply has no tasks yet.
        """
        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)
            response = await stub.GetTask(
                service_pb2.GetTaskRequest(),
                metadata=(("x-tenant-id", TENANT_ID),),
            )

        # A well-formed response with a (repeated) tasks field is what we assert;
        # the list is empty for a tenant that has created nothing.
        assert list(response.tasks) == []

    async def test_seeded_tools_and_agents_present(self) -> None:
        """The catalog seeded at startup is queryable over gRPC."""
        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)
            tools = await stub.GetTool(
                service_pb2.GetToolRequest(), metadata=(("x-tenant-id", TENANT_ID),)
            )
            agents = await stub.GetAgent(
                service_pb2.GetAgentRequest(), metadata=(("x-tenant-id", TENANT_ID),)
            )

        tool_names = {t.name for t in tools.tools}
        agent_names = {a.name for a in agents.agents}
        assert {t.name for t in SEED_TOOLS} <= tool_names
        assert {a.name for a in SEED_AGENTS} <= agent_names

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

        # AES's GetTask won't help here: TaskService only refreshes its local
        # status snapshot on a mutating call (approve/retry), so it never
        # reflects a job that simply runs to completion on its own. Poll
        # job_svc's own database instead — the authoritative source, and
        # where the agent's actual output ends up (see ServiceManager.get_job).
        deadline = time.monotonic() + 90.0
        job = await self.manager.get_job(job_id)
        while job["status"] in _IN_FLIGHT_JOB_STATUSES:
            if time.monotonic() > deadline:
                pytest.fail(f"job {job_id} did not finish within 90s (status={job['status']})")
            await asyncio.sleep(1.0)
            job = await self.manager.get_job(job_id)

        assert job["status"] == "succeeded", job

        # Confirm the reply carries the seeded invoice's real amount, not a
        # hallucinated guess -- i.e. that the agent actually used the tool.
        outputs = " ".join(step["output"] for step in job["progress"].get("steps", {}).values())
        assert re.search(r"75,?000", outputs), outputs
