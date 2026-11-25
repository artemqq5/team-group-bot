import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, update

from bot.data import db_connection
from bot.data.db_connection import log_exc_with_try_except
from bot.data.models import McpKeyModel

TOUCH_INTERVAL = timedelta(seconds=60)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class McpKeyRepository:

    @staticmethod
    @log_exc_with_try_except()
    async def create(label: str, created_by: str, categories: list):
        """Повертає (plaintext, key). Plaintext показується один раз і ніде не зберігається."""
        token = "tgb_" + secrets.token_urlsafe(32)
        key = McpKeyModel(
            token_hash=hash_token(token),
            token_prefix=token[:8],
            label=label.replace("\x00", "")[:255],
            created_by=str(created_by),
            created_at=datetime.utcnow(),
            categories=list(categories),
            expires_at=None,
        )
        async with db_connection.async_session() as session:
            session.add(key)
            await session.commit()
            return token, key

    @staticmethod
    @log_exc_with_try_except()
    async def list_keys() -> list[McpKeyModel]:
        async with db_connection.async_session() as session:
            result = await session.execute(select(McpKeyModel).order_by(McpKeyModel.id.desc()))
            return list(result.scalars().all())

    @staticmethod
    @log_exc_with_try_except()
    async def get(key_id: int) -> Optional[McpKeyModel]:
        async with db_connection.async_session() as session:
            return await session.get(McpKeyModel, key_id)

    @staticmethod
    @log_exc_with_try_except()
    async def revoke(key_id: int, revoked_by: str) -> bool:
        async with db_connection.async_session() as session:
            res = await session.execute(
                update(McpKeyModel)
                .where(McpKeyModel.id == key_id, McpKeyModel.revoked == False)  # noqa: E712
                .values(revoked=True, revoked_at=datetime.utcnow(), revoked_by=str(revoked_by))
            )
            await session.commit()
            return res.rowcount > 0

    @staticmethod
    @log_exc_with_try_except()
    async def resolve_by_token(token: str):
        """(key, None) якщо валідний; (key|None, reason) якщо ні; None при збої БД."""
        async with db_connection.async_session() as session:
            result = await session.execute(
                select(McpKeyModel).where(McpKeyModel.token_hash == hash_token(token))
            )
            key = result.scalar_one_or_none()
        if key is None:
            return None, "invalid_token"
        if key.revoked:
            return key, "revoked"
        if key.expires_at is not None and key.expires_at <= datetime.utcnow():
            return key, "expired"
        return key, None

    @staticmethod
    @log_exc_with_try_except()
    async def touch(key_id: int) -> None:
        """last_used_at не частіше ніж раз на 60 с — без запису на кожен виклик."""
        now = datetime.utcnow()
        async with db_connection.async_session() as session:
            await session.execute(
                update(McpKeyModel)
                .where(McpKeyModel.id == key_id,
                       or_(McpKeyModel.last_used_at.is_(None), McpKeyModel.last_used_at < now - TOUCH_INTERVAL))
                .values(last_used_at=now)
            )
            await session.commit()
