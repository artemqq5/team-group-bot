import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, or_, select, update

from bot.data import db_connection
from bot.data.db_connection import log_exc_with_try_except
from bot.data.models import McpKeyModel, McpOAuthClientModel, McpOAuthTokenModel
from bot.data.repositories.McpKeyRepository import hash_token

ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=90)


def _key_live(now: datetime):
    """Умови «ключ живий»: не відкликаний, не видалений, не прострочений."""
    return (McpKeyModel.revoked == False, McpKeyModel.deleted_at.is_(None),  # noqa: E712
            or_(McpKeyModel.expires_at.is_(None), McpKeyModel.expires_at > now))


def _new_pair(key_id: int, client_id: str, family_id: str, now: datetime):
    access = "tgba_" + secrets.token_urlsafe(32)
    refresh = "tgbr_" + secrets.token_urlsafe(32)
    rows = [
        McpOAuthTokenModel(key_id=key_id, client_id=client_id, family_id=family_id, kind="access",
                           token_hash=hash_token(access), created_at=now, expires_at=now + ACCESS_TTL),
        McpOAuthTokenModel(key_id=key_id, client_id=client_id, family_id=family_id, kind="refresh",
                           token_hash=hash_token(refresh), created_at=now, expires_at=now + REFRESH_TTL),
    ]
    return access, refresh, rows


class McpOAuthRepository:

    @staticmethod
    @log_exc_with_try_except()
    async def create_client(name: Optional[str], redirect_uris: list, client_ip: Optional[str]) -> str:
        client_id = "mcpc_" + secrets.token_urlsafe(24)
        async with db_connection.async_session() as session:
            session.add(McpOAuthClientModel(
                client_id=client_id,
                client_name=None if name is None else name.replace("\x00", "")[:255],
                redirect_uris=list(redirect_uris), created_at=datetime.utcnow(),
                client_ip=None if client_ip is None else client_ip.replace("\x00", "")[:64],
            ))
            await session.commit()
        return client_id

    @staticmethod
    @log_exc_with_try_except()
    async def get_client(client_id: str) -> Optional[McpOAuthClientModel]:
        async with db_connection.async_session() as session:
            return await session.get(McpOAuthClientModel, client_id[:64])

    @staticmethod
    @log_exc_with_try_except()
    async def get_live_key(key_id: int) -> Optional[McpKeyModel]:
        async with db_connection.async_session() as session:
            result = await session.execute(
                select(McpKeyModel).where(McpKeyModel.id == key_id, *_key_live(datetime.utcnow())))
            return result.scalar_one_or_none()

    @staticmethod
    @log_exc_with_try_except()
    async def issue_pair(key_id: int, client_id: str, family_id: Optional[str] = None):
        """Нова пара у новій або наявній родині: (access, refresh, family_id). Plaintext — лише тут."""
        now = datetime.utcnow()
        access, refresh, rows = _new_pair(key_id, client_id, family_id or secrets.token_hex(16), now)
        async with db_connection.async_session() as session:
            session.add_all(rows)
            await session.commit()
        return access, refresh, rows[0].family_id

    @staticmethod
    @log_exc_with_try_except()
    async def revoke_family(family_id: str) -> int:
        async with db_connection.async_session() as session:
            res = await session.execute(
                update(McpOAuthTokenModel)
                .where(McpOAuthTokenModel.family_id == family_id, McpOAuthTokenModel.revoked_at.is_(None))
                .values(revoked_at=datetime.utcnow()))
            await session.commit()
            return res.rowcount

    @staticmethod
    @log_exc_with_try_except()
    async def resolve_access(token: str):
        """(key, None) валідний; (key|None, reason) ні; None при збої БД. Ключ перевіряється наживо."""
        now = datetime.utcnow()
        async with db_connection.async_session() as session:
            result = await session.execute(
                select(McpOAuthTokenModel, McpKeyModel)
                .join(McpKeyModel, McpKeyModel.id == McpOAuthTokenModel.key_id)
                .where(McpOAuthTokenModel.token_hash == hash_token(token),
                       McpOAuthTokenModel.kind == "access"))
            row = result.first()
        if row is None:
            return None, "invalid_token"
        tok, key = row
        if tok.revoked_at is not None or tok.expires_at <= now:
            return key, "expired"
        if key.revoked or key.deleted_at is not None:
            return key, "revoked"
        if key.expires_at is not None and key.expires_at <= now:
            return key, "expired"
        return key, None

    @staticmethod
    @log_exc_with_try_except()
    async def refresh(token: str, client_id: str):
        """Ротація refresh-токена. Повертає (status, data):
        ("ok", (access, refresh, key, family_id)) | ("reuse", (family_id, key_id)) |
        ("client_mismatch", None) | ("invalid", None).
        None — збій БД."""
        now = datetime.utcnow()
        h = hash_token(token)
        async with db_connection.async_session() as session:
            row = (await session.execute(
                select(McpOAuthTokenModel).where(McpOAuthTokenModel.token_hash == h,
                                                 McpOAuthTokenModel.kind == "refresh"))).scalar_one_or_none()
            if row is None:
                return "invalid", None
            if row.client_id != client_id:
                return "client_mismatch", None
            t = McpOAuthTokenModel
            row_id, family_id, key_id, row_client = row.id, row.family_id, row.key_id, row.client_id
            # атомарно: лише один паралельний запит «виграє» ротацію
            res = await session.execute(
                update(t).where(t.id == row_id, t.used_at.is_(None), t.revoked_at.is_(None), t.expires_at > now)
                .values(used_at=now))
            if res.rowcount == 0:
                await session.rollback()
                fresh = (await session.execute(select(t).where(t.id == row_id))).scalar_one()
                if fresh.used_at is not None or fresh.revoked_at is not None:
                    return "reuse", (fresh.family_id, fresh.key_id)
                return "invalid", None  # прострочений
            key = (await session.execute(
                select(McpKeyModel).where(McpKeyModel.id == key_id, *_key_live(now)))).scalar_one_or_none()
            if key is None:
                await session.commit()
                return "invalid", None
            await session.execute(
                update(t).where(t.family_id == family_id, t.kind == "access", t.revoked_at.is_(None))
                .values(revoked_at=now))
            access, refresh, rows = _new_pair(key_id, row_client, family_id, now)
            session.add_all(rows)
            await session.commit()
            return "ok", (access, refresh, key, family_id)

    @staticmethod
    @log_exc_with_try_except()
    async def delete_expired(older_than_days: int = 7) -> int:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)
        async with db_connection.async_session() as session:
            res = await session.execute(
                delete(McpOAuthTokenModel).where(McpOAuthTokenModel.expires_at < cutoff))
            await session.commit()
            return res.rowcount
