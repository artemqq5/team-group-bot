from datetime import datetime
from typing import Optional

from sqlalchemy import select, update

from bot.data import db_connection
from bot.data.db_connection import log_exc_with_try_except
from bot.data.models import McpAuditLogModel

EXPORT_LIMIT = 5000


def _s(value: Optional[str], limit: int) -> Optional[str]:
    return None if value is None else value.replace("\x00", "")[:limit]


class McpAuditRepository:

    @staticmethod
    @log_exc_with_try_except()
    async def add(method: str, status: str, key_id: Optional[int] = None, tool_name: Optional[str] = None,
                  arguments: Optional[dict] = None, reason: Optional[str] = None,
                  result_count: Optional[int] = None, client_ip: Optional[str] = None) -> int:
        """Повертає id; при збої None (декоратор) — виклик тоді відхиляється (fail-closed)."""
        # PG відхиляє NUL і занадто довгі рядки: чистимо/обрізаємо на межі
        row = McpAuditLogModel(
            created_at=datetime.utcnow(), key_id=key_id, method=_s(method, 64), tool_name=_s(tool_name, 64),
            arguments=arguments, status=_s(status, 16), reason=_s(reason, 64),
            result_count=result_count, client_ip=_s(client_ip, 64), hits=1,
        )
        async with db_connection.async_session() as session:
            session.add(row)
            await session.commit()
            return row.id

    @staticmethod
    @log_exc_with_try_except()
    async def increment_hits(audit_id: int) -> bool:
        async with db_connection.async_session() as session:
            await session.execute(
                update(McpAuditLogModel).where(McpAuditLogModel.id == audit_id)
                .values(hits=McpAuditLogModel.hits + 1)
            )
            await session.commit()
            return True

    @staticmethod
    @log_exc_with_try_except()
    async def page_by_key(key_id: int, offset: int, limit: int) -> list[McpAuditLogModel]:
        async with db_connection.async_session() as session:
            result = await session.execute(
                select(McpAuditLogModel).where(McpAuditLogModel.key_id == key_id)
                .order_by(McpAuditLogModel.created_at.desc(), McpAuditLogModel.id.desc())
                .offset(offset).limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    @log_exc_with_try_except()
    async def page_unknown(offset: int, limit: int) -> list[McpAuditLogModel]:
        async with db_connection.async_session() as session:
            result = await session.execute(
                select(McpAuditLogModel).where(McpAuditLogModel.key_id.is_(None))
                .order_by(McpAuditLogModel.created_at.desc(), McpAuditLogModel.id.desc())
                .offset(offset).limit(limit)
            )
            return list(result.scalars().all())

    @staticmethod
    async def export_rows(key_id: int) -> Optional[list[McpAuditLogModel]]:
        return await McpAuditRepository.page_by_key(key_id, 0, EXPORT_LIMIT)
