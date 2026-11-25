# Plan: group message capture + remote MCP server with per-key scoped access + audit log

Status: draft for /review-plan. Architect, 2026-11-25. Supersedes the "fixed MP filter" draft: access scope is now **per key**, chosen when the key is created.

## 1. What and why
Bot starts storing every message (incl. edits) from every group/supergroup it is in into new `chat_messages` table. Existing FastAPI app (port 8019) gets a remote MCP endpoint (streamable HTTP, JSON responses, hand-rolled; official SDK conflicts with pins, see section 4). Every key is a bot-generated secret (hash stored only) with label, creator, optional expiry, revoked flag and a **category scope**; every MCP query filters chats by that scope server-side, out-of-scope chat = "not found". Every MCP call (incl. denied) goes to `mcp_audit_log`, viewable in bot; if the audit insert fails the call is refused. Retention loop deletes old messages.

All NEW user-visible bot texts (buttons, screens, notifications) are in **Russian**. A parallel task translates existing UI from Ukrainian to Russian in keyboards/routers/constants — line numbers in `bot/presentation/keyboard/admin_.py`, `bot/domain/routers/admin/chat/main_chat.py`, `bot/data/other/constants.py` may shift; developer must locate anchors by symbol (`kb_main`, `router.include_routers(...)`), not line number, and rebase onto that task's result.

## 2. Verified facts (from the tree)
- `bot/main.py`: `dp.include_routers(events_.router, main_chat_module.router, main_group_module.router, main_.router)`; `set_webhook(..., drop_pending_updates=True, allowed_updates=dp.resolve_used_update_types())`; `_health_check_loop` + `asyncio.create_task` in `lifespan`, cancelled after `yield`.
- `bot/domain/routers/user/main_.py` `all_message_handler` sits behind `IsGroupUser` (inner middleware) which returns early for admins and known users → unusable for capture.
- aiogram 3.4.1 `Router.propagate_event` (`.venv/.../aiogram/dispatcher/router.py` L118-129) wraps the whole propagation in `observer.wrap_outer_middleware` whenever the observer exists, even if no handler matches → `dp.message.outer_middleware(...)` sees every message once, before filters, without affecting handler choice.
- `resolve_used_update_types` (router.py L97-116) only counts observers **with handlers**; there is no `edited_message` handler anywhere → `edited_message` is currently NOT in allowed_updates. Must be added explicitly.
- `ChatModel` (`bot/data/models.py`) has 12 bool category columns = `TypeOfChats` values minus `ALL` (`bot/data/other/accesses.py`). Trap: both `media_mt` and `media_mp` exist.
- `ChatRepository.chat_by_type("all")` would `getattr(ChatModel, "all")` and fail → "all" has no column; must be special-cased in scope logic.
- Repos: static methods, own `async_session()` per call, `@log_exc_with_try_except()` returns `None` on error (`bot/data/db_connection.py`). `db_connection.py` builds the engine at import from `DB_LINK_CONNECTION` (engine creation is lazy for aiomysql; `None` would fail).
- Role gate pattern: `admin.role != TypeOfAdmins.ADMIN.value` (`statistic_.py`, `bulk_.py`); `kb_main(admin)` shows ADMIN-only buttons.
- Alembic chain: `d8cd90ead460` → `a1b2c3d4e5f6` (002) → `b2c3d4e5f6a7` (003, head). Initial tables are utf8mb4/utf8mb4_unicode_ci; `.env.example` DSN has `?charset=utf8mb4`.
- No tests, no pytest in `requirements.txt`; `.venv` interpreter works only via `.venv/bin/python -m pip` (pip shebang points to an old path).

## 3. Affected files
Modify:
- `/Users/mac/Documents/GitHub/team-group-bot/bot/data/models.py` — add `ChatMessageModel`, `McpKeyModel`, `McpAuditLogModel`.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/data/other/accesses.py` — add `MCP_KEY_ROLES = {TypeOfAdmins.ADMIN.value}`; helper `category_columns()` (TypeOfChats minus ALL). No global MP constant.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/main.py` — register capture outer middleware, explicit `edited_message` in allowed_updates, `_retention_loop`, `app.include_router(mcp_router)`.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/domain/routers/admin/chat/main_chat.py` — include `mcp_keys_.router`.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/presentation/keyboard/admin_.py` — ADMIN-only "🔑 Ключи MCP" button in `kb_main`; new CallbackData + keyboards for keys/audit.
- `/Users/mac/Documents/GitHub/team-group-bot/.env.example` — new vars.
New:
- `/Users/mac/Documents/GitHub/team-group-bot/alembic/versions/004_add_chat_messages_and_mcp.py` (revision `c3d4e5f6a7b8`, down `b2c3d4e5f6a7`).
- `/Users/mac/Documents/GitHub/team-group-bot/bot/data/repositories/MessageRepository.py` — upsert, scoped list/get/search, retention delete.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/data/repositories/McpKeyRepository.py` — create/list/get/revoke/resolve-by-hash/touch.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/data/repositories/McpAuditRepository.py` — insert (returns id or None), page by key, page of unknown-key denials, export rows.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/domain/middlewares/MessageCapture.py` — outer middleware.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/domain/mcp/__init__.py`, `server.py` (APIRouter, JSON-RPC dispatch, auth, audit), `tools.py` (tool schemas + handlers), `scope.py` (pure: key categories → SQLAlchemy condition on `ChatModel`).
- `/Users/mac/Documents/GitHub/team-group-bot/bot/domain/states/McpKey.py` — FSM `label`, `categories`, `expiry`, `confirm`.
- `/Users/mac/Documents/GitHub/team-group-bot/bot/domain/routers/admin/chat/menu/mcp_keys_.py` — keys + audit UI.
- `/Users/mac/Documents/GitHub/team-group-bot/requirements-dev.txt` — `-r requirements.txt`, pytest, pytest-asyncio, httpx (TestClient), aiosqlite.
- `/Users/mac/Documents/GitHub/team-group-bot/tests/conftest.py`, `tests/test_mcp_scope.py`, `tests/test_mcp_auth_audit.py`.
Do not touch: `team_group_db.sql`, applied revisions 001-003, `requirements.txt` (no new runtime deps), `bot/domain/routers/user/main_.py`, `IsGroupUser.py`.

## 4. Dependency check: official `mcp` SDK — rejected
`.venv` (py 3.11.9): pydantic 2.5.3, starlette 0.38.6, anyio 4.13.0, fastapi 0.115.0.
`pip install --dry-run --ignore-installed -r requirements.txt "mcp>=1.8"` → ResolutionImpossible: `aiogram 3.4.1 depends on pydantic<2.6,>=2.4.1`; `mcp 1.8.0 requires pydantic>=2.7.2`, `mcp 1.25-1.30 pydantic>=2.11`, `mcp 2.x pydantic>=2.12`. Unconstrained `mcp` backtracks to `mcp 0.9.1` (pre-streamable-HTTP, useless).
Using the SDK would require moving `aiogram` 3.4.1 → a release allowing pydantic>=2.11 and `pydantic`/`pydantic_core` to ≥2.11 — aiogram API changes across ~20 minor releases, whole bot regression with zero tests. Rejected.
Chosen: hand-rolled minimal MCP streamable-HTTP server (tools only, stateless, JSON responses, no SSE):
- `POST {MCP_PATH}`: one JSON-RPC 2.0 object per request (batch arrays → `-32600`). Methods: `initialize` (echo client `protocolVersion` if in `SUPPORTED_VERSIONS = ("2025-11-25","2025-06-18","2025-03-26")`, else first; `capabilities: {"tools": {"listChanged": false}}`; `serverInfo`), `notifications/*` → HTTP 202 empty, `ping` → `{}`, `tools/list`, `tools/call` → `{"content":[{"type":"text","text":<json>}],"isError":bool}`; unknown method → `-32601`.
- `GET`/`DELETE {MCP_PATH}` → 405 (allowed by spec when no SSE stream/sessions). No `Mcp-Session-Id`.
- Request with `Origin` header → 403 (DNS-rebinding guard; Claude Code sends none). Body > 64 KB → 413.
- Auth on every request: `Authorization: Bearer <secret>`; failure → HTTP 401 + `WWW-Authenticate: Bearer` (audited, see 6).

## 5. Schema (revision `c3d4e5f6a7b8`, file `004_add_chat_messages_and_mcp.py`, hand-written, MySQL)
All three tables: `mysql_charset='utf8mb4', mysql_collate='utf8mb4_unicode_ci'` in `op.create_table` and in model `__table_args__`.

`chat_messages`
- `id` BigInteger PK autoincrement
- `group_id` String(255) NOT NULL (project convention, `str(chat.id)`)
- `message_id` BigInteger NOT NULL; `thread_id` BigInteger NULL (`message_thread_id`)
- `author_id` String(255) NULL; `author_username` String(255) NULL; `author_first_name` String(255) NULL (`from_user`; for anonymous admins/channels also fill from `sender_chat` id/title)
- `text` Text NULL (`text or caption`; ≤4096 chars ≈16 KB utf8mb4 < 64 KB TEXT)
- `message_type` String(32) NOT NULL (`message.content_type`), `has_media` Boolean NOT NULL default 0
- `sent_at` DateTime NOT NULL (`message.date` → naive UTC), `edit_date` DateTime NULL, `is_edited` Boolean NOT NULL default 0
- UNIQUE `uq_chat_messages_group_msg (group_id, message_id)`; INDEX `ix_chat_messages_group_sent (group_id, sent_at)`; INDEX `ix_chat_messages_sent (sent_at)` (retention)

`mcp_keys`
- `id` Integer PK autoincrement; `token_hash` CHAR(64) NOT NULL UNIQUE (sha256 hex); `token_prefix` String(16) NOT NULL (first 8 chars, display only)
- `label` String(255) NOT NULL; `created_by` String(255) NOT NULL (admin telegram_id); `created_at` DateTime NOT NULL
- `categories` JSON NOT NULL (list of `TypeOfChats` values, or `["all"]`)
- `expires_at` DateTime NULL; `revoked` Boolean NOT NULL default 0; `revoked_at` DateTime NULL; `revoked_by` String(255) NULL; `last_used_at` DateTime NULL

`mcp_audit_log`
- `id` BigInteger PK autoincrement; `created_at` DateTime NOT NULL
- `key_id` Integer NULL (NULL = unknown/missing token); no FK (log must survive anything)
- `method` String(64) NOT NULL (`initialize`, `tools/list`, `tools/call`, …); `tool_name` String(64) NULL
- `arguments` JSON NULL (sanitized: chat_id, since, until, query, limit, cursor — nothing else)
- `status` String(16) NOT NULL (`ok` | `denied` | `error`); `reason` String(64) NULL (`no_token`, `invalid_token`, `revoked`, `expired`, `out_of_scope`, `bad_args`, `internal`)
- `result_count` Integer NULL (messages or chats returned); `client_ip` String(64) NULL
- INDEX `ix_mcp_audit_key_created (key_id, created_at)`; INDEX `ix_mcp_audit_created (created_at)`

downgrade: drop indexes + 3 tables.

## 6. Contracts
Token: `"tgb_" + secrets.token_urlsafe(32)`; store `sha256(token).hexdigest()`; lookup by hash (unique index). Valid iff row exists, `revoked = 0`, `expires_at IS NULL OR expires_at > utcnow()`. Plaintext never logged or stored.

Scope (`bot/domain/mcp/scope.py`, pure, unit-tested): `scope_condition(categories) -> ColumnElement`
- every value validated with `has_value_enum(TypeOfChats, v)`; unknown values dropped; empty after validation → `false()` (sees nothing).
- `"all"` in list → `true()` = every row in `chats` (incl. unspecified). Messages of groups with no `chats` row are never reachable.
- else `or_(*(getattr(ChatModel, c) == True for c in cats))`.
All message/chat queries are `chat_messages JOIN chats ON chats.group_id = chat_messages.group_id WHERE scope_condition(key.categories) AND ...`. Explicit `chat_id` outside scope or unknown → identical tool error `"chat not found"` (no existence leak), audited as `denied/out_of_scope`.

Tools (all times ISO-8601, naive = UTC; output `sent_at` ISO with `Z`):
- `list_chats()` → `[{chat_id, title, categories:[...in-scope flags...]}]`.
- `get_messages(chat_id, since, until?, limit=100 (≤500), cursor?)` → ascending by `(sent_at, id)`, keyset cursor = opaque `"<sent_at_iso>|<id>"`; `until-since` ≤ 31 days; text truncated to 4000 chars per message; total payload cap ~200 KB → stop early and return `next_cursor`. Item: `{message_id, thread_id, sent_at, author_id, author_username, author_first_name, text, message_type, has_media, is_edited}`.
- `search_messages(query, chat_id?, since?, until?, limit=50 (≤100))` → `LIKE %q%` with `%`,`_`,`\` escaped; `len(query) ≥ 2`; default window = last 7 days, max = retention window; scoped by join + `(group_id, sent_at)` index. No FULLTEXT index (volume unknown; add later if slow).

Audit (fail-closed): `server.py` per request → resolve key → run handler → build response → `McpAuditRepository.add(...)` → if it returns `None` (insert failed) respond JSON-RPC error `-32603 "audit unavailable"` (or 503 for auth-failure path) and **discard** the result. Denied paths (no/invalid/revoked/expired token, out-of-scope chat, bad args) are logged too. `client_ip` = rightmost entry of `X-Forwarded-For` (the hop appended by our nginx), fallback `request.client.host`. `last_used_at` update only if NULL or older than 60 s (avoid write per call).

## 7. Implementation order
1. Models + revision 004 (section 5). → verify: `alembic upgrade head` then `alembic downgrade -1` then `upgrade head` on a scratch MySQL DB; `SHOW CREATE TABLE chat_messages` shows utf8mb4 + unique/index; `alembic heads` = single head `c3d4e5f6a7b8`.
2. `MessageRepository.upsert_message(...)` via `sqlalchemy.dialects.mysql.insert(...).on_duplicate_key_update(...)`: for `message` updates nothing meaningful (`id=inserted.id`, idempotent redelivery, does not overwrite later edits); for `edited_message` updates `text, message_type, has_media, edit_date, is_edited=1` (insert if never seen). `@log_exc_with_try_except()`. → verify: insert same `(group_id,message_id)` twice → 1 row; edit → text replaced, `is_edited=1`.
3. `bot/domain/middlewares/MessageCapture.py`: outer middleware; if `event.chat.type in {GROUP, SUPERGROUP}` → `try: await MessageRepository.upsert_message(...) except Exception: logging.exception(...)` (never log text), then `return await handler(event, data)` unconditionally. In `main.py`: `dp.message.outer_middleware(MessageCapture(edited=False))`, `dp.edited_message.outer_middleware(MessageCapture(edited=True))`; `allowed_updates=sorted(set(dp.resolve_used_update_types()) | {"edited_message"})`. → verify: send text/emoji/photo-with-caption/forum-topic message in test group as admin AND as known user → rows present; `/start` in group and group-update menu still work; new user still triggers "new user" notification; `getWebhookInfo.allowed_updates` contains `edited_message`; DB down → bot handlers still run, error in log.
4. `_retention_loop(bot)` in `main.py` mirroring `_health_check_loop`: sleep 120 s, then every 24 h `MessageRepository.delete_older_than(utcnow - MESSAGE_RETENTION_DAYS)` in batches (`delete(...).with_dialect_options(mysql_limit=10000)` loop until rowcount < 10000); `MESSAGE_RETENTION_DAYS <= 0` disables; task created/cancelled in `lifespan` next to health check. → verify: seed rows older than N days on scratch DB, run function once, only old rows gone; log line with count.
5. `scope.py` + `McpKeyRepository` + `McpAuditRepository` + `accesses.MCP_KEY_ROLES`. → verify: pytest step 9 scope tests pass.
6. `bot/domain/mcp/server.py` + `tools.py` (sections 4, 6); `app.include_router(mcp_router)` in `main.py`, path `MCP_PATH`. → verify: `curl -X POST` initialize without token → 401 + audit row `denied/no_token`; with token → initialize, `tools/list`, `tools/call list_chats` OK; out-of-scope `chat_id` → "chat not found" + `denied/out_of_scope`; revoked/expired key → 401; then `claude mcp add --transport http mt-chats <WEBHOOK_BASE_URL><MCP_PATH> --header "Authorization: Bearer <secret>"` and `/mcp` in Claude Code lists 3 tools.
7. Admin UI `mcp_keys_.py` (Russian texts), included in `main_chat.router` (already behind `IsPrivateChatAdmin`); each handler additionally checks `admin.role in MCP_KEY_ROLES` else `callback.answer("Нет доступа", show_alert=True)`. Screens:
   - main: "🔑 Ключи MCP" → list (label, prefix, status: активен / отозван / истёк) + "➕ Создать ключ" + "🚫 Отказы без ключа".
   - create: label text input ("Для кого ключ?") → category multi-select toggles (12 categories from `CATEGORY_LABELS` + "Все группы"; "Далее" disabled while empty) → expiry ("Без срока" / "30 дней" / "90 дней") → confirm summary → secret in `<code>` + ready command `claude mcp add --transport http mt-chats {WEBHOOK_BASE_URL}{MCP_PATH} --header "Authorization: Bearer {secret}"` + warning "Ключ показан один раз" + button "Я сохранил — скрыть" (edits message, removes secret).
   - view: label, created_by, created_at, categories, expiry, last_used_at, buttons "📜 Журнал", "🚫 Отозвать" (confirm), "↩️ Назад".
   - audit: latest-first, 10 per page, prev/next; row = time UTC, method/tool, chat_id/range/query (short), result_count, status/reason, IP. Optional last sub-step: "📥 Экспорт CSV" → `BufferedInputFile`, last 5000 rows of the key.
   → verify: full create → use → view log → revoke → next MCP call 401 flow by hand; non-ADMIN admin sees no button and gets "Нет доступа" on forged callback.
8. `.env.example`: `MCP_PATH=/mcp`, `MESSAGE_RETENTION_DAYS=90`. → verify: app starts with vars absent (defaults).
9. Tests (no live DB): `requirements-dev.txt`; `tests/conftest.py` sets `DB_LINK_CONNECTION` to dummy aiomysql DSN before imports, builds aiosqlite in-memory engine, `Base.metadata.create_all`, monkeypatches `async_session` in each repository module. Never import `bot.main` (creates `Bot(token=None)`); mount `mcp_router` on a fresh `FastAPI()`.
   - `test_mcp_scope.py`: seed chats (partner_mp, media_mt, unspecified, none-in-chats messages) + messages; key with `["partner_mp"]` sees only partner_mp chat in list/get/search; explicit `media_mt` chat_id → "chat not found"; `["all"]` sees registered incl. unspecified but not unregistered group; `["bogus"]` / `[]` sees nothing; changing a chat flag after key creation changes visibility (live scope).
   - `test_mcp_auth_audit.py` (TestClient): missing / wrong / revoked / expired token → 401 + audit row; valid → 200 + `ok` audit row with `result_count`; `McpAuditRepository.add` patched to return `None` → error response and no message data in body; `Origin` header → 403.
   → verify: `.venv/bin/python -m pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q` green (sqlite cannot run MySQL `on_duplicate_key_update`; upsert covered by manual step 2).
10. Ops (owner/devops, not code): BotFather `/setprivacy` → Disable, then **remove and re-add** bot to groups (privacy change applies on re-join) or make bot admin in each group; add nginx `location {MCP_PATH}` proxy to `team-group-bot:8019` with `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`, `proxy_buffering off`; `graphify update .` after merge; impl note `.claude/skills/impl-mcp-endpoint/SKILL.md`.

## 8. Env vars added
- `MCP_PATH` (default `/mcp`) — read in `bot/main.py` / `bot/domain/mcp/server.py`.
- `MESSAGE_RETENTION_DAYS` (default `90`, `<=0` disables) — `bot/main.py`.
Public URL in the "claude mcp add" hint reuses existing `WEBHOOK_BASE_URL`.

## 9. Risks / edge cases for tester
- Capture must not change routing: outer middleware must always `return await handler(...)`; a missing `return` silently breaks `main_` new-user flow and `events_.handle_migration`.
- Without privacy disabled / admin rights bot only sees commands and replies → table looks "empty". Ops step 10.
- `drop_pending_updates=True` in `lifespan`: messages sent during a redeploy are lost permanently (accepted; existing behavior).
- Group → supergroup migration: new chat id; old messages stay under old `group_id`, which no longer matches a `chats` row → unreachable via MCP, expire by retention. Not rewritten (message_id sequences can collide after migration).
- Redelivery after edit: plain `message` upsert must not overwrite edited text (step 2).
- Messages from groups not in `chats` are stored but never exposed (even with `["all"]`).
- Timezones: `sent_at` naive UTC, while existing `chats.time`/`broadcasts.sent_at` use local `datetime.now()`; UI shows "UTC" explicitly.
- `media_mt` vs `media_mp` confusion in toggles/labels — test both.
- Payload caps: very long chats → `next_cursor` must advance without duplicates/skips when many rows share one `sent_at` (keyset on `(sent_at,id)`).
- `LIKE` escaping: query `%` or `_` must match literally.
- Audit fail-closed: DB hiccup makes MCP unusable (by design); bot itself unaffected.
- `X-Forwarded-For` spoofing: only rightmost hop trusted; if nginx does not set header IP = docker gateway.
- Claude Code on 401 may try OAuth discovery (`/.well-known/...` → 404) and show auth error — expected for revoked keys.
- Secret visible in Telegram history until "скрыть" pressed; Telegram clients may cache notification preview.
- No rate limiting on bad tokens (128-bit+ secrets; every attempt audited). Volume: audit grows forever by default.
- MemoryStorage FSM: restart mid key-creation loses the draft (no key created).
- UI files are concurrently being translated (Ukrainian → Russian): merge conflicts in `admin_.py` / `main_chat.py`.

## 10. Decided without asking
- Hand-rolled MCP endpoint instead of `mcp` SDK — SDK needs pydantic ≥2.7.2 (≥2.11 current), aiogram 3.4.1 caps pydantic <2.6; upgrading aiogram untested = high risk.
- Capture via `dp.message`/`dp.edited_message` **outer** middleware — only hook that sees every message once before filters without consuming it; `main_` handler is gated by `IsGroupUser`.
- `edited_message` added explicitly to `allowed_updates` instead of a dummy handler — no fake handler, intent visible.
- Edits update the same row (`is_edited`, `edit_date`), no edit history — enough for reading chats; history would multiply volume.
- Dedupe via unique `(group_id, message_id)` + `INSERT ... ON DUPLICATE KEY UPDATE`.
- Service messages (joins, pins, migrations) stored too with their `message_type` — "all messages" requirement; cheap.
- Scope stored as JSON column on `mcp_keys` (not link table) — read in the same row lookup as auth, written atomically with the key, scope is immutable after creation, validated against `TypeOfChats` in Python; no query ever needs "keys by category".
- Scope evaluated live at query time against current `chats` flags — re-categorizing a group immediately grants/removes access; no snapshot to drift.
- `"all"` = every chat registered in `chats` (incl. unspecified), never unregistered groups.
- Keys are independent of their creator: removing the creating admin does not revoke keys (keys are for third parties, labeled); any ADMIN sees and can revoke all keys.
- Audit fail-closed: if the audit insert fails, the call is refused and data discarded.
- Audit logs every request incl. `initialize`/`tools/list`, not only tool calls — "every MCP call".
- sha256 (not bcrypt) for token hash — tokens are 256-bit random, slow hashing adds nothing; enables indexed lookup.
- `last_used_at` throttled to 60 s.
- Out-of-scope and nonexistent chat return identical "chat not found".
- Retention batch delete daily, 10k rows per statement — avoids long table locks.
- `Origin` header present → 403; only Claude Code (no Origin) is a target client.
- No FULLTEXT index for now — `LIKE` within scoped, time-bounded window.
- Tests on aiosqlite + TestClient (dev-only deps), runtime `requirements.txt` untouched.

## 11. Open decisions (defaults used in this plan — adjustable by owner)
1. Message retention: **90 days** (`MESSAGE_RETENTION_DAYS`).
2. Audit log retention: **keep forever** (no cleanup loop for `mcp_audit_log`).
3. Author name + username exposed via MCP: **yes**.
4. Who can create/revoke keys and read the audit log: **only ADMIN role** (`MCP_KEY_ROLES`).
5. Key expiry default: **none** preselected; creator may pick 30 / 90 days.
6. Capture stores all groups, not only those in `chats` or in some category: **yes (all)** — owner-confirmed; listed for privacy awareness.

## Review round 1

Verdict: accept with changes (plan-reviewer, 2026-11-25). Summary of findings (full text was delivered in session; key points kept verbatim in meaning):

What to change:
1. Disabling privacy mode floods admins with "new user" notifications (`main_` / `push_new_user_added` fires for every first-time writer in every group). Move privacy step to the beginning, first check current mode (getMe `can_read_all_group_messages`), decide on notifications (owner decision 1).
2. Response too big for Claude Code: default MCP tool output limit ≈25k tokens (~50–60 KB Cyrillic). Lower to ~40 KB and 100 messages per call; don't repeat author name/username per message — return a separate authors dictionary.
3. Unauthenticated requests can fill the audit log without bound (every bad-token request = row, kept forever, same DB as bot). Rate limit at nginx (e.g. 10/min per IP); log unknown-key denials aggregated once per minute per IP with a counter; audit retention 365 days.
4. Message text can leak into the error log: `log_exc_with_try_except` logs the exception string, which for SQLAlchemy includes bound parameters. Set `hide_parameters=True` on the engine; add check that with DB down no message text appears in logs.
5. `on_duplicate_key_update(id=inserted.id)` on an autoincrement PK is unreliable — on duplicate keep current value (`id = chat_messages.id`) instead, otherwise keyset cursor breaks.
6. No way to notice missing capture (group where privacy/admin didn't take effect → silently empty). Add "messages in last 24 h / last message at" to group view and statistics; log at startup whether bot reads all group messages; separate list of groups with 0 messages for 3 days.
7. Missing blind spots: bot doesn't receive its own messages (broadcasts not archived), doesn't see other bots' messages, gets no deletion events (deleted messages stay for the whole retention), reply structure lost. Add `reply_to_message_id` column; add these to risks; warn owner about deletions.
8. Messages lost on each redeploy due to `drop_pending_updates=True`; CI builds on every push to main/dev (owner decision 3).
9. In aiogram 3.4.1 `Message.edit_date` is an int (unix seconds), not datetime — convert explicitly, otherwise every edit upsert fails silently.
10. Tests as described won't work: SQLite doesn't autoincrement BigInteger PK (use `BigInteger().with_variant(Integer, "sqlite")`); in-memory SQLite needs a single shared connection (StaticPool); Starlette TestClient runs app in another thread — use httpx `AsyncClient` + `ASGITransport` in the same loop; DB URL in tests must be force-overwritten, not setdefault (risk of hitting real DB).
11. Step 6 can't be verified before step 7 (no key UI yet) — swap or add temporary key creation for verification.
12. Internal errors must never put exception text into the response (could contain other chats' data).
13. X-Forwarded-For rightmost hop correct only with exactly one proxy; verify after launch with a request from a known IP.

Answers to open decisions: 1) 90 days, re-check table size after 2 weeks; 2) audit 365 days, cleaned by same daily task; 3) authors yes; 4) only ADMIN; 5) default expiry 90 days preselected; 6) recommend capturing only groups registered in `chats` (owner already chose all).

Missing entirely: notice to group members that messages are stored and exposed via AI assistant (legal/reputation, owner decision); category change retroactively exposes whole history — say so on key creation screen ("all" key also gets every newly added group); notify all ADMINs on key create/revoke; rate limit for valid keys; DB timeout 2–3 s for capture so a hung DB doesn't stall all update handling; verify utf8mb4 in real server DSN.

Owner decisions proposed: (1) new-user notification flood — recommend mute notifications for the first week but keep recording users; (2) deleted messages / history exposure — recommend accept + honest notice in groups; (3) restart gaps — recommend stop dropping pending updates.

Env vars: MCP_PATH (must match nginx), MESSAGE_RETENTION_DAYS (≤0 disables cleanup), WEBHOOK_BASE_URL (now also used in the connect command), DB_LINK_CONNECTION (must include utf8mb4 charset).

## Owner answers received during review (2026-11-25, to be folded into v2 amendments)
- Retention: 180 days, BUT always keep the latest 100 messages per chat even if older.
- Store all message types; persist only text (text or caption) + message type (+ author, time). No media files, no file_id, no photo/voice/video analysis, no voice transcription, no duration/sticker emoji.

# v2 amendments (2026-11-25, after review)
Supersede conflicting parts of sections 1–11 above.

## Owner decisions
- Message retention: delete messages older than **180 days**, BUT always keep the **latest 100 messages per chat** regardless of age. Env `MESSAGE_RETENTION_DAYS` default 180; new `MESSAGE_KEEP_LAST_PER_CHAT` default 100. Retention query: delete rows with `sent_at < cutoff` AND not among the newest 100 of their `group_id` (e.g. per-group: find the 100th newest `(sent_at,id)` and delete older-than-cutoff rows strictly below it; batched, 10k per statement).
- Stored content: all message types are captured, but only `text` (text or caption) + `message_type` + author + time + thread + reply_to. No media files, no file_id, no durations, no sticker emoji, no transcription, no image analysis.
- Restart gaps: **stop dropping pending updates** — `set_webhook(..., drop_pending_updates=False)`; remove the `delete_webhook()` call before it or call it without dropping. Accepted side effect: callbacks/commands sent during downtime are processed after restart.
- Key expiry: **no expiry by default**; keys live until revoked in the bot. Remove the expiry step from the key-creation FSM (keep nullable `expires_at` column for future, always NULL now).
- Audit log retention: **365 days**, cleaned by the same daily retention task (`AUDIT_RETENTION_DAYS` default 365).
- Authors (name + username) exposed: yes. Only ADMIN role manages keys and reads the audit log. Capture covers all groups (owner-confirmed).
- New-user notifications: **left unchanged** for now. Startup logs `getMe().can_read_all_group_messages`; if a notification flood appears after privacy is disabled, muting is a follow-up decision.
- No notice to group members (owner decision).

## Accepted review changes
1. Ops order: privacy check/disable is step 0 (before deploying capture). Startup log line with `can_read_all_group_messages`.
2. MCP output limits: `get_messages` limit default/max 100, payload cap ~40 KB → `next_cursor`; `search_messages` max 50. Response = `{"authors": {author_id: {username, first_name}}, "messages": [... author_id only ...], "next_cursor"}`.
3. Rate limiting: nginx `limit_req` 10 r/min per IP for unauthenticated + app-level per-key limit 60 calls/min (in-memory, single process) → JSON-RPC error, audited `denied/rate_limited`. Unknown/missing-token denials aggregated: at most one audit row per IP per minute with `count` (add `hits` Integer column default 1 to `mcp_audit_log`).
4. Engine `create_async_engine(..., hide_parameters=True)` in `bot/data/db_connection.py`; capture middleware never logs text. Verify: with DB down, log contains no message text.
5. Upsert on duplicate: never touch `id` — for plain `message` use `on_duplicate_key_update(message_id=chat_messages.c.message_id)` (current value, no-op); for `edited_message` update text/type/edit_date/is_edited only.
6. Capture health: group view and statistics show "сообщений за 24 ч / последнее сообщение в …"; statistics lists registered groups with 0 captured messages in 3 days.
7. Add `reply_to_message_id` BigInteger NULL to `chat_messages` and to tool output. Risks section additions: bot doesn't receive its own messages (broadcasts not archived) nor other bots' messages; deletions are never seen — deleted messages stay until retention.
8. `edit_date` is int unix seconds in aiogram 3.4.1 → `datetime.utcfromtimestamp(edit_date)`; `message.date` is datetime (tz-aware) → convert to naive UTC.
9. Tests: `id` columns `BigInteger().with_variant(Integer, "sqlite")`; aiosqlite engine with `StaticPool` + `connect_args={"check_same_thread": False}`; httpx `AsyncClient(transport=ASGITransport(app))` instead of TestClient; conftest force-sets `os.environ["DB_LINK_CONNECTION"]` (not setdefault) to a dummy DSN before any bot import. MySQL-only options (`mysql_charset`, JSON) must not break sqlite create_all (JSON is supported by SQLAlchemy sqlite; mysql_* kwargs ignored).
10. Step order: implement key creation in repository first and provide a one-off CLI/script-free verification via pytest fixture; manual end-to-end check (`claude mcp add`) moves after step 7 (UI).
11. Internal errors: response is always generic `"internal error"`; exception text only to server log (with hidden params).
12. After launch: one request from a known IP, compare `client_ip` in audit.
13. Key creation screen warns: «Доступ проверяется в момент запроса: если группе позже добавят категорию, ключ увидит и её прошлую историю. Ключ «Все группы» увидит и новые группы.» All ADMINs get a notification on key create/revoke (label, categories, who).
14. Capture DB call wrapped in `asyncio.wait_for(..., timeout=3)`; on timeout log and continue.
15. Verify utf8mb4 in the real server DSN (owner/devops, manual).

## Rejected
- Notice to group members about message storage — owner: "не попереджаємо, взагалі нічого не пишемо".
- Default key expiry 90 days — owner: keys without expiry, revocable in the bot.
- Capture only groups registered in `chats` — owner chose all groups.
- Mute new-user notifications for first week — not now; owner believes users are already recorded; revisit only if a flood happens.

## Decided without asking
- MCP payload cap 40 KB / 100 messages — Claude Code default tool-output limit ≈25k tokens.
- Authors returned as a separate dictionary — smaller payload.
- Aggregated logging of unauthenticated denials + nginx rate limit — keeps audit table bounded under scanning.
- Per-key rate limit 60/min in-memory — single container, no Redis needed.
- `hide_parameters=True` globally — one switch protects message text in all DB error logs.
- Keep `reply_to_message_id` — cheap, preserves conversation structure.
- Capture-health indicators in group view/statistics — only way to notice silent capture gaps.
- Capture DB timeout 3 s — hung DB must not stall all bot updates.
- Captions stored as text — they are user-written text attached to media.

## Review round 2

Verdict: accept with changes (plan-reviewer, 2026-11-25). All 13 round-1 findings and "missing" items are addressed by v2. Verified against code/libs: `delete_webhook()` in lifespan has no drop flag → only `set_webhook(drop_pending_updates=...)` matters; `edit_date` int / `date` tz-aware datetime conversions correct; `on_duplicate_key_update(message_id=chat_messages.c.message_id)` compiles to a no-op that doesn't touch `id`; `hide_parameters` supported; single uvicorn process → in-memory rate limiter valid; sqlite test setup (StaticPool, with_variant, ASGITransport, JSON) compatible with installed versions.

What to change:
1. nginx 10 r/min per IP breaks legitimate use (Claude connect = 3 requests, each page = 1; one "read a month" conversation = 15–20/min; office shares one IP). → Strict 10/min only for requests WITHOUT `Authorization` header (nginx `map $http_authorization`); requests with a token get a soft guard (e.g. 5 r/s, burst 20). Real per-key limit stays 60/min in app.
2. Retention "180 days but keep latest 100": groups with <100 messages have no 100th row — if treated as "no bound", ALL old messages of small groups get deleted. → Explicit: group with ≤100 messages → skip. Comparison "older than 100th" written as `sent_at < t OR (sent_at = t AND id < i)` (index-friendly), not a row-value tuple compare. Candidate groups = only groups having rows older than cutoff. Write retention portable (works on sqlite) and add automated test: group with 50 old msgs, group with 150 old msgs, group with new+old → exactly the right rows remain.
3. Rate-limit denials for a key are audited one row per request → aggregate like unauthenticated: first request per (key/IP, minute) inserts a row, further ones increment `hits`; in-memory entries older than 2 min evicted; insert failure → "service unavailable" (fail-closed).
4. `asyncio.wait_for(timeout=3)` cancels a DB call mid-flight; the connection may return to the pool broken and fail the next unrelated bot query. → On timeout invalidate/close the connection instead of returning it, or set driver-level timeouts (aiomysql `connect_timeout` / read timeout) instead of wait_for. Add check: DB hangs 10 s → after recovery admin menu works without errors.
5. Contradictions between sections 1–11 and v2 — fix the text: schema lacks `reply_to_message_id` and audit `hits`; tools still say 500 msgs / 200 KB; key-creation steps still include expiry; risks still say "restart loss accepted" and "no rate limiting"; env list lacks new vars; audit reasons lack `rate_limited`; "every call is audited" is no longer literally true for aggregated denials — state it.
6. Pre-UI verification of the MCP endpoint is vague → state: before key UI, verification is automated tests only; manual curl/Claude Code check after the UI step.
7. Search case sensitivity differs (MySQL utf8mb4_unicode_ci case-insensitive for Cyrillic, sqlite LIKE case-sensitive for non-ASCII) → tests use same case; note it.

Answers to open decisions: retention 180+100 accepted with fix #2; audit 365 days OK; authors yes; ADMIN only; no-expiry keys and all-groups capture are owner decisions, not challenged.

Missing: messages of groups the bot left (and pre-migration old group ids) keep their latest 100 forever under the keep-100 rule — add to risks or apply keep-100 only to groups where the bot currently is; manual check after first deploy that messages sent during downtime are archived and that callbacks pressed during downtime don't re-trigger broadcasts; data volume estimate (~0.5 KB/row incl. indexes; 10k msgs/day × 180 d ≈ 1.8M rows ≈ 1 GB) — measure after 2 weeks; numbers (60/min, 40 KB, 3 s, 100) are estimates — review after first week of usage data; check current privacy mode BEFORE disabling, not from post-deploy log.

Owner decisions proposed: (1) rate limit — strict only without token (recommended); (2) groups bot left — keep-100 only for groups where bot currently is (recommended); (3) automated test for retention (recommended).

Env vars: MESSAGE_RETENTION_DAYS (180, ≤0 disables), MESSAGE_KEEP_LAST_PER_CHAT (100, add to .env.example), AUDIT_RETENTION_DAYS (365, add to .env.example), MCP_PATH (must match nginx), DB_LINK_CONNECTION (utf8mb4).

# v3 amendments (2026-11-25, after review round 2)
Supersede conflicting parts of sections 1–11 and v2. Where sections 1–11 disagree with the "Final values" below, Final values win.

## Owner decision
- Keep-last-100 rule applies ONLY to groups where the bot currently is (rows present in `chats`). Groups not in `chats` (bot removed, pre-migration old ids, never registered): plain 180-day cutoff, no keep-100 protection.

## Final values (resolve contradictions with sections 1–11)
- Schema `chat_messages` adds `reply_to_message_id` BigInteger NULL. Schema `mcp_audit_log` adds `hits` Integer NOT NULL default 1. Audit `reason` values add `rate_limited`.
- Tools: `get_messages` limit default 100, max 100, payload cap ~40 KB; `search_messages` max 50; output `{authors:{...}, messages:[...], next_cursor}`. The 500 / 200 KB numbers in section 6 are void.
- Key creation FSM: label → categories → confirm → secret. No expiry step (section 7 step 7 "expiry" removed); `expires_at` always NULL.
- Risks section: "messages lost on redeploy — accepted" is void (pending updates are now kept); "no rate limiting" is void.
- Env vars (full list): `MCP_PATH`=/mcp, `MESSAGE_RETENTION_DAYS`=180 (≤0 disables message cleanup), `MESSAGE_KEEP_LAST_PER_CHAT`=100, `AUDIT_RETENTION_DAYS`=365; all added to `.env.example`.
- Audit completeness wording: every MCP request is audited; unauthenticated denials and rate-limit denials are aggregated to one row per (IP or key, minute) with `hits` counter — not one row per request.

## Accepted review changes (round 2)
1. nginx: strict `limit_req` 10 r/min per IP only for requests WITHOUT `Authorization` header (`map $http_authorization $mcp_noauth_key { "" $binary_remote_addr; default ""; }`); requests with a token get a soft zone 5 r/s burst 20. App per-key limit 60/min unchanged.
2. Retention algorithm (portable SQL, works on MySQL and sqlite):
   - cutoff = utcnow − MESSAGE_RETENTION_DAYS.
   - Candidate groups = DISTINCT `group_id` having rows with `sent_at < cutoff`.
   - For candidate group in `chats`: if total rows ≤ KEEP_LAST → skip. Else find the KEEP_LAST-th newest row `(t, i)` ordered by `(sent_at DESC, id DESC)`; delete rows where `sent_at < cutoff AND (sent_at < t OR (sent_at = t AND id < i))`, batched (MySQL `mysql_limit` 10k loop; on sqlite delete by id subquery batch).
   - For candidate group NOT in `chats`: delete rows `sent_at < cutoff`, batched.
   - Audit cleanup: delete `mcp_audit_log` rows `created_at < utcnow − AUDIT_RETENTION_DAYS`, batched.
   - Automated tests: group in chats with 50 old msgs (all kept); group in chats with 150 old msgs (newest 100 kept, 50 deleted); group in chats with mixed new/old (new kept, keep-100 respected); group NOT in chats with old msgs (all old deleted); ties on `sent_at`.
3. Rate-limit denials aggregated like unauthenticated ones: in-memory map `(kind, key_id|ip, minute) → audit_row_id`; first hit inserts row (`hits=1`), next ones `UPDATE hits = hits + 1`; entries older than 2 min evicted on access; insert failure → fail-closed ("service unavailable").
4. Capture DB timeout: capture opens its own `async with engine.connect() as conn`, runs the upsert inside `asyncio.wait_for(..., timeout=3)`; on `TimeoutError` (or any error) call `await conn.invalidate()` before leaving the block, so a cancelled connection is discarded, never returned to the pool. Also add engine `connect_args={"connect_timeout": 3}`. Verify: DB paused 10 s → after recovery admin menu works with no errors.
5. Verification order: before key UI (steps 5–6) verification = automated tests only; manual curl + `claude mcp add` check after step 7.
6. Tests for search use same-case queries; note: MySQL utf8mb4_unicode_ci is case-insensitive for Cyrillic, sqlite LIKE is not.

## Post-launch checks (ops/manual)
- Before disabling privacy: check `getMe().can_read_all_group_messages` and whether the bot is admin in groups (predicts the new-user notification wave).
- After first deploy: send a message to a test group while the bot is stopped → it appears in archive after restart; press a broadcast button during downtime → no duplicate broadcast.
- One MCP request from a known IP → `client_ip` in audit matches.
- After 2 weeks: measure `chat_messages` size (estimate ~0.5 KB/row; 10k/day × 180 d ≈ 1 GB). After 1 week: review limits (60/min, 40 KB, 100 msgs) against audit data.
- Add to risks: messages of groups not in `chats` are deleted after 180 days without keep-100.

## Decided without asking
- Strict nginx limit only for token-less requests — legitimate sessions make 15–20 requests/min.
- Retention skips groups with ≤100 messages and uses index-friendly comparison — avoids silently wiping small groups.
- Automated retention tests — only irreversible-delete code path in the plan.
- Aggregated rate-limit audit rows — bounded audit size under any client behaviour.
- Driver-level timeout + connection invalidation instead of plain wait_for — cancelled query must not poison the pool.

## Review round 3

Verdict: ACCEPT (plan-reviewer, 2026-11-25). All 7 round-2 findings addressed. Verified: daily health check removes groups from `chats` when bot is kicked (and also on "chat not found") → keep-100 correctly stops applying; tie-break on id gives exactly 100; aggregated audit rows bounded. Minor text-level fixes listed below (folded into v4). No new owner decisions.

# v4 amendments (2026-11-25, after review round 3) — technical, no owner input needed
1. Retention delete: ONE statement form for MySQL and sqlite — `delete(...).where(...).with_dialect_options(mysql_limit=BATCH)` (sqlite ignores the limit → deletes all in one go; same result). Loop while rowcount == BATCH. No separate sqlite branch — tests exercise the production code path.
2. Capture write: `async with engine.begin() as conn` (commit on exit). Manual verify of capture step checks real rows in MySQL (`SELECT COUNT(*) FROM chat_messages WHERE group_id=...`), not just absence of errors.
3. Capture timeout: wrap the WHOLE block (connection checkout incl. `pool_pre_ping`, upsert, commit) in `asyncio.wait_for(..., 3)`. Explicit `conn.invalidate()` optional — SQLAlchemy already invalidates a connection on cancellation.
4. `connect_args={"connect_timeout": 3}` applies to the whole bot engine — acceptable (DB on same host). Record in Decided without asking.
5. nginx (shared container, not in this repo): both `limit_req_zone` directives go to the http-level config; zone A (no Authorization) 10r/m, zone B (with Authorization, key `$binary_remote_addr`) 5r/s burst=20; `limit_req_status 429;` in the location. Post-launch check: 11 token-less requests within a minute → the 11th gets 429.
6. Final values additions: section 7 step 2 `id=inserted.id` is void (v2 #5 wins); section 7 step 4 plain "delete older than N days" is void (v3 retention algorithm wins); section 11 "90 days" void (180); capture uses its own connection (v4 #2/#3), not a `@log_exc_with_try_except` repository method. Affected files also include: `bot/data/db_connection.py` (hide_parameters, connect_timeout), group view `show_.py` and `statistic_.py` (capture-health indicators), admin notifications on key create/revoke (`mcp_keys_.py` via `AdminRepository.get_admins`), `tests/test_retention.py`.
7. `MESSAGE_KEEP_LAST_PER_CHAT <= 0` → no keep-last protection (plain cutoff). Retention tests add groups with exactly 100 and 101 old messages.
8. If incrementing `hits` on an existing aggregated audit row fails → ignore (row already exists, denial still returned).
9. Risks: group removed from `chats` by health check on "chat not found" also loses keep-100 protection (rare, only old messages affected). Cost: no paid services added; only DB storage (~1 GB / 6 months at 10k msgs/day); AI usage is paid by key holders.

## Decided without asking (v4)
- Single delete statement for both DBs — tests cover the irreversible path.
- `engine.begin()` for capture — avoids silent rollback.
- Timeout covers checkout+write+commit — pre-ping on hung DB must not stall updates.
- Global `connect_timeout=3` — DB is local; fine for all bot queries.
- `MESSAGE_KEEP_LAST_PER_CHAT<=0` disables protection — explicit edge behaviour.
