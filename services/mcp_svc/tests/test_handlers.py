"""Unit tests for the code-defined tool execution bindings (handlers.py),
exercising the built-in http_request handler against a mocked transport."""

from __future__ import annotations

import json

import httpx
import pytest

from mcp_svc import handlers
from mcp_svc.handlers import ToolContext, get_handler

_CTX = ToolContext(tenant_id="t-test")


def _mock_httpx(monkeypatch, responder) -> None:
    """Route the handler's outbound requests through an httpx.MockTransport."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(responder)
        return real_client(**kwargs)

    monkeypatch.setattr(handlers.httpx, "AsyncClient", factory)


def test_registry_exposes_http_request() -> None:
    handler = get_handler("http_request")
    assert handler is not None
    assert handler.input_schema["required"] == ["method", "url"]
    assert get_handler("does_not_exist") is None


async def test_http_request_returns_json_body(monkeypatch) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://example.test/api?q=1"
        return httpx.Response(200, json={"ok": True})

    _mock_httpx(monkeypatch, responder)

    handler = get_handler("http_request")
    result = await handler.run(
        {"method": "get", "url": "https://example.test/api", "query": {"q": "1"}}, _CTX
    )

    payload = json.loads(result)
    assert payload == {"status_code": 200, "json": {"ok": True}}


async def test_http_request_returns_text_for_non_json(monkeypatch) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="plain body")

    _mock_httpx(monkeypatch, responder)

    result = await get_handler("http_request").run(
        {"method": "POST", "url": "https://example.test", "body": {"a": 1}}, _CTX
    )

    payload = json.loads(result)
    assert payload["status_code"] == 201
    assert payload["text"] == "plain body"
    assert payload["truncated"] is False


async def test_http_request_reports_network_error_as_payload(monkeypatch) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _mock_httpx(monkeypatch, responder)

    result = await get_handler("http_request").run(
        {"method": "GET", "url": "https://example.test"}, _CTX
    )

    assert json.loads(result) == {"error": "boom"}


async def test_http_request_requires_method_and_url() -> None:
    with pytest.raises(ValueError):
        await get_handler("http_request").run({"url": "https://example.test"}, _CTX)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:6379/",
        "http://localhost:8000/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://foo.localhost/",
        "ftp://example.test/",
    ],
)
async def test_http_request_blocks_internal_targets(url: str) -> None:
    with pytest.raises(ValueError):
        await get_handler("http_request").run({"method": "GET", "url": url}, _CTX)


# --------------------------------------------------------------------------- #
# query_database                                                              #
# --------------------------------------------------------------------------- #


@pytest.fixture()
async def query_db_sessions(monkeypatch):
    """In-memory customers/invoices tables (not ORM-mapped in this service —
    query_database runs raw SQL against them) seeded with two tenants, so
    tenant-scoping and the allow-list can be exercised against a real query
    engine instead of mocked."""
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            sa_text(
                "CREATE TABLE customers (id TEXT PRIMARY KEY, tenant_id TEXT, "
                "name TEXT, email TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "CREATE TABLE invoices (id TEXT PRIMARY KEY, tenant_id TEXT, "
                "customer_id TEXT, amount NUMERIC, status TEXT)"
            )
        )
        await conn.execute(
            sa_text(
                "INSERT INTO customers VALUES "
                "('CUST-A1', 'tenant-a', 'Acme A', 'a@acme.example')"
            )
        )
        await conn.execute(
            sa_text(
                "INSERT INTO customers VALUES "
                "('CUST-B1', 'tenant-b', 'Beta Corp', 'b@beta.example')"
            )
        )
        await conn.execute(
            sa_text(
                "INSERT INTO invoices VALUES "
                "('INV-A1', 'tenant-a', 'CUST-A1', 1000, 'overdue')"
            )
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(handlers, "Sessions", sessions)
    yield sessions
    await engine.dispose()


async def test_query_database_scopes_to_caller_tenant(query_db_sessions) -> None:
    result = await get_handler("query_database").run(
        {"query": "SELECT * FROM customers"}, ToolContext(tenant_id="tenant-a")
    )
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["rows"][0]["id"] == "CUST-A1"


async def test_query_database_join_across_allowed_tables(query_db_sessions) -> None:
    result = await get_handler("query_database").run(
        {
            "query": (
                "SELECT c.name, i.amount FROM customers c "
                "JOIN invoices i ON i.customer_id = c.id"
            )
        },
        ToolContext(tenant_id="tenant-a"),
    )
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["rows"][0]["name"] == "Acme A"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM customers, invoices_secret",  # comma-join bypass attempt
        "SELECT * FROM public.customers",  # schema-qualification bypass attempt
        "WITH customers AS (SELECT 1) SELECT * FROM customers",  # CTE-shadow attempt
        "DELETE FROM customers",  # mutation
        "SELECT * FROM customers; DROP TABLE customers",  # multi-statement
        "SELECT * FROM secrets",  # non-allow-listed table
    ],
)
async def test_query_database_rejects_bypass_attempts(query_db_sessions, query: str) -> None:
    result = await get_handler("query_database").run(
        {"query": query}, ToolContext(tenant_id="tenant-a")
    )
    assert "error" in json.loads(result)


# --------------------------------------------------------------------------- #
# Accounting-assistant demo tools                                             #
# --------------------------------------------------------------------------- #


async def test_retrieve_invoices_filters_by_customer_and_status() -> None:
    result = await get_handler("retrieve_invoices").run(
        {"customer_id": "CUST-001", "status": "overdue"}, _CTX
    )
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["invoices"][0]["id"] == "INV-1001"


async def test_retrieve_invoices_unfiltered_returns_all() -> None:
    payload = json.loads(await get_handler("retrieve_invoices").run({}, _CTX))
    assert payload["count"] == len(handlers._INVOICES)


async def test_retrieve_customer_by_id_and_by_name() -> None:
    by_id = json.loads(
        await get_handler("retrieve_customer").run({"customer_id": "CUST-001"}, _CTX)
    )
    assert by_id["customer"]["name"] == "Acme Corp"

    by_name = json.loads(
        await get_handler("retrieve_customer").run({"name": "globex llc"}, _CTX)
    )
    assert by_name["customer"]["id"] == "CUST-002"

    missing = json.loads(
        await get_handler("retrieve_customer").run({"customer_id": "NOPE"}, _CTX)
    )
    assert "error" in missing


async def test_create_email_draft_echoes_structured_draft() -> None:
    payload = json.loads(
        await get_handler("create_email_draft").run(
            {"to": "a@b.example", "subject": "Hi", "body": "Body"}, _CTX
        )
    )
    assert payload["draft"] == {"to": "a@b.example", "subject": "Hi", "body": "Body"}


@pytest.fixture()
async def sqlite_sessions(monkeypatch):
    """In-memory email_approvals table, wired in place of the real Postgres
    Sessions so the send_email approval gate can be exercised end to end."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from mcp_svc.models import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(handlers, "Sessions", sessions)
    yield sessions
    await engine.dispose()


async def test_send_email_gated_then_sent_then_idempotent(sqlite_sessions) -> None:
    from sqlalchemy import select, update

    from mcp_svc.models import EmailApprovalRow

    args = {"to": "billing@acme.example", "subject": "Overdue", "body": "Please pay."}

    # First send: no approval on file -> held, marker returned, pending row created.
    first = json.loads(await get_handler("send_email").run(args, _CTX))
    assert first["status"] == handlers.APPROVAL_REQUIRED
    assert handlers.APPROVAL_REQUIRED in json.dumps(first)

    # Still pending on a retry -> still held (does not send).
    second = json.loads(await get_handler("send_email").run(args, _CTX))
    assert second["status"] == handlers.APPROVAL_REQUIRED

    # Approve out of band (as agent_execution_service would on ApproveTask).
    async with sqlite_sessions.begin() as session:
        await session.execute(
            update(EmailApprovalRow)
            .where(EmailApprovalRow.tenant_id == _CTX.tenant_id)
            .values(status="approved")
        )

    # Now it sends, and the row is marked sent.
    third = json.loads(await get_handler("send_email").run(args, _CTX))
    assert third["status"] == "sent"
    async with sqlite_sessions() as session:
        row = (
            await session.scalars(
                select(EmailApprovalRow).where(
                    EmailApprovalRow.recipient == "billing@acme.example"
                )
            )
        ).one()
        assert row.sent is True

    # A resumed run re-executing the send step must not double-send.
    fourth = json.loads(await get_handler("send_email").run(args, _CTX))
    assert fourth["status"] == "already_sent"


async def test_send_email_isolated_per_tenant(sqlite_sessions) -> None:
    args = {"to": "shared@x.example", "subject": "S", "body": "B"}
    other = ToolContext(tenant_id="other-tenant")

    # Tenant A holds one pending approval.
    await get_handler("send_email").run(args, _CTX)
    # Approving tenant A does not approve tenant B: B still gets held.
    from sqlalchemy import update

    from mcp_svc.models import EmailApprovalRow

    async with sqlite_sessions.begin() as session:
        await session.execute(
            update(EmailApprovalRow)
            .where(EmailApprovalRow.tenant_id == _CTX.tenant_id)
            .values(status="approved")
        )
    held = json.loads(await get_handler("send_email").run(args, other))
    assert held["status"] == handlers.APPROVAL_REQUIRED
