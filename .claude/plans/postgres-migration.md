# Plan: MySQL → PostgreSQL 16 (container in bot compose) + full data copy

Status: draft for /review-plan. Architect, 2026-11-25. Owner decisions so far: Postgres as a container in the bot's own prod compose (`prod/docker-compose.yaml`); old MySQL bot schema kept ~1 week as fallback, then the owner drops ONLY this bot's schema (the MySQL server hosts other DBs — never touched).

## 0. Verified facts (from the tree, 2026-11-25)
- `bot/data/db_connection.py`: `create_async_engine(DB_LINK_CONNECTION, pool_pre_ping=True, pool_recycle=28000, hide_parameters=True, connect_args={"connect_timeout": 3})`. `connect_timeout` is aiomysql-only.
- `alembic/env.py`: URL from `DB_LINK_CONNECTION`, async engine NullPool, `target_metadata=Base.metadata`. Nothing MySQL-specific.
- `bot/data/models.py`: `MYSQL_ARGS` (utf8mb4) on `chat_messages`, `mcp_keys`, `mcp_audit_log`; `BigId = BigInteger().with_variant(Integer, "sqlite")`; Booleans `server_default="0"` (valid PG boolean literal, stays); `UserModel`: `user_id` primary_key=True + `UniqueConstraint("user_id","group_id", name="user_group_unique")`.
- Legacy dump `team_group_db.sql`: real MySQL `users` has **no PRIMARY KEY**, only `UNIQUE KEY user_group_unique (user_id, group_id)` — one user can have one row per group. Model PK is wrong for Postgres.
- MySQL revisions: `d8cd90ead460` (only alter_column CHAR→VARCHAR, MEDIUMTEXT→TEXT, tinyint NOT NULL), `002` (`a1b2c3d4e5f6`, UPPER(role) — under `_ci` collation condition always true → roles already uppercase), `003` (`b2c3d4e5f6a7`, broadcasts), `004` (`c3d4e5f6a7b8`, chat_messages/mcp_keys/mcp_audit_log, NOT on server). None create base tables → cannot build an empty DB. Squash required.
- `MessageRepository.py`: `mysql_insert(...).on_duplicate_key_update` (no-op branch `message_id=message_id`); `_delete_batched` and `delete_old_audit` use `.with_dialect_options(mysql_limit=BATCH)`, BATCH=10000.
- `bot/domain/mcp/tools.py:221`: `M.text.like(..., escape="\\")` — case-insensitive on MySQL only via collation.
- `_chat_id` returns `str(value)` with no NUL check; `tests/test_mcp_adversarial.py:55-56` passes `"-1\x00"` expecting "chat not found" → on PG the bind fails → internal error. Same test passes `"-1 "` — MySQL PAD SPACE would match `-1` (leak hidden by sqlite); PG varchar exact → stricter (good).
- `AdminRepository.get_admins` filters `role == "ADMIN"`; on MySQL `_ci` also matched `admin`; PG exact.
- Ordering by `ChatModel.title`: `MessageRepository.py:126` (silent_groups), `tools.py:150` (list_chats). MySQL case-insensitive order; PG per collation.
- Upsert never executed against a real DB in tests (`test_message_capture.py` monkeypatches; `test_upsert_sql_mysql` only compiles).
- `tests/conftest.py`: forces `DB_LINK_CONNECTION=mysql+aiomysql://dummy...`; per-test `sqlite+aiosqlite://` StaticPool + `create_all`; monkeypatches `db_connection.engine/async_session` and `async_session` in all `bot.data.repositories.*`. 173 passed.
- Deps: `aiomysql==0.2.0`, `PyMySQL==1.1.1`, `SQLAlchemy==2.0.40`; dev: pytest, pytest-asyncio, httpx, aiosqlite. Dry-run: `asyncpg==0.30.0`/`0.32.0` resolve. pgloader not installed; docker 29.1.3 local. Only `conftest.py` references aiomysql/PyMySQL besides db URL.
- `bot/main.py` lifespan: `delete_webhook()` (no drop) then `set_webhook(..., drop_pending_updates=False)` — downtime updates delivered after restart. Retention loop 120 s after start, then daily.
- `bot/domain/mcp/server.py:32` `_client_ip` = rightmost X-Forwarded-For; caller-controlled without a trusted proxy.
- Repo `docker-compose.yaml`: service `team-group-bot`, `build: .`, `8019:8019`, external `nginx-network`.
- `prod/docker-compose.yaml` (real server config): service/container `team-group-bot`, image `artemqq5/team-group-bot:latest`, `env_file: .env`, entrypoint `sh /entrypoint.sh`, `extra_hosts host.docker.internal:host-gateway`, `ports "8019:8019"` (all interfaces), volumes `./logs`, `./entrypoint.sh`, json-file 10m×3. No networks, no build. `prod/entrypoint.sh` byte-identical to repo `entrypoint.sh`.
- `.gitignore`: `.env` (any depth, incl. `prod/.env`), `.env.*` + `!.env.example`, `pgdata/`, `prod_config/*` exceptions; nothing for `prod/`. `git check-ignore` → `prod/docker-compose.yaml`, `prod/entrypoint.sh` not ignored.
- `.dockerignore` lacks `pgdata/`. CI only builds/pushes `:latest` on push to main/dev; no tests.

## 1. What changes and why
DB moves from host MySQL (aiomysql) to `postgres:16-alpine` in the bot's compose, private network, not published. Schema squashed to one PG baseline matching models, `users` PK fixed to (user_id, group_id). Four MySQL-specific paths ported: upsert, batched delete, case-insensitive search, NUL-byte inputs. Tests move to real Postgres. One-off script copies all MySQL data, verifies, resets sequences. Cutover ships together with the already-built MCP feature.

## 2. Affected files
- `requirements.txt`: add `asyncpg==0.30.0`; keep `aiomysql==0.2.0`, `PyMySQL==1.1.1` until MySQL drop (copy script only).
- `requirements-dev.txt`: remove `aiosqlite`.
- `bot/data/db_connection.py`: `connect_args={"timeout": 3, "command_timeout": 30}`.
- `bot/data/models.py`: remove `MYSQL_ARGS` (3 uses); `BigId` → `BigInteger`; `UserModel` `group_id` also `primary_key=True`, drop redundant UniqueConstraint; `Index("ix_broadcasts_sent_at","sent_at")` on BroadcastModel.
- `bot/data/repositories/MessageRepository.py`: PG upsert, portable batched delete.
- `bot/domain/mcp/tools.py`: `ilike`; NUL guard in `_chat_id` and `query`.
- `bot/domain/middlewares/MessageCapture.py`: strip `\x00` in `_values`.
- `bot/domain/mcp/server.py`: only if PG tests show audit insert fails on NUL in tool_name/method/client_ip → strip before `McpAuditRepository.add`.
- DELETE `alembic/versions/d8cd90ead460_initial.py`, `002_…`, `003_…`, `004_…`.
- NEW `alembic/versions/001_pg_baseline.py` (fresh id, `down_revision=None`).
- NEW `scripts/__init__.py`, `scripts/mysql_to_pg.py`.
- `tests/conftest.py`: Postgres fixture. `tests/test_message_capture.py`: replace `test_upsert_sql_mysql`. Others only if PG exposes sqlite assumptions.
- NEW `tests/test_pg_dialect.py`: real upsert, ILIKE Cyrillic, NUL inputs, users composite PK, migration-vs-models diff, copy-script transforms.
- Repo `docker-compose.yaml`: postgres service + private network.
- `prod/docker-compose.yaml`: target prod compose (step 9). `prod/entrypoint.sh`: no change.
- `.env.example`: new vars. `.dockerignore`: add `pgdata/`. `.gitignore`: no change needed.
- Orchestrator docs afterwards: CLAUDE.md, agents, skills `sqlalchemy-async`, `alembic-migrations`, `impl-mcp-chat-archive`; new `impl-mysql-to-pg`.

## 3. Schema / contract changes
- `users`: PRIMARY KEY (user_id, group_id) `users_pkey`; same uniqueness as live MySQL. No code uses `session.get(UserModel, …)`.
- PG types: String(n)→varchar(n), Text→text, Boolean→boolean, DateTime→timestamp without tz (naive UTC), JSON→`json` (not jsonb: accepts `\u0000`), Integer/BigInteger PK→serial/bigserial.
- Baseline includes all tables, indexes, named constraints from models (`uq_chat_messages_group_msg`, `ix_chat_messages_group_sent`, `ix_chat_messages_sent`, `ix_broadcasts_sent_at`, mcp_keys unique token_hash, `ix_mcp_audit_key_created`, `ix_mcp_audit_created`, admins.role default `'ADMIN'`).

## 4. Implementation order (developer)
1. **Deps** → verify: pip install both files; `python -c "import asyncpg"`.
2. **Test Postgres + conftest first.** Dev container `docker run -d --name tgb-test-pg -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=test -e POSTGRES_DB=tgb_test postgres:16-alpine`. conftest: dummy `DB_LINK_CONNECTION=postgresql+asyncpg://dummy:dummy@127.0.0.1:1/dummy`; `TEST_DATABASE_URL` default `postgresql+asyncpg://postgres:test@127.0.0.1:55432/tgb_test`; abort unless DB name contains `test`. Session fixture: `DROP SCHEMA public CASCADE; CREATE SCHEMA public` + Alembic `upgrade head`. Function `db` fixture: new NullPool engine per test, `TRUNCATE … RESTART IDENTITY CASCADE`, same monkeypatching. Unreachable → `pytest.exit("start tgb-test-pg …")`. → verify: suite runs on PG; record expected red tests.
3. **Models** → verify import.
4. **Baseline `001_pg_baseline.py`**, hand-written, mirroring models (column `title_gruop` stays); delete old revisions; downgrade drops all. → verify: one head; `test_migration_matches_models` (`compare_metadata` == []); `downgrade base && upgrade head` OK.
5. **db_connection** → verify: no aiomysql import in `bot/`.
6. **MessageRepository**: `pg_insert`; edited → `on_conflict_do_update(constraint="uq_chat_messages_group_msg", set_={text, message_type, has_media, edit_date, is_edited from excluded})`; else `on_conflict_do_nothing(constraint=...)`. Batched delete: `delete(T).where(T.id.in_(select(T.id).where(where).limit(BATCH)))`, loop while rowcount == BATCH. → verify real-DB tests: insert → redelivery (no change, id same) → edit (text updated, id same) → non-edit redelivery after edit (text not reverted); retention green; BATCH=3 loop test.
7. **tools.py**: `ilike`; `_chat_id` with `\x00` → `ToolError(NOT_FOUND,"out_of_scope")`; query with `\x00` → bad_args. → verify: chat_id variants green; "Привіт Світ" found by "привіт" and "СВІТ" on postgres:16-alpine.
8. **MessageCapture `_values`**: strip `\x00` from text, author_username, author_first_name. → verify unit test; full suite on PG all green (173 + new); fix remaining strict-type/NUL failures strip-at-boundary only.
9. **Compose.** `prod/docker-compose.yaml` (base = current real file): add `postgres` (`postgres:16-alpine`, `container_name: team-group-bot-db`, `POSTGRES_DB/USER/PASSWORD: ${…:?}` required, `./pgdata:/var/lib/postgresql/data`, healthcheck `pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB` 5s/5s/10, no ports, restart unless-stopped, json-file 10m×3, `networks: [team-group-bot_network]`). Bot: `depends_on: postgres: condition: service_healthy`, `networks: [team-group-bot_network]`, keep `extra_hosts` until MySQL drop, `ports` per open decision 1. `networks: team-group-bot_network: {}`. Repo compose: same postgres + private network, keep `build: .` and `nginx-network`. `.dockerignore`: `pgdata/`. → verify: `docker compose -f prod/docker-compose.yaml config` with a scratch env file (never the real `.env`); local up: alembic creates tables.
10. **Copy script `scripts/mysql_to_pg.py`** (`python -m scripts.mysql_to_pg [--truncate]`). Source `MYSQL_SOURCE_URL`, target `DB_LINK_CONNECTION`. MySQL read-only (SELECT in `START TRANSACTION READ ONLY`, never other schemas; recommend dedicated SELECT-only user). Preconditions: target `alembic_version` = baseline; target tables empty else abort unless `--truncate` (target only). Order: admins, chats, users, broadcasts, then chat_messages/mcp_keys/mcp_audit_log only if present in MySQL. Never copy `alembic_version`. Read `select(Model.__table__)`, write chunks of 1000 in ONE PG transaction. Transforms (pure, unit-tested, counted): strip `\x00`; tinyint→bool; MySQL zero-date → None for nullable (`users.time`, `chats.time`), abort for NOT NULL; `admins.role` `.upper()` if upper is a `TypeOfAdmins` value; report (not change) ids not `-?\d+`. Sequences: `setval(pg_get_serial_sequence('<t>','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL)` for broadcasts, chat_messages, mcp_keys, mcp_audit_log. Verify after commit: row counts equal; sha256 checksum over rows sorted by PK with identical normalisation; 3 sample rows (ids/timestamps only, never text); exit ≠0 on mismatch. → verify (dev): local MySQL seeded (same user in 2 groups, zero-date, lowercase role, NUL, emoji/Cyrillic) → equal; second run without `--truncate` aborts, with succeeds; new broadcast id = max+1.
11. **Docs and env**: `.env.example`; orchestrator updates docs; `graphify update . --force`; impl note `impl-mysql-to-pg`.
12. **Post-fallback cleanup** (separate task, ~1 week): remove aiomysql/PyMySQL, script, `MYSQL_SOURCE_URL`, `extra_hosts`; owner drops only the bot's MySQL schema.

## 5. Cutover runbook (server, `prod/` dir)
Prereq: new image built/pushed. CI pushes `:latest` on main → merge only when ready to cut over.
1. Before pull: `docker tag artemqq5/team-group-bot:latest artemqq5/team-group-bot:mysql-fallback`; `cp docker-compose.yaml docker-compose.mysql.yaml`; `cp .env .env.mysql-backup` (owner); optional `mysqldump --single-transaction <bot_db>`.
2. Owner edits `prod/.env`: add `POSTGRES_DB/USER/PASSWORD` (hex/alnum, e.g. `openssl rand -hex 24`); `DB_LINK_CONNECTION=postgresql+asyncpg://USER:PASS@postgres:5432/DB`; `MYSQL_SOURCE_URL=<old DB_LINK_CONNECTION>`; new compose in place.
3. `docker compose pull`.
4. `docker compose stop team-group-bot` — downtime starts (Telegram retries ≤24h).
5. `docker compose up -d postgres`, wait healthy.
6. `docker compose run --rm --entrypoint "" team-group-bot sh -c "alembic upgrade head && python -m scripts.mysql_to_pg"` → all OK, exit 0.
7. `docker compose up -d team-group-bot` — downtime ends (~5–10 min).
8. Smoke: logs "Webhook set", no DB errors; admin `/start` menu; group counts per category and user stats match pre-cutover; broadcast history; message in test group captured; create MCP key, `list_chats`, audit row; test broadcast → new id = old max+1.
9. Rollback if 6–8 fail: `docker compose down`; restore `docker-compose.mysql.yaml` with `:mysql-fallback` + `.env.mysql-backup`; `up -d`. Loss: anything written only to PG after cutover; MCP feature disappears with old image.

## 6. Risks / edge cases
- users PK: user in 2 groups → 2 rows; rejoin same group → IntegrityError swallowed to None as before. Test both.
- Upsert: redelivery must not overwrite edited text; edit must not change id; ON CONFLICT target = `uq_chat_messages_group_msg`. Conflicts consume bigserial values (harmless).
- Retention loop with rowcount == BATCH must terminate.
- ILIKE Cyrillic on alpine/musl — proven by test on same image; fallback ICU locale only on fresh pgdata.
- Title ordering differs (codepoint vs case-insensitive) — cosmetic; tests must not assert case-mixed order.
- `get_admins` exact role match — mitigated by copy-time uppercasing.
- NUL bytes: capture stripped; MCP chat_id → not found; query → bad_args; audit strings must not produce 503.
- asyncpg strict types: int bound to varchar (or reverse) → DataError; full suite is the check; watch `callback_data.*` ids.
- `command_timeout=30` vs retention 10k-row delete.
- Compose: bot waits for healthy postgres; wrong POSTGRES_* or URL-unsafe password breaks start; `:?` fails loudly.
- `./pgdata` permissions (uid 70); `.dockerignore` entry.
- Copy: mismatch exit ≠0; re-run w/o `--truncate` aborts; zero-date in NOT NULL aborts clearly; never prints message text.
- SECURITY: `ports: "8019:8019"` publishes on 0.0.0.0 → `/mcp` and webhook reachable directly, bypassing nginx rate limits/TLS; `client_ip` attacker-chosen. Recommend `127.0.0.1:8019:8019` only after ops confirms nginx reaches bot via localhost (host nginx: safe; containerised nginx on another network: needs shared network). Tester: after change, `curl http://<public-ip>:8019/mcp` refused; via domain works.
- Backups contain message text beyond retention deletion for the backup window.

## 7. Env vars
- Changed: `DB_LINK_CONNECTION` → `postgresql+asyncpg://user:password@postgres:5432/dbname`.
- Added: `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (container + compose interpolation; must match URL).
- Temporary: `MYSQL_SOURCE_URL` (copy script; remove after MySQL drop).
- Dev only: `TEST_DATABASE_URL` (documented in tester.md and `.env.example` comment).

## 8. Backups
Host cron daily 03:30: `docker exec team-group-bot-db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > /srv/backups/team-group-bot/tgb_$(date +\%F).dump`; `find … -mtime +N -delete`. Restore: `pg_restore --clean --if-exists` with bot stopped. Manual dump before every deploy. Never copy live `./pgdata`.

## Decided without asking
- Python copy script, not pgloader — reuses models, runs in deployed image, testable, self-verifying; pgloader builds own schema, not installed, MySQL 8 auth issues.
- aiomysql/PyMySQL stay until MySQL drop — copy/re-copy need them.
- Squash to one PG baseline, delete old revisions — they can't build an empty DB and are MySQL-typed.
- `alembic_version` not copied — `upgrade head` stamps baseline.
- Composite PK (user_id, group_id) — matches real data.
- Tests on real Postgres only (docker + `TEST_DATABASE_URL`) — sqlite hid upsert/LIMIT/NUL/case bugs; no testcontainers deps; no dual-dialect maintenance.
- Test schema from baseline migration + compare_metadata test — proves squash == models.
- Batched delete via id-subquery, not ctid — portable, PK index.
- `json`, not `jsonb` — accepts `\u0000`; no JSON querying.
- Boolean `server_default="0"` kept — valid PG literal.
- `command_timeout=30`, connect `timeout=3`.
- MCP feature deploys together with the move — one cutover; revision 004 never ran on real MySQL.
- Copy uppercases known lowercase admin roles, strips NUL — preserves behaviour; counted.
- No `POSTGRES_*` defaults in prod compose — no silent weak password.
- `prod/` tracking: commit `prod/docker-compose.yaml` + `prod/entrypoint.sh`; `prod/.env` ignored by existing rule; check `git status --short prod/` before commit.

## Open decisions (owner)
1. Bot port exposure: (a) server-local only — recommended, after ops confirms nginx reaches the bot via localhost; (b) leave public.
2. Daily backup retention: 7 / 14 / 30 days — recommend 14.
3. Release timing: merge to main replaces the live image → schedule a quiet hour, owner present for checks (~10 min pause, no lost messages).

## Review round 1

Verdict: accept with changes (plan-reviewer, 2026-11-25). Verified: 173 tests, 4 MySQL-specific paths, wrong users PK (dump has only UNIQUE(user_id, group_id)), asyncpg 0.30.0 resolves. No int-to-varchar binds found (ids converted to str everywhere) — asyncpg strict-typing risk smaller than stated.

What to change:
1. Backup cron broken: `$POSTGRES_USER`/`$POSTGRES_DB` don't exist on the host → empty/failed dumps unnoticed. Run `docker exec team-group-bot-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'`; check dump non-empty + log; weekly copy off-server.
2. CI pushes `:latest` on every pull_request too (workflow `pull_request: {}` + `push: true`), not only main/dev. A PR image pulled before cutover won't start. → push only from main; add immutable tag (commit sha). Rollback step 1 must tag the image currently RUNNING (`docker inspect` container image id), not whatever `:latest` is.
3. Checksum: ORDER BY sorts string ids differently (MySQL _ci vs PG collation) → false mismatch. Sort in Python after normalisation. Checksums of what the script read can't detect read-time corruption (emoji → '?' via wrong charset). Add independent per-DB aggregates: row count, SUM(CHAR_LENGTH) of text columns, MIN/MAX dates; assert `MYSQL_SOURCE_URL` contains `charset=utf8mb4`.
4. Read-only only on paper: `SET SESSION TRANSACTION READ ONLY` at connect; dedicated SELECT-only MySQL user mandatory; compare source columns with expected set, abort on unknown column.
5. Don't mutate ids: NUL stripping must not touch `group_id`/`user_id`/`telegram_id`/`admin_id` — abort instead (health check 60 s after start deletes groups Telegram doesn't know).
6. No rehearsal on real server: bot reaches MySQL from a different internal IP after network change; grants/firewall may block → copy fails during downtime. Rehearse days before into a temporary PG DB while bot runs → measured time + network check.
7. `pg_isready` passes during initdb temp server (unix socket only) → healthcheck with `-h 127.0.0.1` + `start_period`.
8. `docker compose config` prints interpolated secrets → `config -q` only.
9. `.dockerignore` only excludes root env file → exclude `**/.env`, `**/.env.*` (keep `!.env.example`), `prod/`, `*.sql`/dumps, `pgdata/`, `**/pgdata/`.
10. Tests: schema prep sync (alembic env.py runs `asyncio.run`); set `DB_LINK_CONNECTION` to test URL during prep; keep "db name contains test" guard; `compare_metadata` with `compare_server_default=True`.
11. Hidden-by-sqlite: string length (PG rejects over-length — validate/truncate MCP key label, author names, titles); tz-aware cursor/since/until → naive UTC before binding.
12. Audit NUL stripping unconditional (method/tool_name/client_ip).
13. Cutover step 1: record "before" numbers (screenshot of admin statistics).
14. Rollback limits honest: after rollback to MySQL new groups/users/category changes/broadcasts made in PG are lost. Practical window = hours.

Money: no paid services; PG ~100–200 MB RAM; 14 daily dumps = MBs; off-server copy ~$0–1/month.

Answers to open decisions: (1) port local-only, but confirm how nginx reaches the bot BEFORE cutover (if nginx is containerised and reaches via host IP, localhost bind cuts it → shared docker network instead); (2) 14 days, only after fixing the command and with weekly off-server copy; (3) quiet hour, owner present, only after on-server rehearsal.

Missing: rehearsal; image version tags; PG password change instructions (POSTGRES_PASSWORD applies only on first init — must ALTER USER); alert when backup not created + disk space monitoring; unauth audit growth via spoofed X-Forwarded-For while port open; test that 3 s capture timeout doesn't poison asyncpg pool.

Owner decisions proposed: (1) release archive together with the move (recommended, with rehearsal + closed port) vs move first, archive a week later; (2) rollback window: 1 day (recommended), 1 week with reverse-copy script, 1 week without (data loss); (3) backups: same server only vs + weekly off-server copy (recommended).

# v2 amendments (2026-11-25, after review)

Owner: everything ships at once, one cutover, no staged release, keep it lean. No second review round.

Accepted (implement):
- Review items 1–5, 7–13 as written (backup command via `docker exec … sh -c`, non-empty dump check; CI push only from main + sha tag; Python-side sort + independent per-DB aggregates + `charset=utf8mb4` assert; read-only session + abort on unknown columns; never mutate ids, abort on NUL in ids; TCP healthcheck + start_period; `config -q`; wider `.dockerignore`; sync alembic test prep with test-name guard and `compare_server_default=True`; string length truncation at boundaries + tz-aware → naive UTC; unconditional audit NUL strip; "before" stats at cutover).
- Item 6 (rehearsal) reduced: copy script supports `--dry-run` into a throwaway PG database; run it on the server right before cutover in the same session (minutes, not days).
- Item 14: runbook states plainly — rollback window 1 day, no reverse-copy script; changes made in PG are lost on rollback.
- Runbook gets PG password-change note (ALTER USER, POSTGRES_PASSWORD only on first init).
- Test: capture timeout doesn't poison the asyncpg pool.

## Rejected
- Separate release of archive a week after the move — owner: ship everything at once.
- Reverse-copy script PG → MySQL — owner wants it lean; rollback window 1 day.
- Weekly off-server backup copy, backup-failure alerts, disk monitoring — out of scope now (owner: no extra work).

## Decided without asking (v2)
- Port 8019 binding left unchanged in this release — closing it blindly could cut nginx off the bot; revisit once nginx wiring is confirmed.
- Backups: same server, daily, 14 days.
