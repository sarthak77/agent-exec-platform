"""ToolService — create/get/update/delete on the `tools` resource, backed
by Postgres. Hard delete: agent_tools links cascade via the FK. The
tenant-owned-vs-global distinction is gone along with `tenant_id` from the
Tool proto message — every tool now belongs to exactly the tenant that
created it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_execution_service.errors import ConflictError, NotFoundError
from agent_execution_service.models import ToolRow, new_id, now


class ToolService:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def create(
        self, *, tenant_id: str, name: str, description: str | None, mutating: bool = False
    ) -> ToolRow:
        row = ToolRow(
            id=new_id(), tenant_id=tenant_id, name=name, description=description, mutating=mutating
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await _flush_or_conflict(session, name)
        return row

    async def get(self, *, tenant_id: str, ids: list[str]) -> list[ToolRow]:
        async with self._sessions() as session:
            stmt = select(ToolRow).where(ToolRow.tenant_id == tenant_id)
            if ids:
                stmt = stmt.where(ToolRow.id.in_(ids))
            return list((await session.scalars(stmt)).all())

    async def update(
        self, *, tenant_id: str, tool_id: str, name: str, description: str, mutating: bool
    ) -> ToolRow:
        async with self._sessions.begin() as session:
            row = await session.get(ToolRow, tool_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"tool {tool_id} not found")
            row.name = name
            row.description = description or None
            row.mutating = mutating
            row.version += 1
            row.updated_at = now()
            await _flush_or_conflict(session, name)
        return row

    async def delete(self, *, tenant_id: str, tool_id: str) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(ToolRow, tool_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError(f"tool {tool_id} not found")
            await session.delete(row)


async def _flush_or_conflict(session, name: str) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"tool named {name!r} already exists") from exc
