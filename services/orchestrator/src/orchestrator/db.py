"""Async SQLAlchemy engine + session factory.

Read-only: no DDL bootstrap here — agent_execution_service owns the
`agents`/`agent_tools`/`tools` tables' schema and creates them on its own
startup.
"""

from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from orchestrator.config import settings


def _dsn() -> URL:
    pg = settings.postgres
    # URL.create percent-encodes each component, so passwords/users containing
    # characters like '@', ':' or '/' don't corrupt the connection string.
    return URL.create(
        "postgresql+asyncpg",
        username=pg.user,
        password=pg.password,
        host=pg.host,
        port=pg.port,
        database=pg.database,
    )


engine: AsyncEngine = create_async_engine(_dsn())
Sessions = async_sessionmaker(engine, expire_on_commit=False)
