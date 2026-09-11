"""Process lifecycle for the whole platform, used by the integration tests.

``ServiceManager`` brings the system up and tears it down as a unit:

* ``init_db()``          ensures the per-service Postgres databases exist. Each
  service creates its *own tables* on startup (via its ``init_models()``); this
  step only guarantees the databases those services connect to are present.
* ``load_sample_data()`` creates and populates the ``customers``/``invoices``
  tables the tests query against, from ``tests/sql/sample_data.sql``.
* ``start_all()`` launches every service as a subprocess (``uv run`` inside each
  service's own project, so each uses its own pinned dependency set) and blocks
  until every service is accepting connections on its port.
* ``stop_all()``  terminates every process, children first, and drains logs.

Services are started in dependency order (leaf dependencies first) so that by the
time a caller-facing service like agent_execution_service is up, everything it
talks to is already listening.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO_ROOT / "services"
LOG_DIR = Path(__file__).resolve().parent / ".logs"


@dataclass(frozen=True)
class ServiceSpec:
    """One service in the platform.

    ``name``     project directory name under services/.
    ``script``   the console script defined in that project's pyproject.
    ``port``     the TCP port it listens on once ready.
    """

    name: str
    script: str
    port: int


# Startup order matters: each service is listed after the services it calls, so
# dependencies are already accepting connections before their callers start.
#   gateway <- orchestrator <- job_svc <- agent_execution_service
#   mcp_svc  <- orchestrator
SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec("gateway", "gateway", 50054),
    ServiceSpec("mcp_svc", "mcp-svc", 8003),
    ServiceSpec("orchestrator", "orchestrator", 50053),
    ServiceSpec("job_svc", "job-svc", 50052),
    ServiceSpec("agent_execution_service", "agent-execution-service", 50051),
)

# Databases the services connect to (from each service's config.toml). Their
# tables are created by the services themselves; we only ensure the DBs exist.
REQUIRED_DATABASES: tuple[str, ...] = ("agent_execution_service", "job_svc")

# agent_execution_service gRPC address the seeder and CRUD tests talk to.
AES_ADDRESS = "localhost:50051"

# job_svc's own database. Its `jobs.progress` column holds the agent's actual
# step outputs (see JobRunner in job_svc), which neither the Task nor Job
# proto surfaces — tests that need to see a real answer read it directly.
JOB_SVC_DATABASE = "job_svc"

# agent_execution_service's own database (agents/tools/agent_tools/tasks). The
# CRUD tests read it back directly to prove each gRPC call actually persisted.
AES_DATABASE = "agent_execution_service"

# Tenant the seed data belongs to (shared with the tests).
SEED_TENANT_ID = "integration-tenant"

# Sample business-domain tables (customers, invoices) the tests query
# against, defined in one file and loaded into the database below.
SAMPLE_DATA_SQL = Path(__file__).resolve().parent / "sql" / "sample_data.sql"
SAMPLE_DATA_DATABASE = AES_DATABASE


@dataclass(frozen=True)
class ToolSeed:
    name: str
    description: str


@dataclass(frozen=True)
class AgentSeed:
    name: str
    instructions: str
    llm_model: str
    temperature: float
    tool_names: tuple[str, ...]  # resolved to tool ids at seed time


# Seed catalog created once the fleet is up. Tools are created first so agents
# can be linked to them by name.
SEED_TOOLS: tuple[ToolSeed, ...] = (
    ToolSeed("web_search", "Search the public web for up-to-date information."),
    ToolSeed("calculator", "Evaluate arithmetic expressions."),
    ToolSeed("send_email", "Send an email to a recipient (requires human approval)."),
)

SEED_AGENTS: tuple[AgentSeed, ...] = (
    AgentSeed(
        name="Research Assistant",
        instructions="Answer questions by searching the web and doing arithmetic when useful.",
        llm_model="openai/gpt-oss-20b",
        temperature=0.7,
        tool_names=("web_search", "calculator"),
    ),
    AgentSeed(
        name="Email Assistant",
        instructions="Draft and send emails on the user's behalf, pausing for approval.",
        llm_model="openai/gpt-oss-20b",
        temperature=0.2,
        tool_names=("send_email",),
    ),
)


@dataclass
class ServiceManager:
    """Owns the lifecycle of the full service fleet for a test run."""

    model_api_key: str
    startup_timeout: float = 60.0
    # Postgres admin connection used to create the per-service databases.
    pg_host: str = field(default_factory=lambda: os.environ.get("AEP_PG_HOST", "localhost"))
    pg_port: int = field(default_factory=lambda: int(os.environ.get("AEP_PG_PORT", "5432")))
    pg_user: str = field(default_factory=lambda: os.environ.get("AEP_PG_USER", "postgres"))
    pg_password: str = field(
        default_factory=lambda: os.environ.get("AEP_PG_PASSWORD", "postgres")
    )
    pg_admin_db: str = field(default_factory=lambda: os.environ.get("AEP_PG_ADMIN_DB", "postgres"))

    _procs: dict[str, subprocess.Popen] = field(default_factory=dict, init=False)
    _log_files: list = field(default_factory=list, init=False)

    # -- database ---------------------------------------------------------------

    def init_db(self) -> None:
        """Create every required database if it does not already exist."""
        asyncio.run(self._ensure_databases())

    async def _ensure_databases(self) -> None:
        import asyncpg

        try:
            conn = await asyncpg.connect(
                host=self.pg_host,
                port=self.pg_port,
                user=self.pg_user,
                password=self.pg_password,
                database=self.pg_admin_db,
            )
        except Exception as exc:  # pragma: no cover - environment problem
            raise RuntimeError(
                f"cannot reach Postgres at {self.pg_host}:{self.pg_port} as "
                f"{self.pg_user!r} (db {self.pg_admin_db!r}): {exc}. Start Postgres "
                "or override AEP_PG_* env vars."
            ) from exc
        try:
            for db in REQUIRED_DATABASES:
                exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db)
                if not exists:
                    # Identifier can't be parameterised; DB names here are trusted constants.
                    await conn.execute(f'CREATE DATABASE "{db}"')
        finally:
            await conn.close()

    def load_sample_data(self) -> None:
        """Create (if missing) and populate the customers/invoices tables the
        tests query against, from the single sql file at ``tests/sql``.
        Idempotent (``CREATE TABLE IF NOT EXISTS`` + ``ON CONFLICT DO
        NOTHING``), so re-running the suite never errors or duplicates rows.
        Only needs the database to exist, so it can run right after
        ``init_db()``, before any service is started.
        """
        asyncio.run(self._load_sample_data())

    async def _load_sample_data(self) -> None:
        import asyncpg

        conn = await asyncpg.connect(
            host=self.pg_host,
            port=self.pg_port,
            user=self.pg_user,
            password=self.pg_password,
            database=SAMPLE_DATA_DATABASE,
        )
        try:
            # Strip comment lines first: a stray ";" in a prose comment would
            # otherwise split a statement in the wrong place.
            lines = (
                line
                for line in SAMPLE_DATA_SQL.read_text().splitlines()
                if not line.strip().startswith("--")
            )
            sql = "\n".join(lines)
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(statement)
        finally:
            await conn.close()

    async def _connect(self, database: str):
        """Open a fresh asyncpg connection to one of the platform's Postgres
        databases with the harness's admin credentials. The caller owns closing
        it. Kept here so get_job/snapshot/reset_tenant share one connection path.
        """
        import asyncpg

        return await asyncpg.connect(
            host=self.pg_host,
            port=self.pg_port,
            user=self.pg_user,
            password=self.pg_password,
            database=database,
        )

    async def get_job(self, job_id: str) -> dict:
        """Read a job's live status and checkpointed progress (plan + per-step
        outputs) straight out of job_svc's own database.

        Tests poll this for the job's *full* checkpointed progress -- the plan
        and every step's output -- which no proto surfaces (the Task/Job
        `result` fields carry only the final answer, not the per-step trail).
        job_svc's JobRunner persists that trail to `jobs.progress`, so reading
        it straight from the database here is how tests inspect the real work.
        """
        import json

        conn = await self._connect(JOB_SVC_DATABASE)
        try:
            row = await conn.fetchrow("SELECT status, progress FROM jobs WHERE id = $1", job_id)
        finally:
            await conn.close()
        if row is None:
            raise LookupError(f"job {job_id} not found")
        progress = row["progress"]
        return {
            "status": row["status"],
            "progress": json.loads(progress) if isinstance(progress, str) else progress,
        }

    # -- database snapshots (integration-test ground truth) ---------------------

    async def snapshot(self, *, tenant_id: str) -> dict[str, list[dict]]:
        """Read every platform table for one tenant straight out of Postgres and
        return it as ``{table_name: [row_dict, ...]}``.

        This is the CRUD integration tests' ground truth: after each gRPC call
        they snapshot the real databases -- no mocks, no going through the
        services -- and assert the row the RPC claimed to write is actually
        there (or gone). Scoped to a single tenant so a test only sees its own
        rows: ``agents``/``tools``/``tasks`` carry ``tenant_id`` directly, the
        ``agent_tools`` join is scoped through its agents, and ``jobs`` lives in
        job_svc's own database.
        """
        aes = await self._connect(AES_DATABASE)
        try:
            tools = await aes.fetch(
                "SELECT * FROM tools WHERE tenant_id = $1 ORDER BY created_at", tenant_id
            )
            agents = await aes.fetch(
                "SELECT * FROM agents WHERE tenant_id = $1 ORDER BY created_at", tenant_id
            )
            tasks = await aes.fetch(
                "SELECT * FROM tasks WHERE tenant_id = $1 ORDER BY created_at", tenant_id
            )
            agent_tools = await aes.fetch(
                "SELECT link.agent_id, link.tool_id FROM agent_tools AS link "
                "JOIN agents AS a ON a.id = link.agent_id "
                "WHERE a.tenant_id = $1 ORDER BY link.agent_id, link.tool_id",
                tenant_id,
            )
        finally:
            await aes.close()

        jobs_conn = await self._connect(JOB_SVC_DATABASE)
        try:
            jobs = await jobs_conn.fetch(
                "SELECT * FROM jobs WHERE tenant_id = $1 ORDER BY created_at", tenant_id
            )
        finally:
            await jobs_conn.close()

        return {
            "tools": [dict(row) for row in tools],
            "agents": [dict(row) for row in agents],
            "agent_tools": [dict(row) for row in agent_tools],
            "tasks": [dict(row) for row in tasks],
            "jobs": [dict(row) for row in jobs],
        }

    async def reset_tenant(self, *, tenant_id: str) -> None:
        """Delete every row owned by ``tenant_id`` from both databases so a CRUD
        test starts from a known-empty baseline and the suite stays re-runnable
        against a persistent database. Refuses the seeded tenant so it can never
        wipe the shared seed/sample data -- it is only ever meant for a test's
        own throwaway tenant.
        """
        if tenant_id == SEED_TENANT_ID:
            raise ValueError("refusing to reset the seeded tenant")
        aes = await self._connect(AES_DATABASE)
        try:
            # agent_tools carries no tenant_id; clear it via this tenant's agents
            # first (the FK would cascade on agent delete too, but be explicit).
            await aes.execute(
                "DELETE FROM agent_tools WHERE agent_id IN "
                "(SELECT id FROM agents WHERE tenant_id = $1)",
                tenant_id,
            )
            await aes.execute("DELETE FROM tasks WHERE tenant_id = $1", tenant_id)
            await aes.execute("DELETE FROM agents WHERE tenant_id = $1", tenant_id)
            await aes.execute("DELETE FROM tools WHERE tenant_id = $1", tenant_id)
        finally:
            await aes.close()

        jobs_conn = await self._connect(JOB_SVC_DATABASE)
        try:
            await jobs_conn.execute("DELETE FROM jobs WHERE tenant_id = $1", tenant_id)
        finally:
            await jobs_conn.close()

    # -- seed data --------------------------------------------------------------

    def seed_data(self) -> None:
        """Populate the catalog with a few tools and agents, via the live AES
        gRPC API. Idempotent: existing tools/agents (matched by name) are left
        as-is, so re-running the suite doesn't duplicate or error. Requires
        agent_execution_service to already be up (call after ``start_all``).
        """
        asyncio.run(self._seed())

    async def _seed(self) -> None:
        import grpc

        from aep.agent_execution.v1 import service_pb2, service_pb2_grpc

        metadata = (("x-tenant-id", SEED_TENANT_ID),)
        async with grpc.aio.insecure_channel(AES_ADDRESS) as channel:
            stub = service_pb2_grpc.AgentExecutionServiceStub(channel)

            # Tools: create any that don't already exist for this tenant.
            existing_tools = await stub.GetTool(service_pb2.GetToolRequest(), metadata=metadata)
            tool_id_by_name = {t.name: t.id for t in existing_tools.tools}
            for tool in SEED_TOOLS:
                if tool.name in tool_id_by_name:
                    continue
                created = await stub.CreateTool(
                    service_pb2.CreateToolRequest(name=tool.name, description=tool.description),
                    metadata=metadata,
                )
                tool_id_by_name[tool.name] = created.tool.id

            # Agents: create any that don't already exist, linking seeded tools.
            existing_agents = await stub.GetAgent(service_pb2.GetAgentRequest(), metadata=metadata)
            existing_agent_names = {a.name for a in existing_agents.agents}
            for agent in SEED_AGENTS:
                if agent.name in existing_agent_names:
                    continue
                tool_ids = [
                    tool_id_by_name[n] for n in agent.tool_names if n in tool_id_by_name
                ]
                await stub.CreateAgent(
                    service_pb2.CreateAgentRequest(
                        name=agent.name,
                        instructions=agent.instructions,
                        llm_config=service_pb2.LLMConfig(
                            name=agent.llm_model, temperature=agent.temperature
                        ),
                        tool_config=service_pb2.ToolConfig(ids=tool_ids),
                    ),
                    metadata=metadata,
                )

    # -- process lifecycle ------------------------------------------------------

    def start_all(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        try:
            for spec in SERVICES:
                self._start_one(spec)
                self._wait_until_ready(spec)
        except Exception:
            # Never leave orphaned processes if one service fails to come up.
            self.stop_all()
            raise

    def _start_one(self, spec: ServiceSpec) -> None:
        env = os.environ.copy()
        # The gateway needs a model API key at startup; every other service
        # ignores it. Set both provider var names so it works regardless of the
        # configured provider.
        env["GROQ_API_KEY"] = self.model_api_key
        env["OPENAI_API_KEY"] = self.model_api_key

        log_path = LOG_DIR / f"{spec.name}.log"
        log_file = open(log_path, "w")  # noqa: SIM115 - closed in stop_all
        self._log_files.append(log_file)

        proc = subprocess.Popen(
            ["uv", "run", spec.script],
            cwd=str(SERVICES_DIR / spec.name),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so we can kill children too
        )
        self._procs[spec.name] = proc

    def _wait_until_ready(self, spec: ServiceSpec) -> None:
        deadline = time.monotonic() + self.startup_timeout
        proc = self._procs[spec.name]
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{spec.name} exited early with code {proc.returncode}.\n"
                    f"{self._log_tail(spec.name)}"
                )
            if self._port_open(spec.port):
                return
            time.sleep(0.25)
        raise TimeoutError(
            f"{spec.name} did not open port {spec.port} within {self.startup_timeout}s.\n"
            f"{self._log_tail(spec.name)}"
        )

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("localhost", port)) == 0

    def stop_all(self) -> None:
        # Terminate in reverse order (callers before their dependencies).
        for spec in reversed(SERVICES):
            proc = self._procs.pop(spec.name, None)
            if proc is None:
                continue
            self._terminate(proc)
        for f in self._log_files:
            try:
                f.close()
            except Exception:
                pass
        self._log_files.clear()

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            # Kill the whole process group: `uv run` spawns the service as a child.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=5)

    @staticmethod
    def _log_tail(name: str, lines: int = 40) -> str:
        path = LOG_DIR / f"{name}.log"
        try:
            content = path.read_text().splitlines()
        except OSError:
            return f"(no log at {path})"
        tail = "\n".join(content[-lines:])
        return f"--- last {lines} lines of {path} ---\n{tail}"
