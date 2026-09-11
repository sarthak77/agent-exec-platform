"""Async SQLAlchemy engine + session factory.

Read-only: no DDL bootstrap here — agent_execution_service owns the
`tools` table's schema and creates it on its own startup.
"""

from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mcp_svc.config import settings


def _url() -> URL:
    pg = settings.postgres
    # URL.create escapes credentials/host, so a password with URL-special
    # characters (@, :, /, ...) can't corrupt the DSN.
    return URL.create(
        "postgresql+asyncpg",
        username=pg.user,
        password=pg.password,
        host=pg.host,
        port=pg.port,
        database=pg.database,
    )


engine: AsyncEngine = create_async_engine(_url())
Sessions = async_sessionmaker(engine, expire_on_commit=False)
