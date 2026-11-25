---
name: impl-mcp-chat-archive
description: How group message capture, retention and the remote MCP server with per-key category scope + audit log are implemented in team-group-bot — outer-middleware capture, hand-rolled JSON-RPC MCP endpoint (no mcp SDK), scope_condition, fail-closed aggregated audit, keep-last-100 retention, admin key UI. Use when working on MessageCapture, chat_messages, MessageRepository retention, bot/domain/mcp/ (server.py, tools.py, scope.py), McpKeyRepository, McpAuditRepository, mcp_keys_.py, MCP_PATH, OAuth, oauth.py, McpOAuthRepository, claude.ai connector, sign-in page, tool output limits, rate limiting, audit log, or edited_message/allowed_updates.
---

# Chat archive + MCP access for claude.ai (OAuth connector)

## Problem
Owner wants Claude (claude.ai connector) users to read/summarize group chats, but only chats of categories chosen per issued key, with every access audited. The bot stored no message text; the official `mcp` SDK needs pydantic ≥2.7.2 while aiogram 3.4.1 pins pydantic <2.6 — upgrading aiogram without tests was too risky.

## Solution
- **Capture**: `MessageCapture` registered as `dp.message.outer_middleware` / `dp.edited_message.outer_middleware`. Outer middleware sees every update once, before filters (aiogram 3.4.1 `Router.propagate_event`), so routing is unchanged; it always `return await handler(...)`. Writes via own `engine.begin()` inside `asyncio.wait_for(..., 3)`. Stores only text/caption + content_type + author + times + thread + reply_to. Upsert: plain message = no-op on duplicate (`message_id = chat_messages.message_id`, never touch `id`); edit updates text/type/edit_date/is_edited.
- `edited_message` is added to `allowed_updates` explicitly (resolve_used_update_types ignores observers without handlers). `drop_pending_updates=False` so downtime messages are archived.
- **Retention** (`MessageRepository.delete_old_messages`, daily `_retention_loop` in `bot/main.py`): delete `sent_at < now-180d`; groups present in `chats` with >100 rows keep newest 100 (boundary via `(sent_at, id)` of the 100th row, `sent_at < t OR (sent_at = t AND id < i)`); groups ≤100 rows skipped; groups not in `chats` get plain cutoff. One `delete(...).with_dialect_options(mysql_limit=10000)` loop for MySQL and sqlite (tests hit the production path). Audit log cleaned after 365 d.
- **MCP endpoint** (`bot/domain/mcp/server.py`): `POST MCP_PATH` JSON-RPC 2.0 (initialize / ping / tools/list / tools/call; notifications → 202; batch → -32600), GET/DELETE 405, Origin → 403 unless `https://claude.ai`/`https://claude.com`, >64 KB → 413. Auth = OAuth access token only (`McpOAuthRepository.resolve_access`, live join to `mcp_keys`); 401 carries `WWW-Authenticate: Bearer resource_metadata="<ISSUER>/.well-known/oauth-protected-resource"` (+ `error="invalid_token"`). Per-key 60/min in-memory limit (single uvicorn process).
- **OAuth 2.1 AS** (`bot/domain/mcp/oauth.py`, `McpOAuthRepository.py`, revision c4e8a1d7b296, plan `.claude/plans/mcp-oauth.md`): for claude.ai custom connectors; connector URL `<WEBHOOK_BASE_URL><MCP_PATH>` has no secret. ISSUER = base + MCP_PATH parent (prod `/team-group-bot`, nginx does NOT strip prefix → `MCP_PATH=/team-group-bot/mcp`). RFC 9728/8414 metadata (+ `openid-configuration` alias), DCR with exact redirect allowlist (`https://claude.ai|claude.com/api/mcp/auth_callback`), PKCE S256, Russian sign-in page where the user pastes the bot key (tx in memory + SameSite=Strict cookie, IP throttle, never echoes key, errors never redirect). Access 1 h / refresh 90 d rotated, sha256 stored, reuse/code replay revoke the family. Revoke/delete key kills all tokens via live join. Claude Code / raw-key Bearer / `/k/` path removed.
- **Scope** (`scope.py`): key `categories` JSON → `or_(ChatModel.<flag> == True)`; `"all"` → every row in `chats`; invalid/empty → `false()`. Every query JOINs `chats`; out-of-scope and unknown chat both return "chat not found". Evaluated live at query time.
- **Tools** (`tools.py`): `list_chats`, `get_messages` (keyset cursor `sent_at|id`, ≤100, ~40 KB cap → `next_cursor`, ≤31-day range), `search_messages` (LIKE escaped, ≤50, `truncated` flag, no pagination). Output `{authors:{id:{...}}, messages:[...]}`.
- **Audit**: every request audited; fail-closed (audit insert fails → no data returned). Unauthenticated and rate-limited denials aggregated: one row per (kind, key|ip, minute) with `hits`.
- **Keys**: `tgb_` + token_urlsafe(32), only sha256 + 8-char prefix stored, no expiry, revocable, soft-deletable (`deleted_at`, revision 3b7d9e4c1a52; delete also revokes, hidden from list, audit rows kept). Admin UI `mcp_keys_.py` (ADMIN role only, each handler re-checks); secret shown once + connector URL + claude.ai steps (key is pasted only on our sign-in page), never stored in FSM, ADMINs notified on create/revoke/delete.

## Key files
- `bot/domain/middlewares/MessageCapture.py` — capture middleware, `_values()` mapping.
- `bot/data/repositories/MessageRepository.py` — upsert, retention, `MessageStats` (UI capture-health indicators).
- `bot/domain/mcp/server.py`, `tools.py`, `scope.py` — MCP protocol, tools, scope.
- `bot/data/repositories/McpKeyRepository.py`, `McpAuditRepository.py` — keys, audit.
- `bot/domain/routers/admin/chat/menu/mcp_keys_.py` — admin UI.
- `alembic/versions/004_add_chat_messages_and_mcp.py` — `chat_messages`, `mcp_keys`, `mcp_audit_log`.
- `tests/` — sqlite (aiosqlite StaticPool) suite incl. `test_mcp_adversarial.py`; run `.venv/bin/python -m pytest -q tests/`.

## Gotchas
- Never import `bot.main` in tests (creates `Bot(token=None)`); mount `mcp_router` on a fresh FastAPI.
- `edit_date` is int unix seconds in aiogram 3.4.1; `date` is tz-aware → store naive UTC.
- `hide_parameters=True` on the engine keeps message text out of DB error logs; capture logs only exception type.
- Claude tool-output limit ≈25k tokens — keep the 40 KB cap.
- sqlite LIKE is case-sensitive for Cyrillic, MySQL utf8mb4_unicode_ci is not — tests use same case.
- Bot sees all group messages only if admin or privacy mode disabled (BotFather, then re-add bot). It never sees its own messages, other bots, or deletions.
- `client_ip` = rightmost X-Forwarded-For — correct only behind exactly one nginx hop.
- nginx (shared container, outside repo): strict 10 r/m only for requests without Authorization, soft 5 r/s burst 20 with token, `limit_req_status 429`.
