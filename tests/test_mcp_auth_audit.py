import pytest
from sqlalchemy import select, update

from bot.data import db_connection
from bot.data.models import McpAuditLogModel, McpKeyModel
from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.domain.mcp import server
from tests.mcp_helpers import T0, call, hdr, make_client, make_key, rpc, seed_chat, seed_msg


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "https://bot.example")
    server._rate.clear()
    server._agg.clear()


@pytest.fixture
async def client(db):
    async with make_client() as c:
        yield c


async def audit_rows():
    async with db_connection.async_session() as s:
        return list((await s.execute(select(McpAuditLogModel).order_by(McpAuditLogModel.id))).scalars())


RM = "https://bot.example/.well-known/oauth-protected-resource"  # MCP_PATH=/mcp → префикс ""
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}


async def test_missing_token_401_and_aggregated(client):
    for _ in range(3):
        r = await client.post("/mcp", json=INIT)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{RM}"'  # без error: токен не предъявлен
    rows = await audit_rows()
    assert len(rows) == 1
    assert (rows[0].status, rows[0].reason, rows[0].key_id, rows[0].hits) == ("denied", "no_token", None, 3)


async def test_wrong_token_401_audited(client):
    r = await client.post("/mcp", json=INIT, headers=hdr("tgba_nope"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{RM}", error="invalid_token"'
    rows = await audit_rows()
    assert rows[0].reason == "invalid_token" and rows[0].key_id is None


async def test_revoked_token_401(client):
    token, key = await make_key(["all"])
    assert await McpKeyRepository.revoke(key.id, "42") is True
    r = await client.post("/mcp", json=INIT, headers=hdr(token))
    assert r.status_code == 401
    rows = await audit_rows()
    assert rows[0].reason == "revoked" and rows[0].key_id == key.id


async def test_expired_token_401(client):
    from datetime import datetime, timedelta
    token, key = await make_key(["all"])
    async with db_connection.async_session() as s:
        await s.execute(update(McpKeyModel).where(McpKeyModel.id == key.id)
                        .values(expires_at=datetime.utcnow() - timedelta(minutes=1)))
        await s.commit()
    r = await client.post("/mcp", json=INIT, headers=hdr(token))
    assert r.status_code == 401
    assert (await audit_rows())[0].reason == "expired"


async def test_key_storage_hash_only(db):
    token, key = await McpKeyRepository.create("lbl", "42", ["all"])
    assert token.startswith("tgb_") and key.token_prefix == token[:8]
    assert len(key.token_hash) == 64 and token not in key.token_hash
    assert key.expires_at is None
    access, _ = await make_key(["all"])
    assert access.startswith("tgba_")


async def test_valid_ok_row_with_result_count(client):
    await seed_chat("-1", "C", partner_mp=True)
    await seed_msg("-1", 1)
    await seed_msg("-1", 2)
    token, key = await make_key(["partner_mp"])
    err, data = await call(client, token, "get_messages", {"chat_id": "-1", "since": T0.isoformat(), "junk": 1})
    assert not err and len(data["messages"]) == 2
    row = (await audit_rows())[-1]
    assert (row.status, row.tool_name, row.result_count, row.key_id) == ("ok", "get_messages", 2, key.id)
    assert row.arguments == {"chat_id": "-1", "since": T0.isoformat()}  # junk відкинуто
    assert (await McpKeyRepository.get(key.id)).last_used_at is not None


async def test_out_of_scope_audited_denied(client):
    await seed_chat("-2", "C", media_mt=True)
    token, _ = await make_key(["partner_mp"])
    await call(client, token, "get_messages", {"chat_id": "-2", "since": T0.isoformat()})
    row = (await audit_rows())[-1]
    assert (row.status, row.reason) == ("denied", "out_of_scope")


async def test_audit_failure_fail_closed(client, monkeypatch):
    await seed_chat("-1", "C", partner_mp=True)
    await seed_msg("-1", 1, text="SECRETDATA")
    token, _ = await make_key(["partner_mp"])

    async def none(**kw):
        return None

    monkeypatch.setattr(McpAuditRepository, "add", staticmethod(none))
    r = await rpc(client, token, "tools/call", {"name": "get_messages",
                                                "arguments": {"chat_id": "-1", "since": T0.isoformat()}})
    body = r.json()
    assert body["error"] == {"code": -32603, "message": "audit unavailable"}
    assert "SECRETDATA" not in r.text and "result" not in body
    r = await client.post("/mcp", json=INIT)
    assert r.status_code == 503 and "SECRETDATA" not in r.text


async def test_origin_403(client):
    token, _ = await make_key(["all"])
    r = await client.post("/mcp", json=INIT, headers={**hdr(token), "Origin": "http://evil"})
    assert r.status_code == 403


async def test_body_too_large_413(client):
    token, _ = await make_key(["all"])
    r = await client.post("/mcp", content=b"x" * (64 * 1024 + 1), headers=hdr(token))
    assert r.status_code == 413


async def test_get_delete_405(client):
    assert (await client.get("/mcp")).status_code == 405
    assert (await client.delete("/mcp")).status_code == 405


async def test_rate_limit_61st_denied_single_row(client):
    token, key = await make_key(["all"])
    for _ in range(60):
        assert "result" in (await rpc(client, token, "ping")).json()
    for _ in range(3):
        r = await rpc(client, token, "ping")
        assert r.json()["error"]["message"] == "rate limit exceeded"
    rl = [r for r in await audit_rows() if r.reason == "rate_limited"]
    assert len(rl) == 1 and rl[0].hits == 3 and rl[0].key_id == key.id


async def test_rate_limit_audit_fail_closed(client, monkeypatch):
    token, _ = await make_key(["all"])
    server._rate[1] = __import__("collections").deque([server._now()] * 60)

    async def none(**kw):
        return None

    monkeypatch.setattr(McpAuditRepository, "add", staticmethod(none))
    r = await rpc(client, token, "ping")
    assert r.json()["error"]["message"] == "audit unavailable"


async def test_protocol(client):
    token, _ = await make_key(["all"])
    r = await rpc(client, token, "initialize", {"protocolVersion": "2025-03-26"})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-03-26" and res["capabilities"] == {"tools": {"listChanged": False}}
    r = await rpc(client, token, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    r = await rpc(client, token, "tools/list")
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["list_chats", "get_messages", "search_messages"]
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdr(token))
    assert r.status_code == 202 and r.content == b""
    r = await client.post("/mcp", json=[INIT, INIT], headers=hdr(token))
    assert r.json()["error"]["code"] == -32600
    r = await rpc(client, token, "nope/nope")
    assert r.json()["error"]["code"] == -32601
    r = await rpc(client, token, "tools/call", {"name": "nope"})
    assert r.json()["error"]["code"] == -32602
    r = await client.post("/mcp", content=b"{bad", headers=hdr(token))
    assert r.json()["error"]["code"] == -32700
    assert (await rpc(client, token, "ping")).json()["result"] == {}


async def test_internal_error_generic(client, monkeypatch):
    token, _ = await make_key(["all"])
    from bot.domain.mcp import tools

    async def boom(cats, args):
        raise RuntimeError("SECRET LEAK")

    monkeypatch.setitem(tools.HANDLERS, "list_chats", boom)
    r = await rpc(client, token, "tools/call", {"name": "list_chats"})
    assert r.json()["error"] == {"code": -32603, "message": "internal error"}
    assert "SECRET" not in r.text
    assert (await audit_rows())[-1].reason == "internal"


async def test_client_ip_rightmost_xff(client):
    await client.post("/mcp", json=INIT, headers={"X-Forwarded-For": "6.6.6.6, 1.2.3.4"})
    assert (await audit_rows())[0].client_ip == "1.2.3.4"


async def test_audit_repo_pages(db):
    for i in range(5):
        await McpAuditRepository.add(method=f"m{i}", status="ok", key_id=7)
    await McpAuditRepository.add(method="x", status="denied", reason="no_token")
    page = await McpAuditRepository.page_by_key(7, 0, 2)
    assert [r.method for r in page] == ["m4", "m3"]
    assert [r.method for r in await McpAuditRepository.page_by_key(7, 4, 2)] == ["m0"]
    assert [r.method for r in await McpAuditRepository.page_unknown(0, 10)] == ["x"]
    assert len(await McpAuditRepository.export_rows(7)) == 5


# --- лише OAuth access-токен ---

def _body(method, params=None, i=1):
    b = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        b["params"] = params
    return b


async def test_k_path_removed(client):
    token, _ = await make_key(["all"])
    assert (await client.post(f"/mcp/k/{token}", json=INIT)).status_code == 404
    assert (await client.get(f"/mcp/k/{token}")).status_code == 404


async def test_claude_origin_allowed_evil_denied(client):
    token, _ = await make_key(["all"])
    for o in ("https://claude.ai", "https://claude.com"):
        r = await client.post("/mcp", json=INIT, headers={**hdr(token), "Origin": o})
        assert r.status_code == 200
    for o in ("https://evil.example", "https://claude.ai.evil.example", "null"):
        r = await client.post("/mcp", json=INIT, headers={**hdr(token), "Origin": o})
        assert r.status_code == 403


async def test_deleted_key_401_header(client):
    token, key = await make_key(["all"])
    assert (await client.post("/mcp", json=INIT, headers=hdr(token))).status_code == 200
    assert await McpKeyRepository.delete(key.id, "42")
    r = await client.post("/mcp", json=INIT, headers=hdr(token))
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    rows = [x for x in await audit_rows() if x.status == "denied"]
    assert rows and all(x.reason == "revoked" and x.key_id == key.id for x in rows)
