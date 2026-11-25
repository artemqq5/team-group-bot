import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.data import db_connection
from bot.data.db_connection import log_exc_with_try_except
from bot.data.models import ChatMessageModel, ChatModel, McpAuditLogModel

BATCH = 10000


async def upsert_message(conn, values: dict, edited: bool) -> None:
    """Запис через вже відкрите з'єднання (engine.begin() у middleware). id ніколи не чіпаємо."""
    stmt = pg_insert(ChatMessageModel).values(**values)
    if edited:
        update = {k: stmt.excluded[k] for k in ("text", "message_type", "has_media", "edit_date", "is_edited")}
        stmt = stmt.on_conflict_do_update(constraint="uq_chat_messages_group_msg", set_=update)
    else:
        # повторна доставка не перезаписує вже відредагований текст
        stmt = stmt.on_conflict_do_nothing(constraint="uq_chat_messages_group_msg")
    await conn.execute(stmt)


async def _delete_batched(conn, where) -> int:
    t = ChatMessageModel
    total = 0
    while True:
        res = await conn.execute(
            delete(t).where(t.id.in_(select(t.id).where(where).limit(BATCH)))
        )
        total += res.rowcount
        if res.rowcount < BATCH:
            return total


async def delete_old_messages(retention_days: int, keep_last: int, now: Optional[datetime] = None) -> int:
    """Видаляє повідомлення старші за retention_days; для груп з chats лишає keep_last найновіших."""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    t = ChatMessageModel
    total = 0
    async with db_connection.engine.begin() as conn:
        candidates = (await conn.execute(
            select(t.group_id).where(t.sent_at < cutoff).distinct()
        )).scalars().all()
        if not candidates:
            return 0
        registered = set((await conn.execute(
            select(ChatModel.group_id).where(ChatModel.group_id.in_(candidates))
        )).scalars().all())

        for gid in candidates:
            where = and_(t.group_id == gid, t.sent_at < cutoff)
            if gid in registered and keep_last > 0:
                count = (await conn.execute(
                    select(func.count()).select_from(t).where(t.group_id == gid)
                )).scalar_one()
                if count <= keep_last:
                    continue
                row = (await conn.execute(
                    select(t.sent_at, t.id).where(t.group_id == gid)
                    .order_by(t.sent_at.desc(), t.id.desc()).offset(keep_last - 1).limit(1)
                )).first()
                if row is None:
                    continue
                bt, bi = row
                where = and_(where, or_(t.sent_at < bt, and_(t.sent_at == bt, t.id < bi)))
            total += await _delete_batched(conn, where)
    return total


async def delete_old_audit(audit_days: int, now: Optional[datetime] = None) -> int:
    if audit_days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=audit_days)
    total = 0
    async with db_connection.engine.begin() as conn:
        while True:
            a = McpAuditLogModel
            res = await conn.execute(
                delete(a).where(a.id.in_(select(a.id).where(a.created_at < cutoff).limit(BATCH)))
            )
            total += res.rowcount
            if res.rowcount < BATCH:
                break
    logging.info(f"retention: audit rows deleted={total}")
    return total


class MessageStats:
    """Запити для індикаторів здоров'я захоплення (UI адміна)."""

    @staticmethod
    @log_exc_with_try_except()
    async def group_activity(group_id: str, since: datetime):
        """(кількість з since, час останнього повідомлення або None)."""
        t = ChatMessageModel
        async with db_connection.async_session() as session:
            cnt = (await session.execute(
                select(func.count()).select_from(t).where(t.group_id == str(group_id), t.sent_at >= since)
            )).scalar_one()
            last = (await session.execute(
                select(func.max(t.sent_at)).where(t.group_id == str(group_id))
            )).scalar_one()
            return cnt, last

    @staticmethod
    @log_exc_with_try_except()
    async def count_since(since: datetime) -> int:
        async with db_connection.async_session() as session:
            return (await session.execute(
                select(func.count()).select_from(ChatMessageModel).where(ChatMessageModel.sent_at >= since)
            )).scalar_one()

    @staticmethod
    @log_exc_with_try_except()
    async def silent_groups(since: datetime) -> list:
        """Зареєстровані групи (chats) без жодного захопленого повідомлення з since: [(group_id, title)]."""
        t = ChatMessageModel
        async with db_connection.async_session() as session:
            result = await session.execute(
                select(ChatModel.group_id, ChatModel.title)
                .where(~select(t.id).where(t.group_id == ChatModel.group_id, t.sent_at >= since).exists())
                .order_by(ChatModel.title)
            )
            return [(r[0], r[1]) for r in result.all()]
