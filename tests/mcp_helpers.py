import json
from datetime import datetime

import httpx
from fastapi import FastAPI

from bot.data import db_connection
from bot.data.models import ChatMessageModel, ChatModel
from bot.data.repositories.McpKeyRepository import McpKeyRepository
from bot.data.repositories.McpOAuthRepository import McpOAuthRepository
from bot.domain.mcp import oauth, server

T0 = datetime(2025, 1, 10, 12, 0, 0)


def make_client():
    app = FastAPI()
    app.include_router(server.router)
    app.include_router(oauth.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def seed_chat(gid, title="T", **flags):
    async with db_connection.async_session() as s:
        s.add(ChatModel(group_id=gid, title=title, **flags))
        await s.commit()


async def seed_msg(gid, mid, sent_at=T0, text="hello", author="1", **kw):
    async with db_connection.async_session() as s:
        s.add(ChatMessageModel(group_id=gid, message_id=mid, sent_at=sent_at, text=text, message_type="text",
                               author_id=author, author_username="u" + str(author), author_first_name="N", **kw))
        await s.commit()


async def make_key(categories):
    """Повертає (OAuth access-токен, key): MCP приймає лише access-токени."""
    _, key = await McpKeyRepository.create("lbl", "42", categories)
    access, _refresh, _family = await McpOAuthRepository.issue_pair(key.id, "mcpc_test")
    return access, key


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


async def rpc(client, token, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/mcp", json=body, headers=hdr(token))


async def call(client, token, name, args=None):
    r = await rpc(client, token, "tools/call", {"name": name, "arguments": args or {}})
    res = r.json()["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))
