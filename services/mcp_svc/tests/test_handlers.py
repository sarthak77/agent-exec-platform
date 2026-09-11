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
