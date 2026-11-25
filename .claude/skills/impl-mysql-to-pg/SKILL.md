---
name: impl-mysql-to-pg
description: How team-group-bot moved from host MySQL to a PostgreSQL 16 container — asyncpg engine, squashed baseline 001_pg_baseline.py, users composite PK, PG upsert on uq_chat_messages_group_msg, NUL stripping/truncation, one-off scripts/mysql_to_pg.py copy with read-only source + checksums + aggregates + --dry-run, tests on real Postgres, cutover/rollback/backup runbook docs/postgres-cutover.md. Use when working on db_connection.py, alembic baseline, conftest.py TEST_DATABASE_URL, mysql_to_pg, MYSQL_SOURCE_URL, pg_dump backups, cutover, rollback, or removing MySQL leftovers.
---

# MySQL → PostgreSQL move

## Problem
Owner wanted the bot's DB in Postgres inside the bot's own compose, with all existing MySQL data copied, shipped in one release together with the message archive + MCP feature. The MySQL server hosts other databases that must never be touched.

## Solution
- Engine `postgresql+asyncpg`, `connect_args={"timeout": 3, "command_timeout": 30}`, `hide_parameters=True` kept.
- Four MySQL revisions squashed into one PG baseline (`9f1c2a7e5b30`); new DB starts from it. `users` PK = `(user_id, group_id)` (old dump only had a UNIQUE). Nullability follows models; `compare_metadata(compare_server_default=True)` test proves baseline == models.
- Upsert: `pg_insert(...).on_conflict_do_update/do_nothing(constraint="uq_chat_messages_group_msg")`. Batched delete via `id IN (SELECT id ... LIMIT n)` (no `mysql_limit`). Search via `ilike`.
- PG rejects NUL bytes and over-length strings: strip NUL + truncate at write boundaries (capture text/authors, chat title, key label, audit strings). Ids are never mutated — NUL in an id aborts the copy (health check would otherwise delete "unknown" groups). Audit `arguments` is `json` (accepts `\u0000`).
- tz-aware cursor/since/until → naive UTC before binding.
- `scripts/mysql_to_pg.py`: source session `READ ONLY` + consistent snapshot, SELECT-only user, `charset=utf8mb4` asserted in `MYSQL_SOURCE_URL`, unknown/missing source columns abort, refuses non-empty target (`--truncate` clears PG tables only). Verification inside the target transaction before commit: row counts, sha256 over rows sorted in Python (MySQL/PG collations sort differently), independent per-DB aggregates (count, SUM(len) of text cols, MIN/MAX dates). Sequences reset to max+1. `--dry-run` copies into a temporary PG database and drops it (needs CREATEDB).
- CI pushes `:latest` + `:<sha>` only from main; rollback tags the RUNNING image (`docker inspect`) as `mysql-fallback`.

## Key files
- `bot/data/db_connection.py`, `bot/data/models.py`, `alembic/versions/001_pg_baseline.py`
- `bot/data/repositories/MessageRepository.py`, `McpAuditRepository.py`, `ChatRepository.py`, `bot/domain/mcp/tools.py`
- `scripts/mysql_to_pg.py`, `docs/postgres-cutover.md`, `prod/docker-compose.yaml`
- `tests/conftest.py`, `tests/test_pg_dialect.py`, `tests/test_mysql_to_pg.py`

## Gotchas
- `POSTGRES_PASSWORD` applies only on first init of `./pgdata`; later changes need `\password` in psql.
- Backups: `POSTGRES_*` exist only inside the container → `docker exec ... sh -c 'pg_dump ...'`; empty dump check.
- `pg_isready` must use `-h 127.0.0.1` (initdb temp server is unix-socket only) + `start_period`.
- `docker compose config` prints secrets — only `-q`.
- Rollback window 1 day, no reverse copy: anything written to PG after cutover is lost on rollback.
- After a week without rollback: drop aiomysql/PyMySQL, the copy script, `MYSQL_SOURCE_URL`, `extra_hosts`; owner drops only this bot's MySQL schema.
- Port 8019 still published on 0.0.0.0 (MCP reachable bypassing nginx, client_ip spoofable) — deliberately unchanged in this release.
