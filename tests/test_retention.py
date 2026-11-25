from datetime import datetime, timedelta

from sqlalchemy import insert, select, func

from bot.data.models import ChatMessageModel, ChatModel, McpAuditLogModel
from bot.data.repositories import MessageRepository

NOW = datetime(2026, 11, 25, 12, 0, 0)
OLD = NOW - timedelta(days=200)
NEW = NOW - timedelta(days=10)


async def seed(engine, group, count, sent_at, start=1, step=timedelta(seconds=1), same_time=False):
    rows = [
        {
            "group_id": group, "message_id": start + i, "message_type": "text",
            "sent_at": sent_at if same_time else sent_at + step * i,
        }
        for i in range(count)
    ]
    async with engine.begin() as conn:
        await conn.execute(insert(ChatMessageModel), rows)


async def register(engine, *groups):
    async with engine.begin() as conn:
        for g in groups:
            await conn.execute(insert(ChatModel).values(group_id=g, title=g))


async def count(engine, group):
    async with engine.connect() as conn:
        return (await conn.execute(
            select(func.count()).select_from(ChatMessageModel).where(ChatMessageModel.group_id == group)
        )).scalar_one()


async def run(keep=100, days=180):
    return await MessageRepository.delete_old_messages(days, keep, now=NOW)


async def test_50_old_kept(db):
    await register(db, "g")
    await seed(db, "g", 50, OLD)
    await run()
    assert await count(db, "g") == 50


async def test_exactly_100_old_kept(db):
    await register(db, "g")
    await seed(db, "g", 100, OLD)
    await run()
    assert await count(db, "g") == 100


async def test_101_old_leaves_100_newest(db):
    await register(db, "g")
    await seed(db, "g", 101, OLD)
    await run()
    assert await count(db, "g") == 100
    async with db.connect() as conn:
        ids = (await conn.execute(select(ChatMessageModel.message_id).where(ChatMessageModel.group_id == "g"))).scalars().all()
    assert 1 not in ids and 101 in ids


async def test_150_old_leaves_100(db):
    await register(db, "g")
    await seed(db, "g", 150, OLD)
    deleted = await run()
    assert deleted == 50
    assert await count(db, "g") == 100


async def test_mixed_new_and_old(db):
    await register(db, "g")
    await seed(db, "g", 150, OLD)
    await seed(db, "g", 30, NEW, start=1000)
    await run()
    # 30 нових + 70 найновіших зі старих = 100
    assert await count(db, "g") == 100
    async with db.connect() as conn:
        new_left = (await conn.execute(
            select(func.count()).select_from(ChatMessageModel).where(ChatMessageModel.sent_at >= NEW)
        )).scalar_one()
    assert new_left == 30


async def test_mixed_many_new_old_all_deleted(db):
    await register(db, "g")
    await seed(db, "g", 60, OLD)
    await seed(db, "g", 120, NEW, start=1000)
    await run()
    assert await count(db, "g") == 120


async def test_unregistered_group_plain_cutoff(db):
    await seed(db, "x", 150, OLD)
    await seed(db, "x", 5, NEW, start=1000)
    await run()
    assert await count(db, "x") == 5


async def test_sent_at_ties_exactly_keep(db):
    await register(db, "g")
    await seed(db, "g", 130, OLD, same_time=True)
    await run()
    assert await count(db, "g") == 100
    async with db.connect() as conn:
        ids = (await conn.execute(select(ChatMessageModel.message_id).where(ChatMessageModel.group_id == "g"))).scalars().all()
    assert min(ids) == 31 and max(ids) == 130  # лишаються найбільші id


async def test_keep_last_zero_disables_protection(db):
    await register(db, "g")
    await seed(db, "g", 150, OLD)
    await run(keep=0)
    assert await count(db, "g") == 0


async def test_keep_last_negative_disables_protection(db):
    await register(db, "g")
    await seed(db, "g", 10, OLD)
    await run(keep=-5)
    assert await count(db, "g") == 0


async def test_retention_disabled(db):
    await seed(db, "x", 10, OLD)
    assert await run(days=0) == 0
    assert await count(db, "x") == 10


async def test_groups_independent(db):
    await register(db, "a", "b")
    await seed(db, "a", 50, OLD)
    await seed(db, "b", 150, OLD)
    await run()
    assert await count(db, "a") == 50
    assert await count(db, "b") == 100


async def test_audit_cleanup(db):
    async with db.begin() as conn:
        await conn.execute(insert(McpAuditLogModel), [
            {"created_at": NOW - timedelta(days=400), "method": "ping", "status": "ok"},
            {"created_at": NOW - timedelta(days=364), "method": "ping", "status": "ok"},
            {"created_at": NOW - timedelta(days=1), "method": "ping", "status": "ok"},
        ])
    deleted = await MessageRepository.delete_old_audit(365, now=NOW)
    assert deleted == 1
    async with db.connect() as conn:
        left = (await conn.execute(select(func.count()).select_from(McpAuditLogModel))).scalar_one()
    assert left == 2
