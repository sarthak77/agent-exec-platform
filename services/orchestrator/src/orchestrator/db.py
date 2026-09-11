"""Async SQLAlchemy engine + session factory.

Read-only: no DDL bootstrap here — agent_execution_service owns the
`agents`/`agent_tools`/`tools` tables' schema and creates them on its own
startup.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from orchestrator.config import settings


def _dsn() -> str:
    pg = settings.postgres
    return f"postgresql+asyncpg://{pg.user}:{pg.password}@{pg.host}:{pg.port}/{pg.database}"


engine: AsyncEngine = create_async_engine(_dsn())
Sessions = async_sessionmaker(engine, expire_on_commit=False)
