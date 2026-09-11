"""Async SQLAlchemy engine + session factory, and DDL bootstrap."""

from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from agent_execution_service.config import settings
from agent_execution_service.models import Base


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


async def init_models() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
