"""Adversarial suite для вбудованого OAuth 2.1 сервера MCP (docs: .claude/plans/mcp-oauth.md, розділ 5)."""
import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, update

from bot.data import db_connection
from bot.data.models import McpAuditLogModel, McpKeyModel, McpOAuthTokenModel
from bot.data.repositories.McpAuditRepository import McpAuditRepository
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.data.repositories.McpOAuthRepository import McpOAuthRepository
from bot.domain.mcp import oauth, server
from tests.mcp_helpers import T0, call, hdr, make_client, seed_chat, seed_msg

BASE = "https://bot.example"
REDIR = "https://claude.ai/api/mcp/auth_callback"
REDIR2 = "https://claude.com/api/mcp/auth_callback"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    server._rate.clear()
    server._agg.clear()
    oauth._reset()
    monkeypatch.setenv("WEBHOOK_BASE_URL", BASE)


@pytest.fixture
async def client(db):
    async with make_client() as c:
        yield c


# ─── хелпери ──────────────────────────────────────────────────────────────────

def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, oauth._s256(verifier)


async def register(c, uris=(REDIR,), name=None):
    body = {"redirect_uris": list(uris)}
    if name is not None:
        body["client_name"] = name
    r = await c.post("/oauth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def auth_params(cid, challenge, **over):
    p = {"response_type": "code", "client_id": cid, "redirect_uri": REDIR, "code_challenge": challenge,
         "code_challenge_method": "S256", "state": "st8", "scope": "whatever"}
    p.update(over)
    return {k: v for k, v in p.items() if v is not None}


async def start(c, cid, challenge, **over):
    """GET /oauth/authorize → (tx, cookie)."""
    r = await c.get("/oauth/authorize", params=auth_params(cid, challenge, **over))
    assert r.status_code == 200, r.text
    tx = re.search(r'name="tx" value="([^"]+)"', r.text).group(1)
    cookie = re.search(r"tgb_oauth_tx=([^;]+)", r.headers["set-cookie"]).group(1)
    return tx, cookie


async def signin(c, tx, cookie, key, origin=None):
    headers = dict(FORM)
    if cookie is not None:
        headers["Cookie"] = f"tgb_oauth_tx={cookie}"
    if origin is not None:
        headers["Origin"] = origin
    return await c.post("/oauth/authorize", data={"tx": tx, "key": key}, headers=headers)


def code_of(r):
    assert r.status_code == 303, r.text
    return parse_qs(urlsplit(r.headers["location"]).query)["code"][0]


async def get_code(c, cid, challenge, key):
    tx, cookie = await start(c, cid, challenge)
    return code_of(await signin(c, tx, cookie, key))


async def exchange(c, code, cid, verifier, redirect_uri=REDIR, **extra):
    data = {"grant_type": "authorization_code", "code": code, "client_id": cid, "redirect_uri": redirect_uri,
            "code_verifier": verifier, **extra}
    return await c.post("/oauth/token", data=data, headers=FORM)


async def refresh(c, token, cid):
    return await c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token,
                                              "client_id": cid}, headers=FORM)


async def new_key(cats=("all",)):
    secret, key = await McpKeyRepository.create("lbl", "42", list(cats))
    return secret, key


async def connect(c, cats=("all",)):
    """Повний цикл → dict(secret, key, cid, tokens)."""
    secret, key = await new_key(cats)
    cid = await register(c)
    verifier, challenge = pkce()
    code = await get_code(c, cid, challenge, secret)
    r = await exchange(c, code, cid, verifier)
    assert r.status_code == 200, r.text
    return {"secret": secret, "key": key, "cid": cid, "tokens": r.json(), "code": code, "verifier": verifier}


async def audit_rows():
    async with db_connection.async_session() as s:
        return list((await s.execute(select(McpAuditLogModel).order_by(McpAuditLogModel.id))).scalars())


async def mcp_status(c, token):
    return (await c.post("/mcp", json=INIT, headers=hdr(token))).status_code


# ─── discovery ────────────────────────────────────────────────────────────────

async def test_metadata_default(client):
    r = await client.get("/.well-known/oauth-protected-resource")
    assert r.json() == {"resource": BASE + "/mcp", "authorization_servers": [BASE],
                        "bearer_methods_supported": ["header"], "scopes_supported": ["mcp"]}
    assert (await client.get("/.well-known/oauth-protected-resource/mcp")).json() == r.json()


async def test_metadata_with_prefixed_mcp_path_ignores_host(client, monkeypatch):
    monkeypatch.setattr(server, "MCP_PATH", "/team-group-bot/mcp")
    monkeypatch.setenv("WEBHOOK_BASE_URL", "https://bot.example/")
    h = {"Host": "evil.example", "X-Forwarded-Host": "evil.example"}
    prm = (await client.get("/.well-known/oauth-protected-resource", headers=h)).json()
    assert prm["resource"] == "https://bot.example/team-group-bot/mcp"
    assert prm["authorization_servers"] == ["https://bot.example/team-group-bot"]
    asm = (await client.get("/.well-known/oauth-authorization-server", headers=h)).json()
    assert asm == (await client.get("/.well-known/openid-configuration", headers=h)).json()
    iss = "https://bot.example/team-group-bot"
    assert asm["issuer"] == iss and asm["authorization_endpoint"] == iss + "/oauth/authorize"
    assert asm["token_endpoint"] == iss + "/oauth/token" and asm["registration_endpoint"] == iss + "/oauth/register"
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]
    assert asm["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert "evil" not in json.dumps(asm) + json.dumps(prm)
    r = await client.post("/mcp", json=INIT)  # відповідь 401 теж несе префіксований resource_metadata
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{iss}/.well-known/oauth-protected-resource"'


# ─── DCR ──────────────────────────────────────────────────────────────────────

async def test_register_ok(client):
    r = await client.post("/oauth/register", json={"redirect_uris": [REDIR, REDIR2], "client_name": "Claude"})
    assert r.status_code == 201
    b = r.json()
    assert b["client_id"].startswith("mcpc_") and b["token_endpoint_auth_method"] == "none"
    assert b["redirect_uris"] == [REDIR, REDIR2] and b["grant_types"] == ["authorization_code", "refresh_token"]
    assert r.headers["cache-control"] == "no-store"
    assert (await audit_rows())[-1].method == "oauth_register"


@pytest.mark.parametrize("uris", [
    ["https://evil.example/cb"], ["https://claude.ai.evil.com/api/mcp/auth_callback"],
    ["https://CLAUDE.AI/api/mcp/auth_callback"], [REDIR + "/"], [REDIR + "?x=1"], [REDIR, "https://evil.example"],
    [], "https://claude.ai/api/mcp/auth_callback", [None], [1], None,
])
async def test_register_bad_redirect(client, uris):
    r = await client.post("/oauth/register", json={"redirect_uris": uris})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


async def test_register_bad_json_and_oversize(client):
    for content in (b"{bad", b"[]", b'"x"'):
        r = await client.post("/oauth/register", content=content)
        assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata"
    r = await client.post("/oauth/register", json={"redirect_uris": [REDIR], "client_name": 5})
    assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata"
    r = await client.post("/oauth/register", content=b"x" * (8 * 1024 + 1))
    assert r.status_code == 400


async def test_register_throttle_30_per_hour(client):
    for _ in range(30):
        assert (await client.post("/oauth/register", json={"redirect_uris": [REDIR]})).status_code == 201
    assert (await client.post("/oauth/register", json={"redirect_uris": [REDIR]})).status_code == 429


# ─── authorize GET ────────────────────────────────────────────────────────────

async def test_authorize_page_headers_and_cookie(client):
    cid = await register(client, name="<script>alert(1)</script>")
    _, ch = pkce()
    r = await client.get("/oauth/authorize", params=auth_params(cid, ch))
    assert r.status_code == 200
    assert "claude.ai" in r.text and "alert(1)" not in r.text  # показуємо host redirect_uri, не client_name
    assert 'type="password"' in r.text and 'autocomplete="off"' in r.text and "Подключить" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
    ck = r.headers["set-cookie"].lower()
    assert "tgb_oauth_tx=" in ck and "secure" in ck and "httponly" in ck and "samesite=strict" in ck
    assert "path=/oauth" in ck and "max-age=600" in ck


@pytest.mark.parametrize("over", [
    {"code_challenge_method": "plain"}, {"code_challenge_method": None}, {"code_challenge": None},
    {"code_challenge": "short"}, {"code_challenge": "a" * 44}, {"code_challenge": "!" * 43},
    {"response_type": "token"}, {"response_type": None}, {"client_id": "mcpc_unknown"}, {"client_id": None},
    {"redirect_uri": "https://evil.example/cb"}, {"redirect_uri": "https://claude.ai.evil.com/api/mcp/auth_callback"},
    {"redirect_uri": "https://claude.ai/api/mcp/auth_callback/../x"}, {"redirect_uri": REDIR + "?"},
    {"redirect_uri": "https://CLAUDE.ai/api/mcp/auth_callback"}, {"redirect_uri": "/../x"},
    {"redirect_uri": REDIR2},  # дозволений, але не зареєстрований цим клієнтом
    {"redirect_uri": None}, {"state": "s" * 2049}, {"resource": "https://evil.example/mcp"},
])
async def test_authorize_invalid_never_redirects(client, over):
    cid = await register(client)  # лише REDIR
    _, ch = pkce()
    r = await client.get("/oauth/authorize", params=auth_params(cid, ch, **over))
    assert r.status_code == 400 and "location" not in r.headers and "set-cookie" not in r.headers
    assert not oauth._tx


async def test_authorize_resource_match_and_state_optional(client):
    cid = await register(client)
    v, ch = pkce()
    secret, _ = await new_key()
    tx, cookie = await start(client, cid, ch, resource=BASE + "/mcp", state=None)
    r = await signin(client, tx, cookie, secret)
    assert "state" not in parse_qs(urlsplit(r.headers["location"]).query)


# ─── authorize POST ───────────────────────────────────────────────────────────

async def test_happy_path_full_flow(client):
    await seed_chat("-100", "G", creo=True)
    await seed_msg("-100", 1)
    secret, key = await new_key(["creo"])
    cid = await register(client)
    v, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    r = await signin(client, tx, cookie, secret, origin=BASE)
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.status_code == 303 and r.headers["cache-control"] == "no-store"
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIR
    q = parse_qs(loc.query)
    assert q["state"] == ["st8"]
    t = await exchange(client, q["code"][0], cid, v)
    assert t.status_code == 200 and t.headers["cache-control"] == "no-store" and t.headers["pragma"] == "no-cache"
    b = t.json()
    assert b["token_type"] == "Bearer" and b["expires_in"] == 3600 and b["scope"] == "mcp"
    assert b["access_token"].startswith("tgba_") and b["refresh_token"].startswith("tgbr_")
    assert await mcp_status(client, b["access_token"]) == 200
    err, data = await call(client, b["access_token"], "list_chats")
    assert not err and [c["group_id"] if "group_id" in c else c for c in data["chats"]]
    # ротація
    r2 = await refresh(client, b["refresh_token"], cid)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["access_token"] != b["access_token"] and b2["refresh_token"] != b["refresh_token"]
    assert await mcp_status(client, b2["access_token"]) == 200
    assert await mcp_status(client, b["access_token"]) == 401  # попередній access відкликано
    methods = [x.method for x in await audit_rows() if x.status == "ok"]
    assert {"oauth_register", "oauth_authorize", "oauth_token", "oauth_refresh"} <= set(methods)
    # TTL: access 1 год, refresh 90 діб
    async with db_connection.async_session() as s:
        rows = (await s.execute(select(McpOAuthTokenModel).where(McpOAuthTokenModel.key_id == key.id))).scalars().all()
    ttl = {x.kind: x.expires_at - x.created_at for x in rows}
    assert ttl["access"] == timedelta(hours=1) and ttl["refresh"] == timedelta(days=90)


async def test_wrong_key_generic_not_echoed_audited(client):
    cid = await register(client)
    _, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    r = await signin(client, tx, cookie, "tgb_SUPERSECRETGUESS")
    assert r.status_code == 401 and "Ключ не подошёл" in r.text
    assert "SUPERSECRETGUESS" not in r.text and "location" not in r.headers
    rows = [x for x in await audit_rows() if x.method == "oauth_authorize"]
    assert len(rows) == 1 and (rows[0].status, rows[0].reason, rows[0].key_id) == ("denied", "invalid_token", None)
    assert "SUPERSECRETGUESS" not in repr((rows[0].arguments, rows[0].reason))
    assert not oauth._codes


async def test_revoked_and_deleted_key_cannot_sign_in(client):
    cid = await register(client)
    for action in (McpKeyRepository.revoke, McpKeyRepository.delete):
        secret, key = await new_key()
        await action(key.id, "42")
        _, ch = pkce()
        tx, cookie = await start(client, cid, ch)
        r = await signin(client, tx, cookie, secret)
        assert r.status_code == 401 and "Ключ не подошёл" in r.text and "location" not in r.headers
    assert not oauth._codes
    rows = [x for x in await audit_rows() if x.reason == "revoked"]
    assert len(rows) == 2 and all(x.key_id for x in rows)


async def test_expired_key_cannot_sign_in(client):
    secret, key = await new_key()
    async with db_connection.async_session() as s:
        await s.execute(update(McpKeyModel).where(McpKeyModel.id == key.id)
                        .values(expires_at=datetime.utcnow() - timedelta(minutes=1)))
        await s.commit()
    cid = await register(client)
    _, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    assert (await signin(client, tx, cookie, secret)).status_code == 401


async def test_csrf_variants(client):
    secret, _ = await new_key()
    cid = await register(client)
    _, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    # без cookie
    r = await signin(client, tx, None, secret)
    assert r.status_code == 400 and "Сессия истекла" in r.text and "location" not in r.headers
    # cookie ≠ tx
    r = await signin(client, tx, "other-cookie", secret)
    assert r.status_code == 400 and "location" not in r.headers
    # чужий tx з валідною cookie
    tx2, cookie2 = await start(client, cid, ch)
    assert (await signin(client, tx, cookie2, secret)).status_code == 400
    # неіснуючий tx
    assert (await signin(client, "nope", "nope", secret)).status_code == 400
    # чужий Origin
    for origin in ("https://evil.example", "https://bot.example.evil.com", "null"):
        r = await signin(client, tx, cookie, secret, origin=origin)
        assert r.status_code == 403 and "location" not in r.headers
    assert not oauth._codes
    # після всього чесний вхід працює (tx не спалено атаками)
    assert (await signin(client, tx, cookie, secret, origin=BASE)).status_code == 303


async def test_tx_expired_and_single_use(client):
    secret, _ = await new_key()
    cid = await register(client)
    _, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    oauth._tx[tx]["exp"] = 0
    assert (await signin(client, tx, cookie, secret)).status_code == 400
    tx, cookie = await start(client, cid, ch)
    assert (await signin(client, tx, cookie, secret)).status_code == 303
    assert (await signin(client, tx, cookie, secret)).status_code == 400  # tx одноразовий


async def test_tx_dropped_after_5_failures(client):
    secret, _ = await new_key()
    cid = await register(client)
    _, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    for _ in range(4):
        assert (await signin(client, tx, cookie, "tgb_bad")).status_code == 401
    assert (await signin(client, tx, cookie, "tgb_bad")).status_code == 400
    assert (await signin(client, tx, cookie, secret)).status_code == 400  # навіть правильний ключ — вже ні


async def test_ip_throttle_11th_failure_429_aggregated(client):
    cid = await register(client)
    _, ch = pkce()
    tx = cookie = None
    for i in range(10):
        if i % 4 == 0:
            tx, cookie = await start(client, cid, ch)
        assert (await signin(client, tx, cookie, "tgb_bad")).status_code == 401
    tx, cookie = await start(client, cid, ch)
    secret, _ = await new_key()
    for _ in range(3):
        r = await signin(client, tx, cookie, secret)  # навіть правильний ключ заблоковано
        assert r.status_code == 429 and "location" not in r.headers
    rows = [x for x in await audit_rows() if x.reason == "throttled"]
    assert len(rows) == 1 and rows[0].hits == 3 and rows[0].key_id is None


async def test_authorize_post_oversize_and_garbage(client):
    cid = await register(client)
    _, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    r = await client.post("/oauth/authorize", content=b"tx=" + b"a" * 5000, headers={**FORM, "Cookie": f"tgb_oauth_tx={cookie}"})
    assert r.status_code == 413
    r = await client.post("/oauth/authorize", content=b"tx=a&tx=b&key=c", headers={**FORM, "Cookie": f"tgb_oauth_tx={cookie}"})
    assert r.status_code == 400


# ─── token endpoint ───────────────────────────────────────────────────────────

async def test_wrong_pkce_verifier_burns_code(client):
    secret, _ = await new_key()
    cid = await register(client)
    v, ch = pkce()
    code = await get_code(client, cid, ch, secret)
    r = await exchange(client, code, cid, pkce()[0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert (await exchange(client, code, cid, v)).status_code == 400  # код спалено, правильний verifier вже не допоможе
    reasons = [x.reason for x in await audit_rows() if x.method == "oauth_token"]
    assert "pkce_mismatch" in reasons
    for bad in ("short", "a" * 129, "!" * 50):
        code = await get_code(client, cid, ch, secret)
        assert (await exchange(client, code, cid, bad)).json()["error"] == "invalid_grant"


async def test_code_replay_revokes_first_tokens(client):
    c = await connect(client)
    first = c["tokens"]
    assert await mcp_status(client, first["access_token"]) == 200
    r = await exchange(client, c["code"], c["cid"], c["verifier"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert await mcp_status(client, first["access_token"]) == 401
    assert (await refresh(client, first["refresh_token"], c["cid"])).status_code == 400
    rows = [x for x in await audit_rows() if x.reason == "code_replay"]
    assert len(rows) == 1 and rows[0].key_id == c["key"].id


async def test_unknown_and_expired_code(client):
    secret, _ = await new_key()
    cid = await register(client)
    v, ch = pkce()
    assert (await exchange(client, "nope", cid, v)).json()["error"] == "invalid_grant"
    code = await get_code(client, cid, ch, secret)
    oauth._codes[code]["exp"] = 0
    assert (await exchange(client, code, cid, v)).json()["error"] == "invalid_grant"


async def test_redirect_and_client_mismatch_at_token(client):
    secret, _ = await new_key()
    cid = await register(client, uris=(REDIR, REDIR2))
    other = await register(client)
    v, ch = pkce()
    code = await get_code(client, cid, ch, secret)
    assert (await exchange(client, code, cid, v, redirect_uri=REDIR2)).json()["error"] == "invalid_grant"
    code = await get_code(client, cid, ch, secret)
    assert (await exchange(client, code, other, v)).json()["error"] == "invalid_grant"
    code = await get_code(client, cid, ch, secret)
    assert (await exchange(client, code, cid, v, resource="https://evil.example/mcp")).status_code == 400
    reasons = {x.reason for x in await audit_rows() if x.method == "oauth_token"}
    assert {"redirect_mismatch", "client_mismatch"} <= reasons
    assert not [x for x in await audit_rows() if x.method == "oauth_token" and x.status == "ok"]


async def test_revoked_key_between_authorize_and_token(client):
    secret, key = await new_key()
    cid = await register(client)
    v, ch = pkce()
    code = await get_code(client, cid, ch, secret)
    await McpKeyRepository.revoke(key.id, "42")
    assert (await exchange(client, code, cid, v)).json()["error"] == "invalid_grant"


async def test_refresh_client_mismatch_does_not_burn_token(client):
    c = await connect(client)
    other = await register(client)
    r = await refresh(client, c["tokens"]["refresh_token"], other)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert (await refresh(client, c["tokens"]["refresh_token"], c["cid"])).status_code == 200
    assert "client_mismatch" in [x.reason for x in await audit_rows()]


async def test_refresh_reuse_revokes_family(client):
    c = await connect(client)
    t1 = c["tokens"]
    t2 = (await refresh(client, t1["refresh_token"], c["cid"])).json()
    assert await mcp_status(client, t2["access_token"]) == 200
    r = await refresh(client, t1["refresh_token"], c["cid"])  # повторне використання старого refresh
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert await mcp_status(client, t2["access_token"]) == 401
    assert (await refresh(client, t2["refresh_token"], c["cid"])).status_code == 400
    rows = [x for x in await audit_rows() if x.reason == "refresh_reuse"]
    assert len(rows) == 1 and rows[0].key_id == c["key"].id


async def test_parallel_refresh_only_one_wins(client):
    c = await connect(client)
    rs = await asyncio.gather(*[refresh(client, c["tokens"]["refresh_token"], c["cid"]) for _ in range(3)])
    assert sorted(r.status_code for r in rs).count(200) == 1


async def test_refresh_expired_and_unknown(client):
    c = await connect(client)
    async with db_connection.async_session() as s:
        await s.execute(update(McpOAuthTokenModel).where(McpOAuthTokenModel.kind == "refresh")
                        .values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        await s.commit()
    assert (await refresh(client, c["tokens"]["refresh_token"], c["cid"])).json()["error"] == "invalid_grant"
    assert (await refresh(client, "tgbr_nope", c["cid"])).json()["error"] == "invalid_grant"


@pytest.mark.parametrize("action", ["revoke", "delete"])
async def test_key_killed_midsession(client, action):
    c = await connect(client)
    t = c["tokens"]
    assert await mcp_status(client, t["access_token"]) == 200
    await (McpKeyRepository.revoke if action == "revoke" else McpKeyRepository.delete)(c["key"].id, "42")
    r = await client.post("/mcp", json=INIT, headers=hdr(t["access_token"]))
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    r = await refresh(client, t["refresh_token"], c["cid"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert [x for x in await audit_rows() if x.reason == "revoked" and x.key_id == c["key"].id]


async def test_expired_access_token_401(client):
    c = await connect(client)
    async with db_connection.async_session() as s:
        await s.execute(update(McpOAuthTokenModel).where(McpOAuthTokenModel.kind == "access")
                        .values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
        await s.commit()
    assert await mcp_status(client, c["tokens"]["access_token"]) == 401


async def test_header_confusion(client):
    c = await connect(client)
    t = c["tokens"]
    for bad in (c["secret"], t["refresh_token"], c["code"], "tgba_" + "x" * 40):
        r = await client.post("/mcp", json=INIT, headers=hdr(bad))
        assert r.status_code == 401, bad
    for h in ({"Authorization": "Basic " + t["access_token"]}, {"Authorization": "Bearer"},
              {"Authorization": "Bearer "}, {"Authorization": t["access_token"]}):
        assert (await client.post("/mcp", json=INIT, headers=h)).status_code == 401, h
    assert (await client.post("/mcp", json=INIT, headers={"Authorization": f"bearer {t['access_token']}"})).status_code == 200
    # access-токен як refresh_token, і навпаки
    assert (await refresh(client, t["access_token"], c["cid"])).json()["error"] == "invalid_grant"
    # 401 без токена — без error=, з токеном — з error=
    r = await client.post("/mcp", json=INIT)
    assert "error=" not in r.headers["www-authenticate"] and "resource_metadata=" in r.headers["www-authenticate"]


async def test_token_endpoint_malformed(client):
    c = await connect(client)
    r = await client.post("/oauth/token", json={"grant_type": "refresh_token"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    r = await client.post("/oauth/token", data={"client_id": c["cid"]}, headers=FORM)
    assert r.json()["error"] == "invalid_request"
    r = await client.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": c["cid"]}, headers=FORM)
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
    r = await client.post("/oauth/token", data={"grant_type": "password", "client_id": c["cid"]}, headers=FORM)
    assert r.json()["error"] == "unsupported_grant_type"
    r = await client.post("/oauth/token", content=b"grant_type=" + b"a" * (8 * 1024), headers=FORM)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    r = await client.post("/oauth/token", content=b"grant_type=refresh_token&grant_type=x&client_id=a", headers=FORM)
    assert r.json()["error"] == "invalid_request"
    r = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": c["cid"]}, headers=FORM)
    assert r.json()["error"] == "invalid_request"
    r = await client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": c["cid"]}, headers=FORM)
    assert r.json()["error"] == "invalid_request"
    assert r.headers["cache-control"] == "no-store"


# ─── scope, fail-closed, зберігання ───────────────────────────────────────────

async def test_scope_is_live_through_oauth(client):
    await seed_chat("-1", "A", partner_mp=True)
    await seed_chat("-2", "B", media_mt=True)
    c = await connect(client, ["partner_mp"])
    tok = c["tokens"]["access_token"]
    err, data = await call(client, tok, "list_chats")
    assert not err and len(data["chats"]) == 1
    async with db_connection.async_session() as s:
        await s.execute(update(McpKeyModel).where(McpKeyModel.id == c["key"].id).values(categories=["media_mt"]))
        await s.commit()
    err, data = await call(client, tok, "list_chats")
    assert not err and len(data["chats"]) == 1 and "B" in json.dumps(data, ensure_ascii=False)


async def test_audit_fail_closed(client, monkeypatch):
    secret, _ = await new_key()
    cid = await register(client)
    v, ch = pkce()
    tx, cookie = await start(client, cid, ch)
    code = await get_code(client, cid, ch, secret)

    async def none(**kw):
        return None

    monkeypatch.setattr(McpAuditRepository, "add", staticmethod(none))
    # authorize: ключ валідний, але аудит недоступний → без коду
    codes_before = set(oauth._codes)
    r = await signin(client, tx, cookie, secret)
    assert r.status_code == 503 and "location" not in r.headers and set(oauth._codes) == codes_before
    # token: аудит недоступний → без токенів, родина відкликана
    r = await exchange(client, code, cid, v)
    assert r.status_code == 503 and "access_token" not in r.text
    async with db_connection.async_session() as s:
        rows = (await s.execute(select(McpOAuthTokenModel))).scalars().all()
    assert rows and all(x.revoked_at is not None for x in rows)
    # register: теж fail-closed
    r = await client.post("/oauth/register", json={"redirect_uris": [REDIR]})
    assert r.status_code == 503 and "client_id" not in r.text


async def test_only_hashes_stored_no_plaintext_in_db_or_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    secret, key = await new_key()
    cid = await register(client)
    v, ch = pkce()
    code = await get_code(client, cid, ch, secret)
    t = (await exchange(client, code, cid, v)).json()
    t2 = (await refresh(client, t["refresh_token"], cid)).json()
    await refresh(client, t["refresh_token"], cid)  # reuse
    tx, cookie = await start(client, cid, ch)
    await signin(client, tx, cookie, "tgb_WRONGGUESS")
    plain = [secret, code, v, t["access_token"], t["refresh_token"], t2["access_token"], t2["refresh_token"],
             "tgb_WRONGGUESS", tx]
    async with db_connection.async_session() as s:
        rows = (await s.execute(select(McpOAuthTokenModel))).scalars().all()
        dump = repr([[getattr(x, c.name) for c in McpOAuthTokenModel.__table__.columns] for x in rows])
        dump += repr([[getattr(x, c.name) for c in McpAuditLogModel.__table__.columns] for x in await audit_rows()])
    assert rows and all(len(x.token_hash) == 64 and re.fullmatch(r"[0-9a-f]{64}", x.token_hash) for x in rows)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for p in plain:
        assert p not in dump, p
        assert p not in logs, p


async def test_delete_expired_tokens_retention(client):
    c = await connect(client)
    async with db_connection.async_session() as s:
        await s.execute(update(McpOAuthTokenModel).where(McpOAuthTokenModel.kind == "access")
                        .values(expires_at=datetime.utcnow() - timedelta(days=8)))
        await s.commit()
    assert await McpOAuthRepository.delete_expired(7) == 1
    async with db_connection.async_session() as s:
        left = (await s.execute(select(McpOAuthTokenModel))).scalars().all()
    assert [x.kind for x in left] == ["refresh"]


async def test_k_routes_and_raw_key_bearer_gone(client):
    secret, _ = await new_key()
    assert (await client.post(f"/mcp/k/{secret}", json=INIT)).status_code == 404
    assert (await client.post("/mcp", json=INIT, headers=hdr(secret))).status_code == 401


def test_access_log_filter_redacts_tokens():
    f = server._RedactSecrets()
    raw, acc, ref = "tgb_" + "A" * 20, "tgba_" + "b-_" * 10, "tgbr_" + "c" * 20
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/1.1" %d',
                            ("1.2.3.4:5", "POST", f"/mcp/k/{raw}?t={acc}&r={ref}", 404), None)
    assert f.filter(rec)
    out = rec.getMessage()
    assert out.count("tgb_***") == 3 and raw not in out and acc not in out and ref not in out and "404" in out
    assert any(isinstance(x, server._RedactSecrets) for x in logging.getLogger("uvicorn.access").filters)


async def test_register_global_cap_200_per_hour(client):
    now = server._now()
    oauth._reg_all.extend([now] * (oauth.REGISTER_GLOBAL_LIMIT - 1))
    assert (await client.post("/oauth/register", json={"redirect_uris": [REDIR]})).status_code == 201
    assert (await client.post("/oauth/register", json={"redirect_uris": [REDIR]})).status_code == 429
    oauth._reg_all.clear()
    oauth._reg_all.extend([now - oauth.REGISTER_WINDOW - 1] * oauth.REGISTER_GLOBAL_LIMIT)
    assert (await client.post("/oauth/register", json={"redirect_uris": [REDIR]})).status_code == 201


async def test_delete_unused_clients(client):
    from bot.data.models import McpOAuthClientModel as C
    old = await register(client)
    fresh = await register(client)
    c = await connect(client)  # у цього клієнта є токени
    used = c["cid"]
    async with db_connection.async_session() as s:
        await s.execute(update(C).where(C.client_id.in_([old, used])).values(
            created_at=datetime.utcnow() - timedelta(hours=25)))
        await s.commit()
    assert await McpOAuthRepository.delete_unused_clients(24) == 1
    async with db_connection.async_session() as s:
        left = set((await s.execute(select(C.client_id))).scalars().all())
    assert left == {used, fresh}
