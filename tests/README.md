# Integration tests

End-to-end tests that boot the **entire** platform (all five services) plus its
Postgres databases, then drive it over the real wire protocols.

## Layout

| File | Purpose |
| --- | --- |
| `harness.py` | `ServiceManager` — DB init + start/stop of every service subprocess, plus `snapshot()`/`reset_tenant()` DB helpers. |
| `test_agent_execution.py` | JUnit-style test class; basic CRUD tests + a sample gRPC query against AES. |
| `conftest.py` | Puts the generated `aep.*` gRPC stubs on `sys.path`. |
| `pyproject.toml` | Test-only dependencies (gRPC client, asyncpg, pytest). |
| `sql/sample_data.sql` | `customers`/`invoices` sample tables the tests query against. |

## Lifecycle

The suite is organised as a class so the fleet starts once and is shared:

- `setup_class` : `init_db()`, `load_sample_data()`, `start_all()`, then `seed_data()`.
- `teardown_class` : `stop_all()`.
- test methods.

## Sample data

`load_sample_data()` runs `sql/sample_data.sql` against the
`agent_execution_service` database right after `init_db()` (it only needs the
database to exist, not any service running). The file creates `customers` and
`invoices` tables and inserts a deliberately varied set of rows — 10
customers, 20 invoices spanning `paid`/`overdue`/`pending` statuses, amounts
from ~3K to 250K, and due dates across several months — so filtering by
customer, status, amount, or due date each has more than one matching and
non-matching row to exercise. It's **idempotent** (`CREATE TABLE IF NOT
EXISTS` + `ON CONFLICT DO NOTHING`), so re-running the suite never errors or
duplicates rows. Edit `sql/sample_data.sql` directly to change the rows.

## Seed data

`seed_data()` populates the catalog once the fleet is up, over the live AES
gRPC API (so it exercises the real create path). It is **idempotent** — tools
and agents are matched by name and only the missing ones are created, so
re-running the suite never duplicates or errors. Edit `SEED_TOOLS` /
`SEED_AGENTS` in `harness.py` to change what's seeded. Everything is created
under the `SEED_TENANT_ID` tenant (`integration-tenant`), which the tests query.

Add new checks as methods on `TestAgentExecutionPlatform` (or new classes that
follow the same `setup_class`/`teardown_class` pattern).

## Basic CRUD tests

`test_tool_crud` / `test_agent_crud` / `test_task_crud` / `test_job_crud` drive
each resource through its create/read/update/delete surface over the real wire —
**all via AES gRPC**, with **no mocking** and no direct calls to any downstream
service. Jobs are driven through AES's task API (a task is AES's handle onto
exactly one job): `CreateTask` creates the job, `GetTask` reads it, and
`RetryTask` exercises the guarded job-mutation path (a freshly created, still
`queued` job isn't retryable, so AES surfaces job_svc's guard as
`FAILED_PRECONDITION`). After *every* call they read the underlying Postgres
straight back via `ServiceManager.snapshot(tenant_id=...)` — a
`{table: [row, ...]}` dump of the `agents`/`tools`/`agent_tools`/`tasks`/`jobs`
tables for one tenant — and assert the row the RPC claimed to write is actually
there (or gone). Each test runs under its own throwaway tenant and calls
`ServiceManager.reset_tenant()` first, so the suite is re-runnable against a
persistent database and the snapshots stay exact.

## Prerequisites

- **Postgres** running on `localhost:5432` (user/password `postgres`/`postgres`).
  Override with `AEP_PG_HOST`, `AEP_PG_PORT`, `AEP_PG_USER`, `AEP_PG_PASSWORD`,
  `AEP_PG_ADMIN_DB`. The harness creates the `agent_execution_service` and
  `job_svc` databases if they are missing; each service creates its own tables
  on startup.
- **`uv`** on `PATH`; each service is launched via `uv run` inside its own
  project so it uses its own pinned dependencies.

## Model API key

Exposed as the `MODEL_API_KEY` class variable in `test_agent_execution.py`. Set
it there, or via the `MODEL_API_KEY` / `GROQ_API_KEY` environment variable. Only
the gateway consumes it, and only a non-empty value is needed to start (it is
not validated until an actual model call is made).

## Run

```bash
cd tests
uv run pytest -v
```

## Known Limitations
- Couldn't test e2e the entire flow.
- Missing single docker based setup. (Was facing some permission issues)

Per-service logs for a run are written to `tests/.logs/<service>.log`; on a
startup failure the harness prints the tail of the offending service's log.
