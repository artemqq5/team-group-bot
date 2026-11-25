---
name: alembic-migrations
description: Conventions for Alembic migrations in team-group-bot (MySQL, async env.py) — alembic/env.py takes DB_LINK_CONNECTION from env and Base.metadata from bot/data/models.py, revision chain in alembic/versions/ (d8cd90ead460 → a1b2c3d4e5f6 → b2c3d4e5f6a7), hand-review checklist, MySQL-specific pitfalls (implicit commits, renames, server defaults), `alembic upgrade head` on container start. Use when working on alembic/, alembic.ini, env.py, versions/*.py, schema changes, new tables/columns, autogenerate, multiple heads, or deploy migrations.
---

# Alembic (async, MySQL)

## env.py

`alembic/env.py`: `load_dotenv()`, `config.set_main_option("sqlalchemy.url", os.getenv("DB_LINK_CONNECTION"))`, `target_metadata = Base.metadata` (from `bot.data.models` — all models live in that one file, so importing it registers everything), async engine + `connection.run_sync(do_run_migrations)`.

- Never put credentials in `alembic.ini`.
- New model in another module → import it in env.py, otherwise autogenerate emits `drop_table`.

## Workflow

1. Change `bot/data/models.py`.
2. `alembic revision --autogenerate -m "add x column"` (needs DB reachable) or write by hand. File name style used here: `00N_short_description.py`; `down_revision` = current head.
3. **Review by hand**: renames come out as drop+add (data loss) — use `op.alter_column(..., new_column_name=...)` with `existing_type`; check server_default, string lengths, indexes; `downgrade()` reverses `upgrade()`.
4. `alembic upgrade head` → `alembic downgrade -1` → `alembic upgrade head` on a non-live DB.
5. Commit model + migration together.

## Rules

- Never edit a revision that's applied on the server — add a new one.
- Multiple heads → `alembic merge heads -m "merge"`.
- App code never calls `Base.metadata.create_all()`.
- Migrations don't import app models; data migrations use `sa.table()` stubs + `op.execute`.
- `team_group_db.sql` is a legacy dump — not the source of truth, not updated.

## MySQL specifics

- DDL commits implicitly: a failed multi-step migration is NOT rolled back — keep one logical change per revision and make steps re-runnable where cheap.
- `alter_column` on MySQL needs `existing_type` (and `existing_nullable`/`existing_server_default`) or it drops attributes.
- New NOT NULL column on a non-empty table: add with `server_default`, or nullable → backfill → alter.
- Enums: prefer `String` + values in Python (`bot/data/other/accesses.py`) — MySQL ENUM changes need table rebuilds.
- Telegram ids are `sa.String(255)` in this schema (match existing columns).

## Deploy

`entrypoint.sh` runs `alembic upgrade head` before uvicorn; a failing migration stops the container start (`set -e`). Single container — no migration race.
