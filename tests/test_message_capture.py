import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from aiogram.types import Message

from bot.data.repositories import MessageRepository
from bot.domain.middlewares import MessageCapture as mc
from bot.domain.middlewares.MessageCapture import MessageCapture, _values

SECRET = "SECRET_TEXT_123"


def mk(chat_type="supergroup", **kw):
    d = {
        "message_id": 10,
        "date": 1700000000,
        "chat": {"id": -100123, "type": chat_type, "title": "G"},
        "from": {"id": 5, "is_bot": False, "first_name": "Al", "username": "al"},
        "text": SECRET,
    }
    d.update(kw)
    return Message.model_validate(d)


class Handler:
    def __init__(self):
        self.calls = 0

    async def __call__(self, event, data):
        self.calls += 1
        return "RET"


@pytest.fixture
def calls(monkeypatch):
    rec = []

    async def fake(conn, values, edited):
        rec.append((values, edited))

    monkeypatch.setattr(MessageRepository, "upsert_message", fake)
    return rec


async def test_group_message_written(db, calls):
    h = Handler()
    assert await MessageCapture(edited=False)(h, mk(), {}) == "RET"
    assert h.calls == 1
    assert len(calls) == 1
    v, edited = calls[0]
    assert edited is False
    assert v["group_id"] == "-100123" and v["message_id"] == 10 and v["text"] == SECRET


async def test_edited_flag_passed(db, calls):
    await MessageCapture(edited=True)(Handler(), mk(edit_date=1700000100), {})
    assert calls[0][1] is True and calls[0][0]["is_edited"] is True


async def test_private_not_written(db, calls):
    h = Handler()
    assert await MessageCapture(edited=False)(h, mk("private"), {}) == "RET"
    assert calls == [] and h.calls == 1


async def test_write_raises_handler_still_runs_no_text_logged(db, monkeypatch, caplog):
    async def boom(conn, values, edited):
        raise RuntimeError(f"db failed params {values}")

    monkeypatch.setattr(MessageRepository, "upsert_message", boom)
    h = Handler()
    with caplog.at_level(logging.DEBUG):
        assert await MessageCapture(edited=False)(h, mk(), {}) == "RET"
    assert h.calls == 1
    assert caplog.records
    assert SECRET not in caplog.text


async def test_write_timeout_handler_still_runs(db, monkeypatch, caplog):
    async def slow(conn, values, edited):
        await asyncio.sleep(5)

    monkeypatch.setattr(MessageRepository, "upsert_message", slow)
    real = asyncio.wait_for
    monkeypatch.setattr(mc.asyncio, "wait_for", lambda aw, timeout: real(aw, 0.05))
    h = Handler()
    with caplog.at_level(logging.DEBUG):
        assert await MessageCapture(edited=False)(h, mk(), {}) == "RET"
    assert h.calls == 1
    assert SECRET not in caplog.text


async def test_values_caption_and_media():
    v = _values(mk(text=None, caption="cap", photo=[{"file_id": "a", "file_unique_id": "b", "width": 1, "height": 1}]))
    assert v["text"] == "cap" and v["has_media"] is True and v["message_type"] == "photo"
    t = _values(mk())
    assert t["has_media"] is False and t["message_type"] == "text"
    assert t["is_edited"] is False and t["edit_date"] is None


def test_values_edit_date_and_dates():
    v = _values(mk(edit_date=1700000100))
    assert v["edit_date"] == datetime(2023, 11, 14, 22, 15, 0) and v["edit_date"].tzinfo is None
    assert v["is_edited"] is True
    assert v["sent_at"] == datetime(2023, 11, 14, 22, 13, 20) and v["sent_at"].tzinfo is None
    m = mk().model_copy(update={"date": datetime(2023, 11, 15, 2, 0, tzinfo=timezone(timedelta(hours=3)))})
    assert _values(m)["sent_at"] == datetime(2023, 11, 14, 23, 0)


def test_values_sender_chat_and_reply():
    v = _values(mk(sender_chat={"id": -777, "type": "channel", "title": "Chan", "username": "chn"},
                   reply_to_message={"message_id": 3, "date": 1700000000,
                                     "chat": {"id": -100123, "type": "supergroup"}}))
    assert v["author_id"] == "-777" and v["author_username"] == "chn" and v["author_first_name"] == "Chan"
    assert v["reply_to_message_id"] == 3
    u = _values(mk())
    assert u["author_id"] == "5" and u["reply_to_message_id"] is None
