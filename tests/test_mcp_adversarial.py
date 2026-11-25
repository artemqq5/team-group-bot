import json
import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from bot.data import db_connection
from bot.data.models import McpAuditLogModel, McpKeyModel
from bot.domain.mcp import server, tools
from tests.mcp_helpers import T0, call, hdr, make_client, make_key, rpc, seed_chat, seed_msg


@pytest.fixture(autouse=True)
def _reset():
    server._rate.clear()
    server._agg.clear()


@pytest.fixture
async def client(db):
    async with make_client() as c:
        yield c


async def audit_rows():
    async with db_connection.async_session() as s:
        return list((await s.execute(select(McpAuditLogModel).order_by(McpAuditLogModel.id))).scalars())


@pytest.fixture
async def world(db):
    await seed_chat("-1", partner_mp=True)
    await seed_chat("-2", media_mt=True)
    await seed_chat("-3")  # unspecified
    await seed_chat("-4", partner_mp=True, media_mt=True)
    for g in ("-1", "-2", "-3", "-4", "-999"):  # -999 unregistered
        await seed_msg(g, 1, text="secret needle " + g, author="a" + g)
    return None


def ids_of(payload):
    return {m.get("chat_id") for m in payload["messages"]}


# ---------------- scope leaks ----------------
async def test_chat_id_int_vs_str_scope(client, world):
    token, _ = await make_key(["partner_mp"])
    err, p = await call(client, token, "get_messages", {"chat_id": -1, "since": "2025-01-01"})
    assert not err and len(p["messages"]) == 1
    err, p = await call(client, token, "get_messages", {"chat_id": -2, "since": "2025-01-01"})
    assert err and p == "chat not found"


@pytest.mark.parametrize("cid", [" -1", "-1 ", "-1' OR '1'='1", "-1; DROP TABLE chats", "%", "-%", "-", "", "-2", "-999",
                                 "-3", "-1\x00", "-01", "-1.0"])
async def test_chat_id_variants_do_not_leak(client, world, cid):
    token, _ = await make_key(["partner_mp"])
    err, p = await call(client, token, "get_messages", {"chat_id": cid, "since": "2025-01-01"})
    if cid == "-1":
        return
    assert err and p == "chat not found", (cid, p)
    err, p = await call(client, token, "search_messages", {"query": "needle", "chat_id": cid})
    assert err and p == "chat not found", (cid, p)


async def test_search_across_scope_only_in_scope(client, world):
    token, _ = await make_key(["partner_mp"])
    err, p = await call(client, token, "search_messages", {"query": "needle", "since": "2025-01-01", "until": "2025-02-01"})
    assert not err
    assert ids_of(p) == {"-1", "-4"}
    assert set(p["authors"]) == {"a-1", "a-4"}


async def test_all_includes_unspecified_but_never_unregistered(client, world):
    token, _ = await make_key(["all"])
    err, p = await call(client, token, "search_messages", {"query": "needle", "since": "2025-01-01", "until": "2025-02-01"})
    assert ids_of(p) == {"-1", "-2", "-3", "-4"}
    assert "a-999" not in p["authors"]
    err, p = await call(client, token, "get_messages", {"chat_id": "-999", "since": "2025-01-01"})
    assert err and p == "chat not found"
    err, p = await call(client, token, "list_chats")
    assert {c["chat_id"] for c in p["chats"]} == {"-1", "-2", "-3", "-4"}


async def test_all_flags_false_chat_hidden_from_non_all(client, world):
    token, _ = await make_key(["partner_mp", "media_mt"])
    err, p = await call(client, token, "list_chats")
    assert {c["chat_id"] for c in p["chats"]} == {"-1", "-2", "-4"}


@pytest.mark.parametrize("cats,expect", [
    (["all", "bogus"], {"-1", "-2", "-3", "-4"}),
    (["bogus", "partner_mp"], {"-1", "-4"}),
    (["bogus", 5, None, ["all"], {"a": 1}], set()),
    ("all", set()), ({"all": True}, set()), (None, set()), (5, set()), ([], set()), (["ALL"], set()),
    (["unspecified"], set()), ([" all"], set()), (["id"], set()), (["time"], set()),
])
async def test_weird_categories_json(client, world, cats, expect):
    token, key = await make_key(["all"])
    async with db_connection.async_session() as s:
        await s.execute(update(McpKeyModel).where(McpKeyModel.id == key.id).values(categories=cats))
        await s.commit()
    err, p = await call(client, token, "list_chats")
    assert not err
    assert {c["chat_id"] for c in p["chats"]} == expect
    err, p = await call(client, token, "search_messages", {"query": "needle", "since": "2025-01-01", "until": "2025-02-01"})
    assert not err and ids_of(p) == expect


async def test_list_chats_does_not_reveal_other_categories(client, world):
    token, _ = await make_key(["partner_mp"])
    err, p = await call(client, token, "list_chats")
    by = {c["chat_id"]: c["categories"] for c in p["chats"]}
    assert by["-4"] == ["partner_mp"]


@pytest.mark.parametrize("cursor", ["garbage", "|", "2025-01-10T12:00:00|abc", "2025-01-10T12:00:00Z|1",
                                    "2025-01-10T12:00:00+00:00|1", "|5", "x|1", "2025-01-10T12:00:00|1|2",
                                    "2025-01-10T12:00:00|99999999999999999999999", "2025-01-10T12:00:00|-1", 123, {"a": 1}, [],
                                    "2025-01-10T12:00:00 |1", "2025-01-10T12:00:00|1 "])
async def test_malformed_cursor_no_500_no_leak(client, world, cursor):
    token, _ = await make_key(["partner_mp"])
    r = await rpc(client, token, "tools/call", {"name": "get_messages",
                  "arguments": {"chat_id": "-1", "since": "2025-01-01", "cursor": cursor}})
    assert r.status_code == 200
    res = r.json()
    if "error" in res:
        pytest.fail(f"internal error for cursor {cursor!r}: {res}")
    body = res["result"]
    if not body["isError"]:
        for m in json.loads(body["content"][0]["text"])["messages"]:
            assert "-2" not in m["text"] and "-3" not in m["text"]


async def test_cursor_from_other_chat_cannot_cross(client, world):
    token, _ = await make_key(["partner_mp"])
    # cursor id belonging to a row of chat -2 must not surface it
    err, p = await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01", "cursor": "2024-01-01T00:00:00|2"})
    assert not err and {m["text"] for m in p["messages"]} == {"secret needle -1"}


# ---------------- auth ----------------
async def test_token_case_whitespace(client):
    token, _ = await make_key(["all"])
    init = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert (await client.post("/mcp", json=init, headers={"Authorization": f"Bearer {token.upper()}"})).status_code == 401
    assert (await client.post("/mcp", json=init, headers={"Authorization": f"Bearer {token}x"})).status_code == 401
    assert (await client.post("/mcp", json=init, headers={"Authorization": f"Bearer  {token} "})).status_code == 200
    assert (await client.post("/mcp", json=init, headers={"Authorization": f"bearer {token}"})).status_code == 200
    assert (await client.post("/mcp", json=init, headers={"Authorization": token})).status_code == 401
    assert (await client.post("/mcp", json=init, headers={"Authorization": f"Basic {token}"})).status_code == 401
    assert (await client.post("/mcp", json=init, headers={"Authorization": f"Bearer {token[:8]}"})).status_code == 401
    assert (await client.post("/mcp", json=init, headers={"Authorization": "Bearer "})).status_code == 401
    assert (await client.post("/mcp", json=init, headers={"Authorization": "Bearer"})).status_code == 401


async def test_two_authorization_headers_not_bypass(client):
    token, _ = await make_key(["all"])
    init = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = await client.post("/mcp", json=init, headers=[("Authorization", "Bearer bad"), ("Authorization", f"Bearer {token}")])
    assert r.status_code == 401


async def test_revoked_prefix_and_valid_other_key(client):
    t1, k1 = await make_key(["all"])
    t2, k2 = await make_key(["all"])
    from bot.data.repositories.McpKeyRepository import McpKeyRepository
    await McpKeyRepository.revoke(k1.id, "1")
    init = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert (await client.post("/mcp", json=init, headers=hdr(t1))).status_code == 401
    assert (await client.post("/mcp", json=init, headers=hdr(t1[:8] + t2[8:]))).status_code == 401
    assert (await client.post("/mcp", json=init, headers=hdr(t2))).status_code == 200


async def test_plaintext_never_stored_or_logged(client, caplog, db):
    caplog.set_level(logging.INFO)
    token, key = await make_key(["all"])
    await seed_chat("-1", partner_mp=True)
    await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01"})
    await client.post("/mcp", content=b"{bad", headers=hdr(token))
    await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=hdr(token + "z"))
    assert token not in caplog.text
    async with db_connection.async_session() as s:
        for tbl in ("mcp_keys", "mcp_audit_log"):
            for row in (await s.execute(text(f"select * from {tbl}"))).all():
                assert token not in json.dumps([str(c) for c in row]), tbl
    rows = await audit_rows()
    assert all(token + "z" not in json.dumps(r.arguments) for r in rows)


async def test_token_in_args_is_truncated_in_audit_not_whitelist_bypass(client, db):
    token, _ = await make_key(["all"])
    await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01", "evil": "x", "Authorization": "y",
                                                 "cursor": "c" * 5000})
    row = (await audit_rows())[-1]
    assert set(row.arguments) <= set(server.SAFE_ARGS)
    assert all(len(str(v)) <= 200 for v in row.arguments.values())


# ---------------- audit ----------------
async def test_every_call_writes_row(client, world):
    token, key = await make_key(["partner_mp"])
    n = 0
    for name, args in [("list_chats", {}), ("get_messages", {"chat_id": "-1", "since": "2025-01-01"}),
                       ("search_messages", {"query": "needle"}), ("get_messages", {"chat_id": "-2", "since": "2025-01-01"}),
                       ("get_messages", {"chat_id": "-1", "since": "garbage"}), ("nope", {})]:
        await rpc(client, token, "tools/call", {"name": name, "arguments": args})
        n += 1
        rows = [r for r in await audit_rows() if r.key_id == key.id]
        assert len(rows) == n, name
        assert rows[-1].tool_name == name
    rows = await audit_rows()
    assert rows[0].result_count == 2  # list_chats -1,-4
    assert rows[3].status == "denied" and rows[3].reason == "out_of_scope"
    assert rows[4].status == "error" and rows[4].reason == "bad_args"


async def test_audit_args_never_contain_unexpected(client, world):
    token, _ = await make_key(["all"])
    await call(client, token, "search_messages", {"query": "needle", "password": "p", "x": {"y": 1}})
    row = (await audit_rows())[-1]
    assert row.arguments == {"query": "needle"}


async def test_fail_closed_returns_no_data(client, world, monkeypatch):
    token, _ = await make_key(["all"])
    from bot.data.repositories.McpAuditRepository import McpAuditRepository

    async def broken(*a, **k):
        return None
    monkeypatch.setattr(McpAuditRepository, "add", broken)
    r = await rpc(client, token, "tools/call", {"name": "search_messages", "arguments": {"query": "needle"}})
    assert "needle" not in r.text and "error" in r.json()
    r = await rpc(client, token, "tools/list")
    assert "error" in r.json()
    r = await rpc(client, token, "initialize", {})
    assert "error" in r.json()
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=hdr("bad"))
    assert r.status_code == 503
    r = await client.post("/mcp", content=b"{", headers=hdr(token))
    assert r.json().get("error", {}).get("code") in (-32700, -32603)


async def test_notification_audit_fail_closed(client, monkeypatch):
    token, _ = await make_key(["all"])
    from bot.data.repositories.McpAuditRepository import McpAuditRepository

    async def broken(*a, **k):
        return None
    monkeypatch.setattr(McpAuditRepository, "add", broken)
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdr(token))
    assert r.status_code != 202


async def test_aggregation_200_bad_requests_one_row_per_bucket(client, monkeypatch):
    monkeypatch.setattr(server, "_now", lambda: 1_000_000.0)
    init = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for i in range(200):
        r = await client.post("/mcp", json=init, headers=hdr(f"tgb_bad{i}"))
        assert r.status_code == 401
    for i in range(50):
        await client.post("/mcp", json=init)
    rows = await audit_rows()
    assert len(rows) == 2
    assert sorted(r.hits for r in rows) == [50, 200]


async def test_aggregation_revoked_key_one_row(client, monkeypatch):
    monkeypatch.setattr(server, "_now", lambda: 1_000_000.0)
    token, key = await make_key(["all"])
    from bot.data.repositories.McpKeyRepository import McpKeyRepository
    await McpKeyRepository.revoke(key.id, "1")
    for _ in range(100):
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=hdr(token))
    rows = await audit_rows()
    assert len(rows) == 1 and rows[0].hits == 100


async def test_xff_spoof_cannot_multiply_rows_for_same_invalid_token_rows(client, monkeypatch):
    """Rightmost XFF is proxy-appended; leftmost spoof must not create new rows."""
    monkeypatch.setattr(server, "_now", lambda: 1_000_000.0)
    for i in range(50):
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          headers={"X-Forwarded-For": f"10.0.0.{i}, 1.2.3.4", "Authorization": "Bearer nope"})
    assert len(await audit_rows()) == 1


async def test_agg_map_eviction(client, monkeypatch):
    t = [1_000_000.0]
    monkeypatch.setattr(server, "_now", lambda: t[0])
    for i in range(30):
        await client.post("/mcp", json={}, headers={"X-Forwarded-For": f"1.1.1.{i}"})
    assert len(server._agg) == 30
    t[0] += 60 * 10
    await client.post("/mcp", json={})
    assert len(server._agg) == 1
    assert all(k[2] >= int(t[0] // 60) - 1 for k in server._agg)


# ---------------- protocol ----------------
async def raw(client, token, body, **kw):
    return await client.post("/mcp", content=body, headers={**hdr(token), "Content-Type": "application/json"}, **kw)


async def test_protocol_edges(client, world):
    token, _ = await make_key(["all"])
    r = await raw(client, token, b"{not json")
    assert r.json()["error"]["code"] == -32700 and r.json()["id"] is None
    r = await raw(client, token, b"\xff\xfe")
    assert r.json()["error"]["code"] == -32700
    r = await raw(client, token, b"[]")
    assert r.json()["error"]["code"] == -32600
    r = await raw(client, token, b'"str"')
    assert r.json()["error"]["code"] == -32600
    r = await raw(client, token, b'{"id":5,"method":"ping"}')
    assert r.json()["error"]["code"] == -32600 and r.json()["id"] == 5
    r = await raw(client, token, b'{"jsonrpc":"1.0","id":5,"method":"ping"}')
    assert r.json()["error"]["code"] == -32600
    r = await raw(client, token, b'{"jsonrpc":"2.0","id":5,"method":7}')
    assert r.json()["error"]["code"] == -32600
    r = await raw(client, token, b'{"jsonrpc":"2.0","id":null,"method":"ping"}')
    assert r.status_code == 200 and r.json()["result"] == {} and r.json()["id"] is None
    r = await raw(client, token, b'{"jsonrpc":"2.0","method":"notifications/initialized"}')
    assert r.status_code == 202 and r.content == b""
    r = await raw(client, token, b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"list_chats"}}')
    assert r.status_code == 202 and r.content == b""
    r = await raw(client, token, b'{"jsonrpc":"2.0","id":1,"method":"nope"}')
    assert r.json()["error"]["code"] == -32601
    for pv in ("1999-01-01", 5, None, ["2025-06-18"], {"a": 1}):
        r = await rpc(client, token, "initialize", {"protocolVersion": pv})
        assert r.json()["result"]["protocolVersion"] == server.SUPPORTED_VERSIONS[0], pv
    r = await rpc(client, token, "initialize", {"protocolVersion": "2025-03-26"})
    assert r.json()["result"]["protocolVersion"] == "2025-03-26"
    r = await raw(client, token, b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":[1]}')
    assert r.status_code == 200 and "result" in r.json()


@pytest.mark.parametrize("params", [{"name": "nope"}, {"name": None}, {}, {"name": 5}, {"name": ["x"]},
                                    {"name": "list_chats", "arguments": "str"}, {"name": "list_chats", "arguments": [1]},
                                    {"name": "__class__"}, {"name": "get_messages", "arguments": 5}])
async def test_tools_call_bad_name_or_args_type(client, world, params):
    token, _ = await make_key(["all"])
    r = await rpc(client, token, "tools/call", params)
    assert r.status_code == 200
    body = r.json()
    assert body.get("error", {}).get("code") == -32602, body


@pytest.mark.parametrize("name,args", [
    ("get_messages", {"chat_id": "-1", "since": "garbage"}),
    ("get_messages", {"chat_id": "-1", "since": 123}),
    ("get_messages", {"chat_id": "-1", "since": None}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": "abc"}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": 0}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": -5}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": 1.5}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": True}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": None}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "until": "x"}),
    ("get_messages", {"chat_id": "-1", "since": "2025-01-01", "until": 5}),
    ("get_messages", {"chat_id": {"a": 1}, "since": "2025-01-01"}),
    ("get_messages", {"chat_id": True, "since": "2025-01-01"}),
    ("get_messages", {"chat_id": [], "since": "2025-01-01"}),
    ("get_messages", {"since": "2025-01-01"}),
    ("get_messages", {"chat_id": "-1"}),
    ("get_messages", {"chat_id": "-1", "since": "2025-02-01", "until": "2025-01-01"}),
    ("search_messages", {"query": 5}), ("search_messages", {"query": "a"}), ("search_messages", {}),
    ("search_messages", {"query": "ab", "limit": "abc"}),
    ("search_messages", {"query": "ab", "since": "garbage"}),
    ("search_messages", {"query": "ab", "chat_id": {"x": 1}}),
    ("search_messages", {"query": "ab", "since": "2020-01-01", "until": "2025-01-01"}),
])
async def test_bad_args_iserror_not_500(client, world, name, args):
    token, _ = await make_key(["all"])
    r = await rpc(client, token, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200
    body = r.json()
    assert "error" not in body, (args, body)
    assert body["result"]["isError"] is True, (args, body)
    row = (await audit_rows())[-1]
    assert row.reason == "bad_args", (args, row.reason)


@pytest.mark.parametrize("args", [
    {"chat_id": "-1", "since": "9999-12-31T00:00:00"},
    {"chat_id": "-1", "since": "0001-01-01T00:00:00+05:00"},
    {"chat_id": "-1", "since": "9999-12-31T23:59:59-05:00", "until": "9999-12-31T23:59:59"},
])
async def test_datetime_extremes_are_bad_args(client, world, args):
    token, _ = await make_key(["all"])
    r = await rpc(client, token, "tools/call", {"name": "get_messages", "arguments": args})
    body = r.json()
    assert "error" not in body, body  # internal -32603 would be a defect
    assert body["result"]["isError"] is True


# ---------------- limits ----------------
async def test_limit_clamped(client, db):
    await seed_chat("-1", partner_mp=True)
    for i in range(1, 130):
        await seed_msg("-1", i, sent_at=T0 + timedelta(seconds=i))
    token, _ = await make_key(["all"])
    err, p = await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01", "limit": 10**9})
    assert not err and len(p["messages"]) == 100 and p["next_cursor"]
    err, p = await call(client, token, "search_messages", {"query": "hello", "since": "2024-12-01", "until": "2025-02-01", "limit": 10**9})
    assert len(p["messages"]) == 50


async def test_range_boundaries(client, db):
    await seed_chat("-1", partner_mp=True)
    token, _ = await make_key(["all"])
    ok, _p = await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01", "until": "2025-02-01"})
    assert not ok
    bad, _p = await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01", "until": "2025-02-01T00:00:01"})
    assert bad
    bad, _p = await call(client, token, "search_messages", {"query": "hello", "since": "2024-01-01", "until": "2024-07-01"})
    assert bad  # >180 days
    ok, _p = await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01T00:00:00+02:00", "until": "2025-01-02T00:00:00Z"})
    assert not ok


async def test_40kb_cap_final_response_size(client, db):
    await seed_chat("-1", partner_mp=True)
    for i in range(1, 40):
        await seed_msg("-1", i, sent_at=T0 + timedelta(seconds=i), text='"\n\\' * 1300)  # escape-heavy, 3900 chars
    token, _ = await make_key(["all"])
    r = await rpc(client, token, "tools/call", {"name": "get_messages", "arguments": {"chat_id": "-1", "since": "2025-01-01"}})
    inner = json.loads(r.json()["result"]["content"][0]["text"])
    assert inner["next_cursor"]
    inner_bytes = len(r.json()["result"]["content"][0]["text"].encode())
    assert inner_bytes <= tools.MAX_BYTES, inner_bytes
    assert len(r.content) <= tools.MAX_BYTES * 2  # outer escaping tolerated


async def test_text_truncation_4000(client, db):
    await seed_chat("-1", partner_mp=True)
    await seed_msg("-1", 1, text="я" * 5000)
    await seed_msg("-1", 2, text="x" * 4000, sent_at=T0 + timedelta(seconds=1))
    await seed_msg("-1", 3, text=None, sent_at=T0 + timedelta(seconds=2))
    token, _ = await make_key(["all"])
    err, p = await call(client, token, "get_messages", {"chat_id": "-1", "since": "2025-01-01"})
    assert [len(m["text"]) if m["text"] else None for m in p["messages"]] == [4000, 4000, None]


async def test_search_truncated_by_bytes_signals_more(client, db):
    await seed_chat("-1", partner_mp=True)
    for i in range(1, 20):
        await seed_msg("-1", i, sent_at=T0 + timedelta(seconds=i), text="needle " + "z" * 3900)
    token, _ = await make_key(["all"])
    err, p = await call(client, token, "search_messages", {"query": "needle", "since": "2025-01-01", "until": "2025-02-01"})
    assert not err and 0 < len(p["messages"]) < 19
    assert p["truncated"] is True
    assert "next_cursor" not in p


async def test_search_fits_not_truncated(client, db):
    await seed_chat("-1", partner_mp=True)
    for i in range(1, 4):
        await seed_msg("-1", i, sent_at=T0 + timedelta(seconds=i), text="needle short")
    token, _ = await make_key(["all"])
    err, p = await call(client, token, "search_messages", {"query": "needle", "since": "2025-01-01", "until": "2025-02-01"})
    assert not err and len(p["messages"]) == 3
    assert p["truncated"] is False
