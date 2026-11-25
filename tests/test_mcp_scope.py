from datetime import timedelta

import pytest
from sqlalchemy import update

from bot.data import db_connection
from bot.data.models import ChatModel
from bot.domain.mcp import server
from bot.domain.mcp.scope import scope_condition
from tests.mcp_helpers import T0, call, make_client, make_key, seed_chat, seed_msg

WIN = {"since": (T0 - timedelta(days=1)).isoformat(), "until": (T0 + timedelta(days=1)).isoformat()}


@pytest.fixture(autouse=True)
def _reset():
    server._rate.clear()
    server._agg.clear()


@pytest.fixture
async def client(db):
    async with make_client() as c:
        yield c


@pytest.fixture
async def world(db):
    await seed_chat("-1", "PMP", partner_mp=True)
    await seed_chat("-2", "MMT", media_mt=True)
    await seed_chat("-3", "UNSPEC")
    await seed_msg("-1", 1, text="alpha pm")
    await seed_msg("-2", 1, text="alpha mt")
    await seed_msg("-3", 1, text="alpha un")
    await seed_msg("-999", 1, text="alpha unregistered")


def test_scope_condition_pure():
    assert str(scope_condition([])) == "false"
    assert str(scope_condition(["bogus"])) == "false"
    assert str(scope_condition("partner_mp")) == "false"
    assert str(scope_condition(["all", "bogus"])) == "true"
    assert "partner_mp" in str(scope_condition(["partner_mp", "bogus"]))
    assert "media_mp" not in str(scope_condition(["media_mt"]))


async def test_partner_mp_sees_only_own(client, world):
    token, _ = await make_key(["partner_mp"])
    err, data = await call(client, token, "list_chats")
    assert [c["chat_id"] for c in data["chats"]] == ["-1"]
    assert data["chats"][0]["categories"] == ["partner_mp"]
    err, data = await call(client, token, "get_messages", {"chat_id": "-1", "since": T0.isoformat()})
    assert [m["text"] for m in data["messages"]] == ["alpha pm"]
    err, data = await call(client, token, "search_messages", {"query": "alpha", **WIN})
    assert [m["text"] for m in data["messages"]] == ["alpha pm"]
    assert data["messages"][0]["chat_id"] == "-1"


async def test_out_of_scope_and_unknown_identical(client, world):
    token, _ = await make_key(["partner_mp"])
    e1 = await call(client, token, "get_messages", {"chat_id": "-2", "since": T0.isoformat()})
    e2 = await call(client, token, "get_messages", {"chat_id": "-12345", "since": T0.isoformat()})
    e3 = await call(client, token, "get_messages", {"chat_id": "-999", "since": T0.isoformat()})
    assert e1 == e2 == e3 == (True, "chat not found")
    err, msg = await call(client, token, "search_messages", {"query": "alpha", "chat_id": "-2"})
    assert (err, msg) == (True, "chat not found")


async def test_all_sees_registered_incl_unspecified(client, world):
    token, _ = await make_key(["all"])
    err, data = await call(client, token, "list_chats")
    assert sorted(c["chat_id"] for c in data["chats"]) == ["-1", "-2", "-3"]
    err, data = await call(client, token, "search_messages", {"query": "alpha", **WIN})
    assert sorted(m["text"] for m in data["messages"]) == ["alpha mt", "alpha pm", "alpha un"]
    assert await call(client, token, "get_messages", {"chat_id": "-999", "since": T0.isoformat()}) == (
        True, "chat not found")


@pytest.mark.parametrize("cats", [["bogus"], []])
async def test_bogus_and_empty_see_nothing(client, world, cats):
    token, _ = await make_key(cats)
    err, data = await call(client, token, "list_chats")
    assert data["chats"] == []
    err, data = await call(client, token, "search_messages", {"query": "alpha", **WIN})
    assert data["messages"] == []
    assert await call(client, token, "get_messages", {"chat_id": "-1", "since": T0.isoformat()}) == (
        True, "chat not found")


async def test_media_mt_vs_media_mp(client, db):
    await seed_chat("-5", "mp", media_mp=True)
    await seed_chat("-6", "mt", media_mt=True)
    token, _ = await make_key(["media_mt"])
    err, data = await call(client, token, "list_chats")
    assert [c["chat_id"] for c in data["chats"]] == ["-6"]


async def test_scope_is_live(client, world):
    token, _ = await make_key(["partner_mp"])
    assert (await call(client, token, "get_messages", {"chat_id": "-2", "since": T0.isoformat()}))[0] is True
    async with db_connection.async_session() as s:
        await s.execute(update(ChatModel).where(ChatModel.group_id == "-2").values(partner_mp=True))
        await s.commit()
    err, data = await call(client, token, "get_messages", {"chat_id": "-2", "since": T0.isoformat()})
    assert not err and [m["text"] for m in data["messages"]] == ["alpha mt"]
    # у чаті обидва прапорці, але ключ бачить лише свою категорію
    err, data = await call(client, token, "list_chats")
    assert all(c["categories"] == ["partner_mp"] for c in data["chats"])


async def test_pagination_equal_sent_at_no_dupes(client, db):
    await seed_chat("-1", "C", partner_mp=True)
    for i in range(1, 8):
        await seed_msg("-1", i, sent_at=T0 if i <= 5 else T0 + timedelta(seconds=1), text=f"m{i}")
    token, _ = await make_key(["partner_mp"])
    seen, cursor = [], None
    for _ in range(10):
        args = {"chat_id": "-1", "since": T0.isoformat(), "limit": 2}
        if cursor:
            args["cursor"] = cursor
        err, data = await call(client, token, "get_messages", args)
        seen += [m["message_id"] for m in data["messages"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert seen == [1, 2, 3, 4, 5, 6, 7]


async def test_byte_cap_gives_cursor(client, db):
    await seed_chat("-1", "C", partner_mp=True)
    for i in range(1, 21):
        await seed_msg("-1", i, text="я" * 4000)
    token, _ = await make_key(["partner_mp"])
    err, data = await call(client, token, "get_messages", {"chat_id": "-1", "since": T0.isoformat()})
    assert 0 < len(data["messages"]) < 20 and data["next_cursor"]
    err, data2 = await call(client, token, "get_messages",
                            {"chat_id": "-1", "since": T0.isoformat(), "cursor": data["next_cursor"]})
    assert data2["messages"][0]["message_id"] == len(data["messages"]) + 1


async def test_authors_dict_and_truncation(client, db):
    await seed_chat("-1", "C", partner_mp=True)
    await seed_msg("-1", 1, text="x" * 5000, author="7")
    token, _ = await make_key(["partner_mp"])
    err, data = await call(client, token, "get_messages", {"chat_id": "-1", "since": T0.isoformat()})
    assert len(data["messages"][0]["text"]) == 4000
    assert data["authors"] == {"7": {"username": "u7", "first_name": "N"}}
    assert data["messages"][0]["author_id"] == "7"
    assert data["messages"][0]["sent_at"].endswith("Z")


async def test_range_over_31_days_rejected(client, db):
    await seed_chat("-1", "C", partner_mp=True)
    token, _ = await make_key(["partner_mp"])
    err, msg = await call(client, token, "get_messages", {
        "chat_id": "-1", "since": T0.isoformat(), "until": (T0 + timedelta(days=32)).isoformat()})
    assert err is True and "31" in msg


async def test_like_escaping(client, db):
    await seed_chat("-1", "C", partner_mp=True)
    await seed_msg("-1", 1, text="100% sure", sent_at=T0)
    await seed_msg("-1", 2, text="100x sure", sent_at=T0)
    await seed_msg("-1", 3, text="a_b", sent_at=T0)
    await seed_msg("-1", 4, text="axb", sent_at=T0)
    await seed_msg("-1", 5, text="back\\slash", sent_at=T0)
    token, _ = await make_key(["partner_mp"])
    since = {"since": (T0 - timedelta(days=1)).isoformat(), "until": (T0 + timedelta(days=1)).isoformat()}

    async def ids(q):
        err, d = await call(client, token, "search_messages", {"query": q, **since})
        return [m["message_id"] for m in d["messages"]]

    assert await ids("0% s") == [1]
    assert await ids("a_b") == [3]
    assert await ids("k\\s") == [5]
    err, msg = await call(client, token, "search_messages", {"query": "a", **since})
    assert err is True


async def test_search_same_case_cyrillic_and_default_window(client, db):
    from datetime import datetime
    await seed_chat("-1", "C", partner_mp=True)
    await seed_msg("-1", 1, text="Привіт світ", sent_at=datetime.utcnow() - timedelta(hours=1))
    await seed_msg("-1", 2, text="Привіт старий", sent_at=datetime.utcnow() - timedelta(days=30))
    token, _ = await make_key(["partner_mp"])
    err, d = await call(client, token, "search_messages", {"query": "Привіт"})
    assert [m["message_id"] for m in d["messages"]] == [1]
