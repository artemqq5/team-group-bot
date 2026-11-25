"""Перевірки, які sqlite ховав: реальний upsert, ILIKE, NUL, довжини, tz, схема == моделі."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from bot.data import db_connection
from bot.data.models import Base, ChatMessageModel as M, McpAuditLogModel, UserModel
from bot.data.repositories import MessageRepository
from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.UserRepository import UserRepository
from bot.domain.middlewares import MessageCapture as mc
from bot.domain.middlewares.MessageCapture import MessageCapture, _values
from tests.conftest import TEST_URL
from tests.mcp_helpers import T0, call, make_client, make_key, seed_chat, seed_msg
from tests.test_message_capture import Handler, mk


def _vals(**kw):
    d = {"group_id": "g", "message_id": 1, "message_type": "text", "text": "orig", "has_media": False,
         "sent_at": datetime(2025, 1, 1), "edit_date": None, "is_edited": False}
    d.update(kw)
    return d


async def _write(engine, values, edited):
    async with engine.begin() as conn:
        await MessageRepository.upsert_message(conn, values, edited)


async def _row(engine):
    async with engine.connect() as conn:
        return (await conn.execute(select(M.id, M.text, M.is_edited, M.message_type))).one()


# ---- схема ----

async def test_migration_matches_models(db):
    def diff(sync_conn):
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True, "compare_server_default": True})
        return compare_metadata(ctx, Base.metadata)

    async with db.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_users_composite_pk(db):
    t = datetime(2025, 1, 1)
    assert await UserRepository.add_user(1, "u", "g1", t, "n", "en", "G1", None) is not None
    assert await UserRepository.add_user(1, "u", "g2", t, "n", "en", "G2", None) is not None  # той самий user, інша група
    assert await UserRepository.add_user(1, "u", "g1", t, "n", "en", "G1", None) is None  # дубль (user, group)
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(UserModel))).scalar_one() == 2


# ---- upsert ----

async def test_upsert_redelivery_edit_redelivery(db):
    await _write(db, _vals(), False)
    first = await _row(db)
    await _write(db, _vals(text="redelivered"), False)  # повторна доставка: без змін
    assert await _row(db) == first
    edited = _vals(text="new", is_edited=True, edit_date=datetime(2025, 1, 2), message_type="text")
    await _write(db, edited, True)
    r = await _row(db)
    assert r.id == first.id and r.text == "new" and r.is_edited is True
    await _write(db, _vals(text="orig-again"), False)  # не-edit після edit не повертає старий текст
    assert (await _row(db)).text == "new"


# ---- ILIKE / NUL ----

async def test_search_ilike_cyrillic(db):
    await seed_chat("-1", creo=True)
    await seed_msg("-1", 1, text="Привіт Світ")
    token, _ = await make_key(["creo"])
    async with make_client() as c:
        for q in ("привіт", "СВІТ", "ВіТ"):
            err, res = await call(c, token, "search_messages", {"query": q, "since": T0.isoformat(),
                                                                "until": (T0 + timedelta(days=1)).isoformat()})
            assert not err and len(res["messages"]) == 1, q


async def test_mcp_nul_inputs(db):
    await seed_chat("-1", creo=True)
    token, _ = await make_key(["creo"])
    async with make_client() as c:
        err, res = await call(c, token, "search_messages", {"query": "ab\x00cd"})
        assert err and "invalid" in res
        err, res = await call(c, token, "get_messages", {"chat_id": "-1\x00", "since": T0.isoformat()})
        assert err and res == "chat not found"


async def test_cursor_tz_aware_ok(db):
    await seed_chat("-1", creo=True)
    for i in range(3):
        await seed_msg("-1", i + 1, sent_at=T0 + timedelta(seconds=i))
    token, _ = await make_key(["creo"])
    async with make_client() as c:
        cur = f"{(T0).isoformat()}+02:00|1"
        err, res = await call(c, token, "get_messages", {"chat_id": "-1", "since": T0.isoformat(), "cursor": cur})
        assert not err
        err, res = await call(c, token, "get_messages",
                              {"chat_id": "-1", "since": "2025-01-10T14:00:00+02:00",
                               "until": "2025-01-10T15:00:00+02:00"})
        assert not err and len(res["messages"]) == 3


def test_values_strip_nul_and_truncate_but_not_ids():
    v = _values(mk(text="a\x00b", **{"from": {"id": 5, "is_bot": False, "first_name": "N" * 300 + "\x00",
                                                 "username": "u\x00"}}))
    assert v["text"] == "ab" and v["author_username"] == "u" and len(v["author_first_name"]) == 255
    assert v["group_id"] == "-100123" and v["author_id"] == "5"


async def test_capture_real_nul_and_long_values(db):
    m = mk(text="x\x00y", **{"from": {"id": 5, "is_bot": False, "first_name": "F" * 400, "username": "a\x00b"}})
    await MessageCapture(edited=False)(Handler(), m, {})
    async with db.connect() as conn:
        r = (await conn.execute(select(M.text, M.author_first_name, M.author_username))).one()
    assert r.text == "xy" and len(r.author_first_name) == 255 and r.author_username == "ab"


async def test_audit_strips_nul_and_truncates(db):
    aid = await McpAuditRepository.add(method="m\x00" + "x" * 100, status="denied", tool_name="t\x00",
                                       reason="r", client_ip="1.2.3.4\x00" + "9" * 100,
                                       arguments={"q": "a\x00b"})
    assert aid is not None
    async with db.connect() as conn:
        r = (await conn.execute(select(McpAuditLogModel))).one()
    assert "\x00" not in r.method and len(r.method) == 64 and r.tool_name == "t"
    assert "\x00" not in r.client_ip and len(r.client_ip) == 64
    assert r.arguments == {"q": "a\x00b"}  # json-колонка приймає \u0000


# ---- batched delete ----

async def test_batched_delete_loops(db, monkeypatch):
    monkeypatch.setattr(MessageRepository, "BATCH", 3)
    old = datetime(2025, 1, 1)
    async with db.begin() as conn:
        await conn.execute(insert(M), [{"group_id": "g", "message_id": i, "message_type": "text",
                                        "sent_at": old + timedelta(seconds=i)} for i in range(10)])
        await conn.execute(insert(McpAuditLogModel), [
            {"created_at": old, "method": "m", "status": "ok", "hits": 1} for _ in range(7)])
    now = datetime(2026, 11, 25)
    assert await MessageRepository.delete_old_messages(180, 100, now=now) == 10
    assert await MessageRepository.delete_old_audit(30, now=now) == 7
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(M))).scalar_one() == 0


async def test_batched_delete_exact_multiple_terminates(db, monkeypatch):
    monkeypatch.setattr(MessageRepository, "BATCH", 3)
    async with db.begin() as conn:
        await conn.execute(insert(M), [{"group_id": "g", "message_id": i, "message_type": "text",
                                        "sent_at": datetime(2025, 1, 1)} for i in range(6)])
    assert await MessageRepository.delete_old_messages(180, 100, now=datetime(2026, 11, 25)) == 6


# ---- таймаут захоплення не псує пул asyncpg ----

async def test_capture_timeout_does_not_poison_pool(monkeypatch):
    engine = create_async_engine(TEST_URL, poolclass=AsyncAdaptedQueuePool, pool_size=1, max_overflow=0,
                                 pool_timeout=5, connect_args={"timeout": 3, "command_timeout": 30})
    monkeypatch.setattr(db_connection, "engine", engine)

    async def slow(conn, values, edited):
        await conn.execute(text("select pg_sleep(5)"))

    real_upsert = MessageRepository.upsert_message
    monkeypatch.setattr(MessageRepository, "upsert_message", slow)
    real = asyncio.wait_for
    monkeypatch.setattr(mc.asyncio, "wait_for", lambda aw, timeout: real(aw, 0.3))
    try:
        h = Handler()
        assert await MessageCapture(edited=False)(h, mk(), {}) == "RET"
        assert h.calls == 1
        # той самий єдиний з'єднувач пулу далі працює (не лишився в зламаній транзакції)
        monkeypatch.setattr(MessageRepository, "upsert_message", real_upsert)
        for i in range(3):
            async with engine.begin() as conn:
                assert (await conn.execute(text("select 1"))).scalar_one() == 1
        monkeypatch.setattr(mc.asyncio, "wait_for", real)
        assert await MessageCapture(edited=False)(Handler(), mk(message_id=77), {}) == "RET"
        async with engine.connect() as conn:
            assert (await conn.execute(select(M.message_id).where(M.message_id == 77))).scalar_one() == 77
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE chat_messages RESTART IDENTITY"))
        await engine.dispose()


def test_alembic_config_percent_encoded_password():
    # регресія: %21 у паролі ламав ConfigParser-інтерполяцію в alembic/env.py
    from alembic.config import Config
    url = "postgresql+asyncpg://tgb:abc%21s@postgres:5432/db"
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    assert cfg.get_main_option("sqlalchemy.url") == url
