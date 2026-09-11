"""Async SQLAlchemy engine + session factory, and DDL bootstrap."""

from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from job_svc.config import settings
from job_svc.models import Base


def _url() -> URL:
    # URL.create escapes each component, so a password containing characters
    # like @ : / # does not corrupt the connection string.
    pg = settings.postgres
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


async def init_models() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
