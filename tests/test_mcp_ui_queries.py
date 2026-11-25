from datetime import datetime, timedelta

from sqlalchemy import insert

from bot.data.models import ChatMessageModel, ChatModel
from bot.data.repositories.MessageRepository import MessageStats

NOW = datetime(2026, 11, 25, 12, 0, 0)


async def _msgs(engine, rows):
    async with engine.begin() as conn:
        await conn.execute(insert(ChatMessageModel), [
            {"group_id": g, "message_id": i, "message_type": "text", "sent_at": t}
            for i, (g, t) in enumerate(rows, 1)
        ])


async def _chats(engine, *groups):
    async with engine.begin() as conn:
        for g in groups:
            await conn.execute(insert(ChatModel).values(group_id=g, title=f"t{g}"))


async def test_group_activity(db):
    await _msgs(db, [("a", NOW - timedelta(hours=1)), ("a", NOW - timedelta(hours=30)),
                     ("a", NOW - timedelta(days=5)), ("b", NOW)])
    cnt, last = await MessageStats.group_activity("a", NOW - timedelta(hours=24))
    assert cnt == 1 and last == NOW - timedelta(hours=1)
    assert await MessageStats.group_activity("zzz", NOW - timedelta(hours=24)) == (0, None)


async def test_count_since(db):
    await _msgs(db, [("a", NOW - timedelta(hours=1)), ("b", NOW - timedelta(hours=23)),
                     ("b", NOW - timedelta(hours=25))])
    assert await MessageStats.count_since(NOW - timedelta(hours=24)) == 2


async def test_silent_groups(db):
    await _chats(db, "a", "b", "c")
    await _msgs(db, [("a", NOW - timedelta(days=1)), ("b", NOW - timedelta(days=10)),
                     ("unregistered", NOW)])
    silent = await MessageStats.silent_groups(NOW - timedelta(days=3))
    assert [g for g, _ in silent] == ["b", "c"]
